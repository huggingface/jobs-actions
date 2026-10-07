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


@pytest.fixture
def pending(tracker):
    from dispatcher.runners import QueuedJob

    tracker.queued[("owner/repo", 2)] = QueuedJob(
        "owner/repo", 1, 2, 2, "hf-jobs-cpu-basic", "hf-jobs-cpu-basic", "ubuntu:24.04",
    )
    return tracker.queued[("owner/repo", 2)]


async def test_failed_compute_is_replaced(tracker, pending, fake_gh, fake_hf, monkeypatch):
    monkeypatch.setattr(fake_hf, "is_finished", lambda job_id: job_id == "hf1")
    await tracker.recover(fake_gh, fake_hf)
    assert len(fake_hf.dispatches) == 1
    assert ("owner/repo", "hfjobs-1-1") not in tracker.runners


async def test_inventory_failure_never_launches_speculative_replacement(tracker, pending, fake_gh, fake_hf):
    fake_gh.runners = AsyncMock(side_effect=RuntimeError("unavailable"))
    await tracker.recover(fake_gh, fake_hf)
    assert not fake_hf.dispatches
    assert tracker.queued


async def test_failed_dispatch_backs_off_then_recovers(tracker, pending, fake_gh, fake_hf, monkeypatch):
    tracker.runners.clear()
    original = fake_hf.dispatch
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("temporarily unavailable")

    monkeypatch.setattr(fake_hf, "dispatch", fail)
    monkeypatch.setattr("dispatcher.runners.time.monotonic", lambda: 100)
    await tracker.recover(fake_gh, fake_hf)
    await tracker.recover(fake_gh, fake_hf)
    assert len(calls) == 1
    monkeypatch.setattr("dispatcher.runners.time.monotonic", lambda: 131)
    monkeypatch.setattr(fake_hf, "dispatch", original)
    await tracker.recover(fake_gh, fake_hf)
    assert len(fake_hf.dispatches) == 1


async def test_status_changes_during_token_mint_do_not_launch(tracker, pending, fake_gh, fake_hf):
    tracker.runners.clear()
    fake_gh.workflow_job = AsyncMock(side_effect=[{"status": "queued"}, {"status": "completed"}])
    await tracker.recover(fake_gh, fake_hf)
    assert not fake_hf.dispatches
    assert not tracker.queued


async def test_capacity_is_not_shared_across_labels(tracker, pending, fake_gh, fake_hf):
    next(iter(tracker.runners.values())).label = "hf-jobs-t4-small"
    await tracker.recover(fake_gh, fake_hf)
    assert len(fake_hf.dispatches) == 1


async def test_each_starting_runner_covers_only_one_queued_job(tracker, pending, fake_gh, fake_hf):
    from dataclasses import replace

    next(iter(tracker.runners.values())).label = pending.label
    tracker.queued[("owner/repo", 3)] = replace(pending, job_id=3)
    await tracker.recover(fake_gh, fake_hf)
    assert len(fake_hf.dispatches) == 1
    await tracker.recover(fake_gh, fake_hf)
    assert len(fake_hf.dispatches) == 1


async def test_concurrent_recovery_does_not_duplicate_capacity(tracker, pending, fake_gh, fake_hf):
    import asyncio

    tracker.runners.clear()
    await asyncio.gather(tracker.recover(fake_gh, fake_hf), tracker.recover(fake_gh, fake_hf))
    assert len(fake_hf.dispatches) == 1
