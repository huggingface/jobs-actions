"""GitHub App authentication and runner-token minting.

Two tokens are involved:

1. **App JWT** — signed locally with the App's private key. Short-lived
   (~10min). Used to call `/app/installations/.../access_tokens`.
2. **Installation token** — returned by GitHub for one installation.
   Lifetime: 1 hour. Used to call repo-level APIs.

We cache installation tokens per installation_id for ~50min to avoid burning
through GitHub's rate limit. Runner registration tokens are short-lived
(1 hour) and single-use; we never cache them.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field

import httpx
import jwt as pyjwt

log = logging.getLogger(__name__)

# Installation tokens are valid for 60 minutes. Refresh at 50 to stay safe.
INSTALLATION_TOKEN_TTL = 60 * 60
INSTALLATION_TOKEN_REFRESH_AT = 50 * 60

GITHUB_API = "https://api.github.com"


def verify_signature(body: bytes, signature_header: str, secret: bytes) -> bool:
    """Constant-time verify of GitHub's X-Hub-Signature-256 header.

    GitHub signs the raw request body with HMAC-SHA256 using the configured
    webhook secret. The header is `sha256=<hex>`.
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def make_app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    """Generate a short-lived JWT signed with the App's private key.

    `iat` is backdated 60s to tolerate clock skew between us and GitHub.
    `exp` is 9 minutes out — GitHub's max is 10.
    """
    now = now if now is not None else int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
    return pyjwt.encode(payload, private_key, algorithm="RS256")


@dataclass
class _CachedToken:
    token: str
    expires_at: float


@dataclass
class GitHubAppClient:
    app_id: str
    private_key: str
    http_client: httpx.AsyncClient = field(default_factory=httpx.AsyncClient)
    _installation_tokens: dict[int, _CachedToken] = field(default_factory=dict)

    async def installation_token(self, installation_id: int) -> str:
        """Return a fresh-enough installation access token, minting if needed."""
        cached = self._installation_tokens.get(installation_id)
        if cached and cached.expires_at > time.time():
            return cached.token

        jwt_token = make_app_jwt(self.app_id, self.private_key)
        r = await self.http_client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        token = data["token"]
        # Cap the cache TTL at 50min regardless of server-reported expiry.
        self._installation_tokens[installation_id] = _CachedToken(
            token=token,
            expires_at=time.time() + INSTALLATION_TOKEN_REFRESH_AT,
        )
        return token

    async def runner_registration_token(self, repo: str, installation_token: str) -> str:
        """Mint a one-shot runner registration token.

        These are valid for 1 hour and are intended to be consumed immediately
        by `actions/runner` during `config.sh`.
        """
        r = await self.http_client.post(
            f"{GITHUB_API}/repos/{repo}/actions/runners/registration-token",
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["token"]

    async def _get(self, path: str, token: str) -> httpx.Response:
        response = await self.http_client.get(
            f"{GITHUB_API}/{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        response.raise_for_status()
        return response

    async def workflow_job(self, repo: str, job_id: int, token: str) -> dict:
        return (await self._get(f"repos/{repo}/actions/jobs/{job_id}", token)).json()

    async def runners(self, repo: str, token: str) -> list[dict]:
        runners = []
        page = 1
        while True:
            response = await self._get(
                f"repos/{repo}/actions/runners?per_page=100&page={page}", token
            )
            runners.extend(response.json()["runners"])
            if "next" not in response.links:
                return runners
            page += 1

    async def remove_runner(self, repo: str, runner_id: int, token: str) -> None:
        response = await self.http_client.delete(
            f"{GITHUB_API}/repos/{repo}/actions/runners/{runner_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self.http_client.aclose()
