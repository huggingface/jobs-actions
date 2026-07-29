"""Tests for dispatcher environment configuration."""

from __future__ import annotations

import pytest

from dispatcher.config import Settings


@pytest.fixture
def base_env(monkeypatch, rsa_keypair):
    pem, _ = rsa_keypair
    monkeypatch.setenv("GH_APP_ID", "123456")
    monkeypatch.setenv("GH_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GH_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("HF_TOKEN", "hf_test")
    monkeypatch.delenv("HF_NAMESPACE", raising=False)
    monkeypatch.delenv("SPACE_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("SPACE_ID", raising=False)
    monkeypatch.delenv("ALLOWED_GITHUB_REPOSITORIES", raising=False)


def test_hf_namespace_uses_explicit_override(base_env, monkeypatch):
    monkeypatch.setenv("HF_NAMESPACE", "billing-org")
    monkeypatch.setenv("SPACE_AUTHOR_NAME", "space-owner")

    assert Settings.from_env().hf_namespace == "billing-org"


def test_hf_namespace_defaults_to_space_author(base_env, monkeypatch):
    monkeypatch.setenv("SPACE_AUTHOR_NAME", "space-owner")

    assert Settings.from_env().hf_namespace == "space-owner"


def test_hf_namespace_falls_back_to_space_id_owner(base_env, monkeypatch):
    monkeypatch.setenv("SPACE_ID", "space-owner/jobs-actions-dispatcher")

    assert Settings.from_env().hf_namespace == "space-owner"


def test_hf_namespace_required_outside_space(base_env):
    with pytest.raises(RuntimeError, match="HF_NAMESPACE"):
        Settings.from_env()


def test_allowed_github_repositories_defaults_to_unrestricted(base_env, monkeypatch):
    monkeypatch.setenv("SPACE_AUTHOR_NAME", "space-owner")

    assert Settings.from_env().allowed_github_repositories is None


def test_allowed_github_repositories_parses_and_normalizes(base_env, monkeypatch):
    monkeypatch.setenv("SPACE_AUTHOR_NAME", "space-owner")
    monkeypatch.setenv(
        "ALLOWED_GITHUB_REPOSITORIES",
        " Gradio-App/Trackio, huggingface/trl ",
    )

    assert Settings.from_env().allowed_github_repositories == frozenset(
        {"gradio-app/trackio", "huggingface/trl"}
    )


@pytest.mark.parametrize(
    "value",
    ["huggingface", "/trl", "huggingface/", "huggingface/trl/extra"],
)
def test_allowed_github_repositories_rejects_invalid_entries(
    base_env, monkeypatch, value
):
    monkeypatch.setenv("SPACE_AUTHOR_NAME", "space-owner")
    monkeypatch.setenv("ALLOWED_GITHUB_REPOSITORIES", value)

    with pytest.raises(RuntimeError, match="ALLOWED_GITHUB_REPOSITORIES"):
        Settings.from_env()
