"""FastAPI dispatcher.

Entrypoints:
    GET  /         — health/metadata for humans browsing the Space
    GET  /healthz  — liveness probe
    POST /webhook  — GitHub App webhook

The webhook flow:
    1. Verify HMAC against the configured secret.
    2. Filter for `workflow_job.queued` / `workflow_job.completed`.
    3. On queued: find an `hf-jobs-*` label, mint a runner token, dispatch.
    4. On completed: best-effort cancel the corresponding HF Job if still
       running (handles the case where GitHub cancels a workflow before our
       runner picked it up).
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from . import __version__
from .config import Settings
from .flavors import LABEL_TO_FLAVOR, resolve_label, supported_labels, is_gpu_flavor
from .github_app import GitHubAppClient, verify_signature
from .hf_jobs import HFJobsClient

log = logging.getLogger("jobs_actions.dispatcher")

# In-memory map: (run_id, job_id) -> HF Job id. Used to support cancellation
# when GitHub fires workflow_job.completed with conclusion=cancelled.
# Lives only for the lifetime of this process — Space restarts wipe it. That's
# acceptable: stranded HF Jobs will hit their own timeout and exit.
_active_jobs: dict[tuple[int, int], str] = {}


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
        app.state.deps = {"settings": s, "gh": gh, "hf": hf}
        log.info("dispatcher ready (namespace=%s)", s.hf_namespace)
        try:
            yield
        finally:
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

        return await _handle_workflow_job(
            payload,
            gh=gh,
            hf=hf,
            allowed_repositories=s.allowed_github_repositories,
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
) -> dict[str, Any]:
    action = payload.get("action")
    wj = payload.get("workflow_job", {})
    labels = wj.get("labels", [])
    run_id = wj.get("run_id")
    job_id = wj.get("id")
    key = (run_id, job_id) if run_id and job_id else None

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
        inst_token = await gh.installation_token(installation_id)
        runner_token = await gh.runner_registration_token(repo, inst_token)
        result = hf.dispatch(
            label=hf_label,
            repo=repo,
            image=runner_images[image_label],
            runner_token=runner_token,
            runner_name=runner_name,
            runner_label=gh_label,
        )

        if key:
            _active_jobs[key] = result.job_id

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
        # On completion, drop our tracking. On cancellation, also try to stop
        # the HF Job in case our runner hadn't picked up the work yet.
        hf_job_id = _active_jobs.pop(key, None) if key else None
        conclusion = wj.get("conclusion")
        if action == "completed" and conclusion == "cancelled" and hf_job_id:
            hf.cancel(hf_job_id)
            return {"ok": True, "cancelled_hf_job_id": hf_job_id}
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
