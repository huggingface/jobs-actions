"""Track provisioned runners independently of the jobs that requested them."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from uuid import uuid4

from .github_app import GitHubAppClient
from .hf_jobs import HFJobsClient

log = logging.getLogger(__name__)


@dataclass
class Runner:
    repo: str
    installation_id: int
    name: str
    hf_job_id: str
    idle_since: float | None = None
    claimed: bool = False
    retired: bool = False
    label: str = ""


@dataclass
class QueuedJob:
    repo: str
    installation_id: int
    run_id: int
    job_id: int
    label: str
    flavor_label: str
    image: str
    retry_at: float = 0
    failures: int = 0


class RunnerTracker:
    def __init__(self, idle_timeout: int = 300):
        self.runners: dict[tuple[str, str], Runner] = {}
        self.queued: dict[tuple[str, int], QueuedJob] = {}
        # Bound lock storage while allowing unrelated runners to progress.
        # Webhooks and reconciliation for the same repository share a lock,
        # including provisioning's awaits, so capacity is not counted twice.
        self._locks = [asyncio.Lock() for _ in range(64)]
        self.idle_timeout = idle_timeout

    def lock_for(self, repo: str, name: str) -> asyncio.Lock:
        return self._locks[hash(repo.lower()) % len(self._locks)]

    async def reap(self, gh: GitHubAppClient, hf: HFJobsClient) -> None:
        inventories: dict[str, dict[str, dict]] = {}
        for key, runner in list(self.runners.items()):
            async with self.lock_for(*key):
                if self.runners.get(key) is not runner:
                    continue
                try:
                    if await asyncio.to_thread(hf.is_finished, runner.hf_job_id):
                        self.runners.pop(key, None)
                        continue
                    if runner.retired:
                        if await asyncio.to_thread(hf.cancel, runner.hf_job_id):
                            self.runners.pop(key, None)
                        continue
                    token = await gh.installation_token(runner.installation_id)
                    if runner.repo not in inventories:
                        inventories[runner.repo] = {
                            r["name"]: r for r in await gh.runners(runner.repo, token)
                        }
                    registered = inventories[runner.repo].get(runner.name)
                    if registered is None or registered["status"] != "online":
                        runner.idle_since = None
                        continue  # Still booting, or already deregistered.
                    if registered["busy"]:
                        runner.claimed = True
                    if runner.claimed:
                        continue  # Ephemeral runners exit after their one job.
                    now = time.monotonic()
                    if runner.idle_since is None:
                        runner.idle_since = now
                    if now - runner.idle_since < self.idle_timeout:
                        continue
                    # GitHub refuses removal of a busy runner. Only stop HF
                    # compute after successful removal; never kill on a stale
                    # inventory alone or when removal is rejected.
                    await gh.remove_runner(runner.repo, registered["id"], token)
                    runner.retired = True
                    log.info("retired idle runner %s (HF job %s)", runner.name, runner.hf_job_id)
                    if await asyncio.to_thread(hf.cancel, runner.hf_job_id):
                        self.runners.pop(key, None)
                except Exception:
                    log.exception("runner reconciliation failed for %s", runner.name)

    async def recover(self, gh: GitHubAppClient, hf: HFJobsClient) -> None:
        """Replace missing capacity, even when no new webhook arrives.

        Keep demand independent of runner identity: a runner's first job can
        consume capacity that was originally launched for a different job.
        """
        for repo in {job.repo for job in self.queued.values()}:
            async with self.lock_for(repo, ""):
                try:
                    jobs = [job for job in self.queued.values() if job.repo == repo]
                    if not jobs:
                        continue
                    token = await gh.installation_token(jobs[0].installation_id)
                    # Refresh every job before allocating capacity. Never infer
                    # queued demand from a delayed webhook alone.
                    waiting = []
                    for job in jobs:
                        current = await gh.workflow_job(repo, job.job_id, token)
                        if current["status"] != "queued":
                            self.queued.pop((repo, job.job_id), None)
                        else:
                            waiting.append(job)
                    inventory = {r["name"]: r for r in await gh.runners(repo, token)}
                    capacity: dict[str, int] = {}
                    for runner in list(self.runners.values()):
                        if runner.repo != repo or runner.retired or runner.claimed:
                            continue
                        if await asyncio.to_thread(hf.is_finished, runner.hf_job_id):
                            self.runners.pop((repo, runner.name), None)
                            continue
                        registered = inventory.get(runner.name)
                        if registered and registered["busy"]:
                            runner.claimed = True
                            continue
                        # Include booting runners, so repeated polls do not
                        # launch a new instance on every cold-start interval.
                        capacity[runner.label] = capacity.get(runner.label, 0) + 1
                    for job in waiting:
                        if capacity.get(job.label, 0):
                            capacity[job.label] -= 1
                            continue
                        if time.monotonic() < job.retry_at:
                            continue
                        # A fresh name is essential: --replace must never evict
                        # the original runner while it executes another job.
                        name = f"hfjobs-{job.run_id}-{job.job_id}-{uuid4().hex[:12]}"
                        job.retry_at = time.monotonic() + min(300, 30 * 2 ** min(job.failures, 4))
                        try:
                            runner_token = await gh.runner_registration_token(repo, token)
                            # Status may have changed while minting credentials.
                            if (await gh.workflow_job(repo, job.job_id, token))["status"] != "queued":
                                self.queued.pop((repo, job.job_id), None)
                                continue
                            result = await asyncio.to_thread(
                                hf.dispatch, label=job.flavor_label, repo=repo,
                                image=job.image, runner_token=runner_token,
                                runner_name=name, runner_label=job.label,
                            )
                            self.runners[(repo, name)] = Runner(
                                repo, job.installation_id, name, result.job_id, label=job.label,
                            )
                            job.failures = 0
                            log.info("replaced missing capacity for GitHub job %s with %s", job.job_id, name)
                        except Exception:
                            job.failures += 1
                            log.exception("replacement dispatch failed for GitHub job %s", job.job_id)
                except Exception:
                    # A failed inventory/status request is not evidence that
                    # compute is missing. Leave state intact and retry later.
                    log.exception("queue reconciliation failed for %s", repo)

    async def watch(self, gh: GitHubAppClient, hf: HFJobsClient) -> None:
        while True:
            await asyncio.sleep(30)
            await self.reap(gh, hf)
            await self.recover(gh, hf)
