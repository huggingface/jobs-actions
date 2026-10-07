from __future__ import annotations

import hashlib
import hmac
import time

import httpx
import jwt as pyjwt
import pytest

from dispatcher.github_app import (
    GitHubAppClient,
    make_app_jwt,
    verify_signature,
)


def test_verify_signature_accepts_correct_hmac():
    body = b'{"hello": "world"}'
    secret = b"topsecret"
    sig = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, secret)


def test_verify_signature_rejects_wrong_secret():
    body = b'{"hello": "world"}'
    sig = "sha256=" + hmac.new(b"wrong", body, hashlib.sha256).hexdigest()
    assert not verify_signature(body, sig, b"topsecret")


def test_verify_signature_rejects_missing_prefix():
    body = b"x"
    bare = hmac.new(b"s", body, hashlib.sha256).hexdigest()
    assert not verify_signature(body, bare, b"s")


def test_verify_signature_rejects_empty():
    assert not verify_signature(b"x", "", b"s")


def test_make_app_jwt_payload(rsa_keypair):
    pem, pub = rsa_keypair
    token = make_app_jwt("12345", pem, now=1_000_000)
    decoded = pyjwt.decode(token, pub, algorithms=["RS256"], options={"verify_exp": False})
    assert decoded["iss"] == "12345"
    assert decoded["iat"] == 1_000_000 - 60
    assert decoded["exp"] == 1_000_000 + 9 * 60


@pytest.mark.asyncio
async def test_installation_token_minting(rsa_keypair, monkeypatch):
    pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        # The App JWT should appear in the Authorization header.
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(
            200, json={"token": "ghs_installation_xyz", "expires_at": "2099-01-01T00:00:00Z"}
        )

    transport = httpx.MockTransport(handler)
    client = GitHubAppClient(
        app_id="42",
        private_key=pem,
        http_client=httpx.AsyncClient(transport=transport),
    )
    token = await client.installation_token(installation_id=999)
    assert token == "ghs_installation_xyz"

    # Second call within TTL should be served from cache (no network).
    token2 = await client.installation_token(installation_id=999)
    assert token2 == "ghs_installation_xyz"
    await client.aclose()


@pytest.mark.asyncio
async def test_installation_token_refreshes_after_expiry(rsa_keypair):
    pem, _ = rsa_keypair
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"token": f"tok-{calls['n']}"})

    transport = httpx.MockTransport(handler)
    client = GitHubAppClient(
        app_id="42",
        private_key=pem,
        http_client=httpx.AsyncClient(transport=transport),
    )
    t1 = await client.installation_token(1)
    # Force expiry by rewriting cache.
    client._installation_tokens[1].expires_at = time.time() - 1
    t2 = await client.installation_token(1)
    assert t1 == "tok-1"
    assert t2 == "tok-2"
    await client.aclose()


@pytest.mark.asyncio
async def test_runner_registration_token(rsa_keypair):
    pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/actions/runners/registration-token"
        assert request.headers["Authorization"] == "Bearer instok"
        return httpx.Response(
            200,
            json={"token": "AAAAA-RUNNER-TOK", "expires_at": "2099-01-01T00:00:00Z"},
        )

    client = GitHubAppClient(
        app_id="42",
        private_key=pem,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    token = await client.runner_registration_token("owner/repo", "instok")
    assert token == "AAAAA-RUNNER-TOK"
    await client.aclose()


async def test_workflow_job_and_paginated_runners(rsa_keypair):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer instok"
        if request.url.path.endswith("/jobs/123"):
            return httpx.Response(200, json={"status": "in_progress"})
        page = request.url.params["page"]
        headers = {"Link": '<https://api.github.com/repos/o/r/actions/runners?page=2>; rel="next"'} if page == "1" else {}
        return httpx.Response(200, json={"runners": [{"id": int(page)}]}, headers=headers)

    client = GitHubAppClient("42", rsa_keypair[0], httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.workflow_job("o/r", 123, "instok") == {"status": "in_progress"}
    assert await client.runners("o/r", "instok") == [{"id": 1}, {"id": 2}]
    await client.aclose()


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422, 500])
async def test_runner_removal_rejection_propagates(rsa_keypair, status):
    def handler(request):
        assert request.method == "DELETE"
        assert request.url.path == "/repos/o/r/actions/runners/42"
        return httpx.Response(status)

    client = GitHubAppClient("42", rsa_keypair[0], httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(httpx.HTTPStatusError):
        await client.remove_runner("o/r", 42, "instok")
    await client.aclose()
