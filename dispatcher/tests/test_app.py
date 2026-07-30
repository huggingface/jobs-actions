"""End-to-end tests for the dispatcher FastAPI app.

The `client` fixture provides a TestClient with fake GitHub + HF Jobs clients
already wired in, so we can exercise the full webhook path.
"""

from __future__ import annotations

from dispatcher.tests.helpers import encode, sign, workflow_job_payload


def _post(client, payload, secret="s3cret", event="workflow_job"):
    body = encode(payload)
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body, secret),
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        },
    )


def test_root_returns_metadata(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "jobs-actions-dispatcher"
    assert "supported_labels" in body
    assert "hf-jobs-cpu-basic" in body["supported_labels"]
    assert body["configured"] is True


def test_unconfigured_mode_keeps_landing_page_alive(monkeypatch):
    """If env is missing, the Space should still serve / and /healthz so the
    operator can point a webhook at it during setup. Webhook POSTs 503."""
    from fastapi.testclient import TestClient

    from dispatcher import app as app_mod

    monkeypatch.delenv("GH_APP_ID", raising=False)
    monkeypatch.delenv("GH_APP_PRIVATE_KEY", raising=False)

    # Pass settings=None so the lifespan goes through the env path and fails.
    app = app_mod.make_app(None)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is False
        assert "next_steps" in body

        h = c.get("/healthz")
        assert h.status_code == 200
        assert h.json()["status"] == "needs-config"

        w = c.post("/webhook", content=b"{}")
        assert w.status_code == 503


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_webhook_rejects_bad_signature(client):
    payload = workflow_job_payload()
    body = encode(payload)
    r = client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Event": "workflow_job",
        },
    )
    assert r.status_code == 401


def test_webhook_rejects_missing_signature(client):
    payload = workflow_job_payload()
    body = encode(payload)
    r = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "workflow_job"},
    )
    assert r.status_code == 401


def test_webhook_pong_on_ping(client):
    body = encode({"zen": "Anything added dilutes everything else."})
    r = client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body, "s3cret"),
            "X-GitHub-Event": "ping",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "pong": True}


def test_webhook_ignores_unrelated_events(client):
    payload = {"action": "opened"}
    r = _post(client, payload, event="pull_request")
    assert r.status_code == 200
    assert r.json()["skipped"] == "event=pull_request"


def test_queued_with_hf_label_dispatches(client, fake_gh, fake_hf):
    payload = workflow_job_payload(
        action="queued",
        labels=["hf-jobs-cpu-basic"],
        repo="owner/repo",
        installation_id=1,
        run_id=11,
        job_id=22,
    )
    r = _post(client, payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["label"] == "hf-jobs-cpu-basic"
    assert body["flavor"] == "cpu-basic"
    assert body["hf_job_id"].startswith("hfjob-")

    # Verify the path through the fakes
    assert len(fake_hf.dispatches) == 1
    d = fake_hf.dispatches[0]
    assert d["label"] == "hf-jobs-cpu-basic"
    assert d["repo"] == "owner/repo"
    assert d["runner_token"] == "RUNNERTOKEN-XYZ"
    assert d["runner_name"] == "hfjobs-11-22"
    assert d["flavor"] == "cpu-basic"

    # GitHub side: installation token then runner token
    ops = [c["op"] for c in fake_gh.calls]
    assert ops == ["installation_token", "runner_registration_token"]


def test_queued_with_gpu_label_dispatches_gpu_image(client, fake_hf):
    payload = workflow_job_payload(
        action="queued",
        labels=["hf-jobs-t4-small"],
    )
    r = _post(client, payload)
    assert r.status_code == 200
    assert r.json()["flavor"] == "t4-small"


def test_queued_without_hf_label_skips(client, fake_hf):
    payload = workflow_job_payload(
        action="queued",
        labels=["ubuntu-latest"],
    )
    r = _post(client, payload)
    assert r.status_code == 200
    assert "skipped" in r.json()
    assert fake_hf.dispatches == []


def test_queued_from_repository_outside_allowlist_skips(
    client, fake_gh, fake_hf, settings
):
    object.__setattr__(
        settings,
        "allowed_github_repositories",
        frozenset({"gradio-app/trackio", "huggingface/trl"}),
    )
    payload = workflow_job_payload(
        action="queued",
        labels=["hf-jobs-cpu-basic"],
        repo="someone-else/expensive-ci",
    )

    r = _post(client, payload)

    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "skipped": "repository not allowed",
        "repo": "someone-else/expensive-ci",
    }
    assert fake_gh.calls == []
    assert fake_hf.dispatches == []


def test_queued_from_repository_inside_allowlist_dispatches(
    client, fake_gh, fake_hf, settings
):
    object.__setattr__(
        settings,
        "allowed_github_repositories",
        frozenset({"gradio-app/trackio", "huggingface/trl"}),
    )
    fake_gh.runner_tokens_by_repo["HuggingFace/TRL"] = "RUNNERTOKEN-XYZ"
    payload = workflow_job_payload(
        action="queued",
        labels=["hf-jobs-cpu-basic"],
        repo="HuggingFace/TRL",
    )

    r = _post(client, payload)

    assert r.status_code == 200
    assert r.json()["hf_job_id"].startswith("hfjob-")
    assert len(fake_hf.dispatches) == 1


def test_queued_with_self_hosted_and_hf_label_dispatches(client, fake_hf):
    payload = workflow_job_payload(
        action="queued",
        labels=["self-hosted", "Linux", "hf-jobs-a10g-small"],
    )
    r = _post(client, payload)
    assert r.status_code == 200
    assert r.json()["flavor"] == "a10g-small"
    assert len(fake_hf.dispatches) == 1


def test_queued_missing_installation_id_400s(client):
    payload = workflow_job_payload(
        action="queued",
        labels=["hf-jobs-cpu-basic"],
        installation_id=None,
    )
    r = _post(client, payload)
    assert r.status_code == 400


def test_completed_cancellation_cancels_active_hf_job(client, fake_hf):
    # First queue a job…
    payload_q = workflow_job_payload(
        action="queued",
        labels=["hf-jobs-cpu-basic"],
        run_id=33,
        job_id=44,
    )
    r = _post(client, payload_q)
    hf_job_id = r.json()["hf_job_id"]

    # …then send a cancelled completion for the same (run_id, job_id).
    payload_c = workflow_job_payload(
        action="completed",
        labels=["hf-jobs-cpu-basic"],
        run_id=33,
        job_id=44,
        conclusion="cancelled",
    )
    r2 = _post(client, payload_c)
    assert r2.status_code == 200
    assert r2.json()["cancelled_hf_job_id"] == hf_job_id
    assert fake_hf.cancels == [hf_job_id]


def test_completed_success_does_not_cancel(client, fake_hf):
    payload_q = workflow_job_payload(
        action="queued",
        labels=["hf-jobs-cpu-basic"],
        run_id=33,
        job_id=44,
    )
    _post(client, payload_q)

    payload_c = workflow_job_payload(
        action="completed",
        labels=["hf-jobs-cpu-basic"],
        run_id=33,
        job_id=44,
        conclusion="success",
    )
    r = _post(client, payload_c)
    assert r.status_code == 200
    assert fake_hf.cancels == []
