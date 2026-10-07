from unittest.mock import AsyncMock

import pytest

from dispatcher.runners import Runner, RunnerTracker


@pytest.fixture
def tracker():
    tracker = RunnerTracker(idle_timeout=300)
    tracker.runners[("owner/repo", "hfjobs-1-1")] = Runner("owner/repo", 1, "hfjobs-1-1", "hf1")
    return tracker


@pytest.fixture
def registered(fake_gh):
    runner = {"name": "hfjobs-1-1", "id": 42, "status": "online", "busy": False}
    fake_gh.registered_runners = [runner]
    return runner


async def test_idle_runner_is_removed_before_compute_stops(tracker, registered, fake_gh, fake_hf, monkeypatch):
    monkeypatch.setattr("dispatcher.runners.time.monotonic", lambda: 0)
    await tracker.reap(fake_gh, fake_hf)
    assert not fake_hf.cancels
    monkeypatch.setattr("dispatcher.runners.time.monotonic", lambda: 301)
    original_cancel = fake_hf.cancel

    def cancel(job_id):
        assert fake_gh.removals == [42]
        return original_cancel(job_id)

    monkeypatch.setattr(fake_hf, "cancel", cancel)
    await tracker.reap(fake_gh, fake_hf)
    assert fake_hf.cancels == ["hf1"]
    assert not tracker.runners


async def test_busy_runner_survives_idle_deadline(tracker, registered, fake_gh, fake_hf):
    runner = next(iter(tracker.runners.values()))
    runner.idle_since = -1000
    registered["busy"] = True
    await tracker.reap(fake_gh, fake_hf)
    registered["busy"] = False  # Job finished; runner is shutting down.
    await tracker.reap(fake_gh, fake_hf)
    assert runner.claimed
    assert not fake_gh.removals
    assert not fake_hf.cancels


async def test_busy_race_removal_rejection_never_cancels_compute(tracker, registered, fake_gh, fake_hf):
    next(iter(tracker.runners.values())).idle_since = -1000
    fake_gh.remove_runner = AsyncMock(side_effect=RuntimeError("runner became busy"))
    await tracker.reap(fake_gh, fake_hf)
    assert not fake_hf.cancels
    assert tracker.runners


async def test_failed_cancel_is_retried_after_successful_removal(tracker, registered, fake_gh, fake_hf, monkeypatch):
    next(iter(tracker.runners.values())).idle_since = -1000
    monkeypatch.setattr(fake_hf, "cancel", lambda _: False)
    await tracker.reap(fake_gh, fake_hf)
    assert next(iter(tracker.runners.values())).retired
    fake_gh.registered_runners = []
    monkeypatch.setattr(fake_hf, "cancel", lambda _: True)
    await tracker.reap(fake_gh, fake_hf)
    assert fake_gh.removals == [42]
    assert not tracker.runners


async def test_booting_runner_is_not_retired(tracker, fake_gh, fake_hf):
    await tracker.reap(fake_gh, fake_hf)
    assert next(iter(tracker.runners.values())).idle_since is None
    assert not fake_hf.cancels


async def test_finished_hf_job_is_forgotten(tracker, fake_gh, fake_hf, monkeypatch):
    monkeypatch.setattr(fake_hf, "is_finished", lambda _: True)
    await tracker.reap(fake_gh, fake_hf)
    assert not tracker.runners
    assert not fake_hf.cancels
