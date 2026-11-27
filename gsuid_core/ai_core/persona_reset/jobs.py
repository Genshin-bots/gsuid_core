"""人格重置 / 还原的后台任务注册表与进度（进程内，全局同一时间只跑一个）。"""

from __future__ import annotations

import time
import uuid
from dataclasses import field, dataclass

from gsuid_core.ai_core.persona_reset.types import (
    JobKind,
    JobStep,
    JobView,
    StepKey,
    JobStatus,
    StepStatus,
)


@dataclass
class _Step:
    key: StepKey
    estimate_s: float
    status: StepStatus = "pending"
    done: int = 0
    total: int = 0
    baseline: int = 0
    started_at: float = 0.0


@dataclass
class Job:
    job_id: str
    kind: JobKind
    persona: str
    steps: list[_Step]
    status: JobStatus = "running"
    error: str = ""
    backup_file: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def _get(self, key: StepKey) -> _Step:
        for step in self.steps:
            if step.key == key:
                return step
        raise KeyError(key)

    def has_step(self, key: StepKey) -> bool:
        return any(step.key == key for step in self.steps)

    def running_step(self) -> StepKey | None:
        for step in self.steps:
            if step.status == "running":
                return step.key
        return None

    def start(self, key: StepKey, total: int = 0, *, initial_done: int = 0) -> None:
        step = self._get(key)
        step.status = "running"
        step.total = total
        step.done = initial_done
        step.baseline = initial_done
        step.started_at = time.time()

    def advance(self, key: StepKey, n: int) -> None:
        step = self._get(key)
        step.done += n
        if step.total and step.done > step.total:
            step.total = step.done

    def end(self, key: StepKey, status: StepStatus = "done") -> None:
        step = self._get(key)
        step.status = status
        if status == "done" and step.total:
            step.done = step.total

    def add_count(self, key: str, n: int) -> None:
        self.counts[key] = (self.counts[key] if key in self.counts else 0) + n

    def eta_seconds(self) -> int:
        if self.status != "running":
            return 0
        now = time.time()
        eta = 0.0
        for step in self.steps:
            if step.status == "pending":
                eta += step.estimate_s
            elif step.status == "running":
                elapsed = now - step.started_at
                progressed = step.done - step.baseline
                if step.total > 0 and progressed > 0:
                    eta += elapsed / progressed * (step.total - step.done)
                else:
                    eta += max(step.estimate_s - elapsed, 0.0)
        return int(eta + 0.999)

    def view(self) -> JobView:
        steps: list[JobStep] = [
            {"key": s.key, "status": s.status, "done": s.done, "total": s.total} for s in self.steps
        ]
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "persona": self.persona,
            "status": self.status,
            "steps": steps,
            "eta_seconds": self.eta_seconds(),
            "error": self.error,
            "backup_file": self.backup_file,
            "counts": dict(self.counts),
            "started_at": int(self.started_at),
            "finished_at": int(self.finished_at),
        }


_jobs: dict[str, Job] = {}
_active_job_id: str = ""
_KEEP_FINISHED = 20


def active_job() -> Job | None:
    if _active_job_id and _active_job_id in _jobs:
        return _jobs[_active_job_id]
    return None


def begin_job(kind: JobKind, persona: str, plan: list[tuple[StepKey, float]], job_id: str = "") -> Job | None:
    """登记新任务；已有任务在跑时返回 None。"""
    global _active_job_id
    if active_job() is not None:
        return None
    job = Job(
        job_id=job_id or uuid.uuid4().hex,
        kind=kind,
        persona=persona,
        steps=[_Step(key=key, estimate_s=est) for key, est in plan],
    )
    _jobs[job.job_id] = job
    _active_job_id = job.job_id
    finished = [j for j in _jobs.values() if j.status != "running"]
    for old in sorted(finished, key=lambda j: j.started_at)[:-_KEEP_FINISHED]:
        del _jobs[old.job_id]
    return job


def finish_job(job: Job, status: JobStatus, error: str = "") -> None:
    global _active_job_id
    job.status = status
    job.error = error
    job.finished_at = time.time()
    for step in job.steps:
        if step.status == "running":
            step.status = "failed" if status in ("failed", "resumable") else "done"
    if _active_job_id == job.job_id:
        _active_job_id = ""


def get_job(job_id: str) -> Job | None:
    return _jobs[job_id] if job_id in _jobs else None
