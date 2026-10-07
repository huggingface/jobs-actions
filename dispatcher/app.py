"""FastAPI dispatcher.

Entrypoints:
    GET  /         — health/metadata for humans browsing the Space
    GET  /healthz  — liveness probe
    POST /webhook  — GitHub App webhook

The webhook flow:
    1. Verify HMAC against the configured secret.
    2. Filter for `workflow_job.queued` / `workflow_job.completed`.
    3. On queued: find an `hf-jobs-*` label, mint a runner token, dispatch.
    4. Track the actual runner in start/completion events.
    5. Periodically retire surplus idle runners and replace missing queue capacity.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from . import __version__
from .config import Settings
from .flavors import LABEL_TO_FLAVOR, is_gpu_flavor, resolve_label, supported_labels
from .github_app import GitHubAppClient, verify_signature
from .hf_jobs import HFJobsClient
from .runners import QueuedJob, Runner, RunnerTracker

log = logging.getLogger("jobs_actions.dispatcher")

def _state(request: Request) -> dict[str, Any]:
    return request.app.state.deps


def make_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Settings are loaded from env if not passed.

    Factored out so tests can pass a synthetic Settings + injected clients.

    If env vars are missing at boot, the app still starts and serves a
    "needs configuration" response on every endpoint. This keeps a freshly
    deployed Space reachable while the operator sets secrets.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            s = settings or Settings.from_env()
        except RuntimeError as e:
            logging.basicConfig(level="INFO")
            log.warning("dispatcher starting in UNCONFIGURED mode: %s", e)
            app.state.deps = {"error": str(e)}
            yield
            return

        logging.basicConfig(
            level=s.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        gh = GitHubAppClient(app_id=s.gh_app_id, private_key=s.gh_app_private_key)
        hf = HFJobsClient(
            token=s.hf_token,
            namespace=s.hf_namespace,
            timeout=s.default_timeout,
        )
        tracker = RunnerTracker(s.runner_idle_timeout)
        app.state.deps = {"settings": s, "gh": gh, "hf": hf, "tracker": tracker}
        watcher = asyncio.create_task(tracker.watch(gh, hf))
        log.info("dispatcher ready (namespace=%s)", s.hf_namespace)
        try:
            yield
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            await gh.aclose()

    app = FastAPI(title="jobs-actions dispatcher", version=__version__, lifespan=lifespan)

    def _configured(request: Request) -> bool:
        return "settings" in _state(request)

    @app.get("/")
    async def root(request: Request) -> dict[str, Any]:
        configured = _configured(request)
        body: dict[str, Any] = {
            "service": "jobs-actions-dispatcher",
            "version": __version__,
            "configured": configured,
            "supported_labels": supported_labels(),
            "supported_image_labels": [t[0] for t in _state(request)["settings"].runner_images] if configured else [],
            "docs": "https://github.com/huggingface/jobs-actions",
        }
        if not configured:
            body["next_steps"] = (
                "Set GH_APP_PRIVATE_KEY, GH_WEBHOOK_SECRET, HF_TOKEN as Space "
                "secrets; set GH_APP_ID as a Space variable; then restart. "
                "HF_NAMESPACE is optional and defaults to this Space's owner."
            )
            body["error"] = _state(request).get("error")
        return body

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        return {"status": "ok" if _configured(request) else "needs-config"}

    @app.post("/webhook")
    async def webhook(request: Request) -> dict[str, Any]:
        if not _configured(request):
            raise HTTPException(status_code=503, detail="dispatcher not configured")
        deps = _state(request)
        s: Settings = deps["settings"]
        gh: GitHubAppClient = deps["gh"]
        hf: HFJobsClient = deps["hf"]

        body = await request.body()
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(body, sig, s.webhook_secret.encode()):
            log.warning("invalid signature on webhook")
            raise HTTPException(status_code=401, detail="invalid signature")

        event = request.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"bad json: {e}") from e

        if event == "ping":
            return {"ok": True, "pong": True}

        if event != "workflow_job":
            return {"ok": True, "skipped": f"event={event}"}

        wj = payload.get("workflow_job", {})
        runner_name = (
            f"hfjobs-{wj.get('run_id')}-{wj.get('id')}"
            if payload.get("action") == "queued" else wj.get("runner_name", "")
        )
        async with deps["tracker"].lock_for(payload["repository"]["full_name"], runner_name):
            return await _handle_workflow_job(
                payload,
                gh=gh,
                hf=hf,
                allowed_repositories=s.allowed_github_repositories,
                tracker=deps["tracker"],
                runner_images=dict(s.runner_images),
            )

    return app


async def _handle_workflow_job(
    payload: dict[str, Any],
    *,
    gh: GitHubAppClient,
    hf: HFJobsClient,
    allowed_repositories: frozenset[str] | None = None,
    runner_images: dict[str, str],
    tracker: RunnerTracker,
) -> dict[str, Any]:
    action = payload.get("action")
    wj = payload.get("workflow_job", {})
    labels = wj.get("labels", [])
    run_id = wj.get("run_id")
    job_id = wj.get("id")
    repo = payload["repository"]["full_name"].lower()

    if action == "queued":
        gh_label = resolve_label(labels)
        if gh_label is None:
            return {"ok": True, "skipped": "no hf-jobs-* label", "labels": labels}
        hf_label, image_label, gh_label = gh_label
        flavor = LABEL_TO_FLAVOR[hf_label]

        if image_label:
            image_label = image_label.upper()
        else:
            image_label = "GPU" if is_gpu_flavor(flavor) else "CPU"

        if image_label not in runner_images:
            log.warning(
                "skipping workflow job with no matching image label",
                extra={"label": image_label},
            )
            return {"ok": True, "skipped": "no matching image label", "labels": labels}

        repo = payload["repository"]["full_name"]
        if (
            allowed_repositories is not None
            and repo.lower() not in allowed_repositories
        ):
            log.warning(
                "skipping workflow job from repository outside allowlist",
                extra={"repo": repo},
            )
            return {
                "ok": True,
                "skipped": "repository not allowed",
                "repo": repo,
            }

        installation_id = payload.get("installation", {}).get("id")
        if not installation_id:
            raise HTTPException(
                status_code=400,
                detail="missing installation id in payload",
            )

        runner_name = f"hfjobs-{run_id}-{job_id}"
        runner_key = (repo.lower(), runner_name)
        if (repo.lower(), job_id) in tracker.queued:
            return {"ok": True, "skipped": "runner already provisioned"}
        inst_token = await gh.installation_token(installation_id)
        current = await gh.workflow_job(repo, job_id, inst_token)
        if current["status"] != "queued":
            return {"ok": True, "skipped": "job no longer queued"}
        tracker.queued[(repo.lower(), job_id)] = QueuedJob(
            repo.lower(), installation_id, run_id, job_id, gh_label,
            hf_label, runner_images[image_label],
        )
        if runner_key in tracker.runners:
            # A requeued job still needs demand tracking even if its original
            # runner is busy. Reconciliation will allocate a fresh replacement.
            return {"ok": True, "skipped": "runner already provisioned"}
        runner_token = await gh.runner_registration_token(repo, inst_token)
        result = await asyncio.to_thread(
            hf.dispatch,
            label=hf_label,
            repo=repo,
            image=runner_images[image_label],
            runner_token=runner_token,
            runner_name=runner_name,
            runner_label=gh_label,
        )

        tracker.runners[runner_key] = Runner(
            repo.lower(), installation_id, runner_name, result.job_id, label=gh_label
        )

        log.info(
            "queued -> dispatched",
            extra={
                "repo": repo,
                "label": hf_label,
                "image": result.image,
                "flavor": result.flavor,
                "hf_job_id": result.job_id,
                "gh_run_id": run_id,
                "gh_job_id": job_id,
            },
        )
        return {
            "ok": True,
            "hf_job_id": result.job_id,
            "flavor": result.flavor,
            "label": gh_label,
        }

    if action in {"completed", "in_progress"}:
        tracker.queued.pop((repo, job_id), None)
        # The runner that accepted this job may have been provisioned for a
        # completely different job. Never cancel by the original queued key.
        runner_key = (repo, wj.get("runner_name"))
        runner = tracker.runners.get(runner_key)
        conclusion = wj.get("conclusion")
        if runner:
            runner.claimed = True
            if action == "completed":
                if conclusion == "cancelled":
                    runner.retired = True
                    if await asyncio.to_thread(hf.cancel, runner.hf_job_id):
                        tracker.runners.pop(runner_key, None)
                    return {"ok": True, "cancelled_hf_job_id": runner.hf_job_id}
                tracker.runners.pop(runner_key, None)
        return {"ok": True, "action": action, "conclusion": conclusion}

    return {"ok": True, "skipped": f"action={action}"}


# Export an ASGI app for `uvicorn dispatcher.app:app`.
# We only build it eagerly when not under test, to avoid requiring env vars
# at import time during pytest collection.
import os as _os  # noqa: E402

if not _os.environ.get("JOBS_ACTIONS_SKIP_BOOT"):
    try:
        app = make_app()
    except RuntimeError:
        # Missing env vars — fine when running tests or in CI lint contexts.
        app = None  # type: ignore[assignment]
else:
    app = None  # type: ignore[assignment]


# Convenience for ad-hoc inspection
__all__ = [
    "make_app",
    "LABEL_TO_FLAVOR",
    "supported_labels",
]
