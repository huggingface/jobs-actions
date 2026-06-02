"""Configuration loaded from environment.

All settings are read once at import time. Values are validated where it's cheap
to do so; anything that requires a network call (e.g. verifying the HF token)
fails lazily on first use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required env var: {name}. "
            f"See setup/SETUP.md for the full list."
        )
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _hf_namespace() -> str:
    value = os.environ.get("HF_NAMESPACE", "").strip()
    if value:
        return value

    # HF Spaces expose these built-ins at runtime. Defaulting to the Space
    # owner keeps duplicated Spaces easier to configure, while HF_NAMESPACE
    # still allows operators to bill jobs to a different namespace.
    value = os.environ.get("SPACE_AUTHOR_NAME", "").strip()
    if value:
        return value

    space_id = os.environ.get("SPACE_ID", "").strip()
    if "/" in space_id:
        return space_id.split("/", 1)[0]

    raise RuntimeError(
        "Missing required env var: HF_NAMESPACE. "
        "Set it explicitly when not running inside an HF Space."
    )


@dataclass(frozen=True)
class Settings:
    # GitHub App
    gh_app_id: str
    gh_app_private_key: str
    webhook_secret: str

    # HF
    hf_token: str
    hf_namespace: str  # who gets billed for jobs

    # Runner images
    runner_image_cpu: str
    runner_image_gpu: str

    # Behavior
    default_timeout: str  # "1h" etc.
    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        # PEM keys are typically multi-line. Allow `\n` literal in env for ease
        # of pasting into HF Space "Secrets" UI which is single-line.
        raw_key = _required("GH_APP_PRIVATE_KEY")
        if "\\n" in raw_key and "\n" not in raw_key:
            raw_key = raw_key.replace("\\n", "\n")

        return cls(
            gh_app_id=_required("GH_APP_ID"),
            gh_app_private_key=raw_key,
            webhook_secret=_required("GH_WEBHOOK_SECRET"),
            hf_token=_required("HF_TOKEN"),
            hf_namespace=_hf_namespace(),
            # Default to public base images + runtime install. Operators who
            # care about cold-start latency can point these at prebuilt
            # ghcr.io/<their-org>/jobs-actions-runner[:tag] images instead;
            # see runner/Dockerfile{,.gpu}.
            runner_image_cpu=_optional("RUNNER_IMAGE_CPU", "ubuntu:22.04"),
            runner_image_gpu=_optional(
                "RUNNER_IMAGE_GPU",
                "nvidia/cuda:12.4.0-runtime-ubuntu22.04",
            ),
            default_timeout=_optional("JOB_TIMEOUT", "1h"),
            log_level=_optional("LOG_LEVEL", "INFO"),
        )
