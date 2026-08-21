"""Thin wrapper around `huggingface_hub.HfApi` for our dispatch flow.

We isolate HF Jobs interaction here so it can be mocked cleanly in tests.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from huggingface_hub import HfApi

from .flavors import LABEL_TO_FLAVOR, is_gpu_flavor
from .runner_bootstrap import BOOTSTRAP

log = logging.getLogger(__name__)

_INVALID_LABEL_CHARS = re.compile(r"[^a-zA-Z0-9._-]")


def sanitize_label_value(value: str) -> str:
    """Coerce a label value to the charset HF Jobs accepts.

    HF Jobs validates label values against `^[a-zA-Z0-9._-]*$` and rejects the
    whole request otherwise. `gh-repo` is an `owner/name` slug, so the slash
    would 400 every dispatch and leave the GitHub job queued forever.
    """
    return _INVALID_LABEL_CHARS.sub("_", value)


@dataclass
class DispatchResult:
    job_id: str
    flavor: str
    image: str


class HFJobsClient:
    def __init__(
        self,
        *,
        token: str,
        namespace: str,
        runner_image_cpu: str,
        runner_image_gpu: str,
        timeout: str = "1h",
    ) -> None:
        self._api = HfApi(token=token)
        self._namespace = namespace
        self._image_cpu = runner_image_cpu
        self._image_gpu = runner_image_gpu
        self._timeout = timeout

    def dispatch(
        self,
        *,
        label: str,
        repo: str,
        runner_token: str,
        runner_name: str,
    ) -> DispatchResult:
        flavor = LABEL_TO_FLAVOR[label]
        image = self._image_gpu if is_gpu_flavor(flavor) else self._image_cpu

        env = {
            "GH_REPO": repo,
            "RUNNER_LABELS": label,
            "RUNNER_NAME": runner_name,
        }
        # RUNNER_TOKEN is sensitive — pass via `secrets` so it's not echoed
        # in HF Jobs logs or the inspect_job output.
        secrets = {"RUNNER_TOKEN": runner_token}

        # If the operator pointed us at a prebuilt runner image we trust it has
        # `/entrypoint.sh` (see runner/Dockerfile). Otherwise default to a
        # public base image + inline bootstrap that installs the GHA runner
        # at job startup — no image hosting required.
        is_prebuilt = "jobs-actions-runner" in image
        if is_prebuilt:
            command = ["/entrypoint.sh"]
        else:
            command = ["bash", "-c", BOOTSTRAP]

        job = self._api.run_job(
            image=image,
            command=command,
            env=env,
            secrets=secrets,
            flavor=flavor,
            timeout=self._timeout,
            namespace=self._namespace,
            labels={
                "managed-by": "jobs-actions",
                "gh-repo": sanitize_label_value(repo),
                "gh-label": sanitize_label_value(label),
            },
        )
        log.info(
            "dispatched HF Job",
            extra={
                "hf_job_id": job.id,
                "flavor": flavor,
                "repo": repo,
                "label": label,
                "namespace": self._namespace,
            },
        )
        return DispatchResult(job_id=job.id, flavor=flavor, image=image)

    def cancel(self, job_id: str) -> None:
        try:
            self._api.cancel_job(job_id=job_id, namespace=self._namespace)
            log.info("cancelled HF Job", extra={"hf_job_id": job_id})
        except Exception as e:
            # Job might already be finished — that's fine.
            log.warning(
                "cancel failed (job may already be done)",
                extra={"hf_job_id": job_id, "error": str(e)},
            )
