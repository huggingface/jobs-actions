"""Tests for the HFJobsClient — particularly the prebuilt-vs-bootstrap
command-selection logic, since that determines whether we trust the image's
own /entrypoint.sh or inject our bootstrap script."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dispatcher.hf_jobs import HFJobsClient


@pytest.fixture
def patched_api():
    with patch("dispatcher.hf_jobs.HfApi") as m:
        api = MagicMock()
        m.return_value = api
        api.run_job.return_value = MagicMock(id="hfjob-test")
        yield api


def _client(**overrides):
    defaults = dict(
        token="hf_test",
        namespace="ns",
        runner_image_cpu="ubuntu:22.04",
        runner_image_gpu="nvidia/cuda:12.4.0-runtime-ubuntu22.04",
    )
    defaults.update(overrides)
    return HFJobsClient(**defaults)


def test_dispatch_cpu_with_base_image_uses_bootstrap(patched_api):
    c = _client()
    c.dispatch(
        label="hf-jobs-cpu-basic",
        repo="o/r",
        runner_token="tok",
        runner_name="hfjobs-1-2",
    )
    kw = patched_api.run_job.call_args.kwargs
    assert kw["image"] == "ubuntu:22.04"
    assert kw["command"][0] == "bash"
    assert kw["command"][1] == "-c"
    assert "actions-runner" in kw["command"][2]
    assert kw["flavor"] == "cpu-basic"


def test_dispatch_gpu_with_base_image_uses_bootstrap_and_gpu_image(patched_api):
    c = _client()
    c.dispatch(
        label="hf-jobs-t4-small",
        repo="o/r",
        runner_token="tok",
        runner_name="hfjobs-1-2",
    )
    kw = patched_api.run_job.call_args.kwargs
    assert "cuda" in kw["image"]
    assert kw["command"][0] == "bash"
    assert kw["flavor"] == "t4-small"


def test_dispatch_with_prebuilt_image_uses_entrypoint(patched_api):
    c = _client(runner_image_cpu="ghcr.io/myorg/jobs-actions-runner:latest")
    c.dispatch(
        label="hf-jobs-cpu-basic",
        repo="o/r",
        runner_token="tok",
        runner_name="hfjobs-1-2",
    )
    kw = patched_api.run_job.call_args.kwargs
    assert kw["command"] == ["/entrypoint.sh"]


def test_runner_token_passes_via_secrets_not_env(patched_api):
    c = _client()
    c.dispatch(
        label="hf-jobs-cpu-basic",
        repo="o/r",
        runner_token="SUPERSECRET",
        runner_name="hfjobs-1-2",
    )
    kw = patched_api.run_job.call_args.kwargs
    assert "RUNNER_TOKEN" not in kw["env"]
    assert kw["secrets"]["RUNNER_TOKEN"] == "SUPERSECRET"


def test_dispatch_includes_labels_for_grouping(patched_api):
    c = _client()
    c.dispatch(
        label="hf-jobs-a10g-small",
        repo="myorg/myrepo",
        runner_token="tok",
        runner_name="hfjobs-1-2",
    )
    labels = patched_api.run_job.call_args.kwargs["labels"]
    assert labels["managed-by"] == "jobs-actions"
    assert labels["gh-repo"] == "myorg_myrepo"
    assert labels["gh-label"] == "hf-jobs-a10g-small"


def test_dispatch_label_values_match_hf_jobs_charset(patched_api):
    import re

    c = _client()
    c.dispatch(
        label="hf-jobs-a10g-small",
        repo="my org/my@repo#1",
        runner_token="tok",
        runner_name="hfjobs-1-2",
    )
    labels = patched_api.run_job.call_args.kwargs["labels"]
    for value in labels.values():
        assert re.fullmatch(r"[a-zA-Z0-9._-]*", value), value


def test_cancel_calls_api(patched_api):
    c = _client()
    c.cancel("hfjob-abc")
    patched_api.cancel_job.assert_called_once_with(job_id="hfjob-abc", namespace="ns")


def test_cancel_swallows_api_errors(patched_api):
    patched_api.cancel_job.side_effect = RuntimeError("already finished")
    c = _client()
    c.cancel("hfjob-abc")
