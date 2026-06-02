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
