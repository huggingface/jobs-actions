"""Shared pytest fixtures.

These give every test a real `make_app` instance whose downstream clients
(GitHub + HF Jobs) are replaced with controllable doubles. We don't mock the
FastAPI layer itself — that's the thing we want to exercise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

# Ensure module import does not try to build an app from real env.
os.environ.setdefault("JOBS_ACTIONS_SKIP_BOOT", "1")

from dispatcher import app as app_mod  # noqa: E402
from dispatcher.config import Settings  # noqa: E402
from dispatcher.hf_jobs import DispatchResult  # noqa: E402


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[str, Any]:
    """Generate an RSA keypair once per session for App JWT tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@pytest.fixture
def settings(rsa_keypair) -> Settings:
    pem, _ = rsa_keypair
    return Settings(
        gh_app_id="123456",
        gh_app_private_key=pem,
        webhook_secret="s3cret",
        hf_token="hf_test",
        hf_namespace="testuser",
        runner_images=(("CPU", "ghcr.io/test/jobs-actions-runner:cpu"), ("GPU", "ghcr.io/test/jobs-actions-runner-gpu:gpu")),
        allowed_github_repositories=None,
        default_timeout="1h",
        log_level="DEBUG",
    )


@dataclass
class FakeGH:
    inst_tokens: dict[int, str] = field(default_factory=lambda: {1: "inst-token-1"})
    runner_tokens_by_repo: dict[str, str] = field(
        default_factory=lambda: {"owner/repo": "RUNNERTOKEN-XYZ"}
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def installation_token(self, installation_id: int) -> str:
        self.calls.append({"op": "installation_token", "installation_id": installation_id})
        return self.inst_tokens[installation_id]

    async def runner_registration_token(self, repo: str, installation_token: str) -> str:
        self.calls.append(
            {"op": "runner_registration_token", "repo": repo, "inst": installation_token}
        )
        return self.runner_tokens_by_repo[repo]

    async def aclose(self) -> None:
        pass


class FakeHF:
    def __init__(self) -> None:
        self.dispatches: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self._next_id = 1

    def dispatch(
        self, *, label: str, repo: str, runner_token: str, runner_name: str
    ) -> DispatchResult:
        from dispatcher.flavors import LABEL_TO_FLAVOR, is_gpu_flavor

        flavor = LABEL_TO_FLAVOR[label]
        image = "gpu-image" if is_gpu_flavor(flavor) else "cpu-image"
        job_id = f"hfjob-{self._next_id:04d}"
        self._next_id += 1
        self.dispatches.append(
            {
                "label": label,
                "repo": repo,
                "runner_token": runner_token,
                "runner_name": runner_name,
                "flavor": flavor,
                "job_id": job_id,
            }
        )
        return DispatchResult(job_id=job_id, flavor=flavor, image=image)

    def cancel(self, job_id: str) -> None:
        self.cancels.append(job_id)


@pytest.fixture
def fake_gh() -> FakeGH:
    return FakeGH()


@pytest.fixture
def fake_hf() -> FakeHF:
    return FakeHF()


@pytest.fixture
def client(monkeypatch, settings, fake_gh, fake_hf):
    """A TestClient wired up with fake downstream clients.

    We monkey-patch `GitHubAppClient` and `HFJobsClient` constructors in
    `app_mod` to return our fakes, then call `make_app(settings)`.
    """
    monkeypatch.setattr(app_mod, "GitHubAppClient", lambda **kw: fake_gh)
    monkeypatch.setattr(app_mod, "HFJobsClient", lambda **kw: fake_hf)
    app = app_mod.make_app(settings)
    # Reset the cross-request _active_jobs map so tests don't leak.
    app_mod._active_jobs.clear()
    with TestClient(app) as c:
        yield c
