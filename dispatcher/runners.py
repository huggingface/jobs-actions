"""Track provisioned runners independently of the jobs that requested them."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

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


class RunnerTracker:
    def __init__(self, idle_timeout: int = 300):
        self.runners: dict[tuple[str, str], Runner] = {}
        # Bound lock storage while allowing unrelated runners to progress.
        # A queued delivery and lifecycle/retirement operations for the same
        # runner share a lock, including provisioning's network awaits.
        self._locks = [asyncio.Lock() for _ in range(64)]
        self.idle_timeout = idle_timeout

    def lock_for(self, repo: str, name: str) -> asyncio.Lock:
        return self._locks[hash((repo.lower(), name)) % len(self._locks)]

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

    async def watch(self, gh: GitHubAppClient, hf: HFJobsClient) -> None:
        while True:
            await asyncio.sleep(30)
            await self.reap(gh, hf)
