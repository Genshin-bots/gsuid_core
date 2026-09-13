"""Scheduler job listing must snapshot without re-locking per job."""

from __future__ import annotations

from datetime import datetime

from gsuid_core.webconsole import scheduler_api as mod


class _Trig:
    def __str__(self) -> str:
        return "interval[0:01:00]"


def _dummy() -> None:
    """hello doc"""
    return None


class _FakeJob:
    def __init__(self, jid: str, paused: bool) -> None:
        self.id = jid
        self.name = jid
        self.next_run_time = None if paused else datetime(2026, 1, 1, 12, 0, 0)
        self.trigger = _Trig()
        self.func = _dummy


class _FakeSched:
    def __init__(self, jobs: list[_FakeJob]) -> None:
        self._jobs = jobs
        self.get_job_calls = 0

    def get_jobs(self) -> list[_FakeJob]:
        return self._jobs

    def get_job(self, job_id: str) -> _FakeJob | None:
        self.get_job_calls += 1
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None


def test_collect_scheduler_jobs_does_not_call_get_job(monkeypatch) -> None:
    sched = _FakeSched([_FakeJob("a", False), _FakeJob("b", True)])
    monkeypatch.setattr(mod, "scheduler", sched)
    rows = mod.collect_scheduler_jobs()
    assert sched.get_job_calls == 0
    assert len(rows) == 2
    assert rows[0]["paused"] is False
    assert rows[0]["description"] == "hello doc"
    assert rows[0]["id"] == "a"
    assert rows[1]["paused"] is True
    assert rows[1]["next_run_time"] is None
