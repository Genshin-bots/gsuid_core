"""
Scheduler APIs
提供调度器相关的 RESTful APIs
"""

from __future__ import annotations

import asyncio
from typing import Literal, Optional, TypedDict
from datetime import datetime

from fastapi import Query, Depends, Request
from apscheduler.job import Job

from gsuid_core.aps import scheduler, _get_trigger_description
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.webconsole.session_store import SessionRecord

from ._api_tags import SCHEDULER

SchedulerAction = Literal["run", "pause", "resume", "delete"]
SchedulerActionResult = Literal["ok", "missing"]


class SchedulerJobRow(TypedDict):
    id: str
    name: str
    description: str
    next_run_time: str | None
    trigger: str
    trigger_description: str
    paused: bool


class SchedulerJobsMeta(TypedDict):
    """分页与统计信息。

    ``data`` 仍是数组（旧前端直接遍历），所以元数据放在**信封的 meta 上**而不是
    塞进 ``data``——改 ``data`` 的形状会打断旧前端。
    """

    total: int
    count: int
    limit: Optional[int]
    offset: int
    has_more: bool
    counts: dict[str, int]


class SchedulerJobsResponse(TypedDict):
    status: int
    msg: str
    data: list[SchedulerJobRow]
    meta: Optional[SchedulerJobsMeta]


class SchedulerActionResponse(TypedDict):
    status: int
    msg: str


def get_job_description(job: Job) -> str:
    func = job.func
    if func is None:
        return ""
    doc = func.__doc__
    if not isinstance(doc, str):
        return ""
    return doc.strip()


def collect_scheduler_jobs() -> list[SchedulerJobRow]:
    """Snapshot jobs off the event loop. Do not call get_job() per row (re-takes the lock)."""
    rows: list[SchedulerJobRow] = []
    for job in scheduler.get_jobs():
        next_run_dt = job.next_run_time
        next_run = str(next_run_dt) if next_run_dt is not None else None
        name = job.name if isinstance(job.name, str) else str(job.name)
        rows.append(
            {
                "id": str(job.id),
                "name": name,
                "description": get_job_description(job),
                "next_run_time": next_run,
                "trigger": str(job.trigger),
                "trigger_description": _get_trigger_description(job.trigger),
                "paused": next_run is None,
            }
        )
    return rows


def apply_scheduler_action(job_id: str, action: SchedulerAction) -> SchedulerActionResult:
    job = scheduler.get_job(job_id)
    if job is None:
        return "missing"
    if action == "run":
        tz = scheduler.timezone
        now = datetime.now(tz) if tz is not None else datetime.now()
        job.modify(next_run_time=now)
    elif action == "pause":
        job.pause()
    elif action == "resume":
        job.resume()
    else:
        scheduler.remove_job(job_id)
    return "ok"


@app.get("/api/scheduler/jobs", summary="获取任务列表", tags=SCHEDULER)
async def get_scheduler_jobs(
    request: Request,
    _user: SessionRecord = Depends(require_auth),
    search: Optional[str] = Query(None, description="按任务名/描述模糊匹配"),
    limit: Optional[int] = Query(None, ge=1, le=200, description="单页条数；不传=全量"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> SchedulerJobsResponse:
    """
    获取所有计划任务列表

    返回所有已注册的计划任务信息。jobstore 快照在线程池中完成，避免卡住事件循环。

    `search` 过滤、`limit`/`offset` 分页都在服务端做。**不传 `limit`** 时返回全量
    （旧契约不变），此时 `data` 就是完整列表。
    """
    jobs = await asyncio.to_thread(collect_scheduler_jobs)

    keyword = (search or "").strip().lower()
    matched = [
        job
        for job in jobs
        if not keyword or keyword in job["name"].lower() or keyword in (job["description"] or job["name"]).lower()
    ]
    total = len(matched)
    page = matched[offset:] if limit is None else matched[offset : offset + limit]

    # 统计对**全量**任务做，不受搜索/分页影响（与旧前端一致）
    counts = {
        "total": len(jobs),
        "running": sum(1 for job in jobs if not job["paused"]),
        "paused": sum(1 for job in jobs if job["paused"]),
        "error": 0,
    }

    return {
        "status": 0,
        "msg": "ok",
        "data": page,
        "meta": {
            "total": total,
            "count": len(page),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page) < total,
            "counts": counts,
        },
    }


@app.post("/api/scheduler/jobs/{job_id}/run", summary="手动触发任务", tags=SCHEDULER)
async def run_scheduler_job(
    request: Request,
    job_id: str,
    _user: SessionRecord = Depends(require_admin),
) -> SchedulerActionResponse:
    """立即执行指定任务，忽略其调度周期。"""
    result = await asyncio.to_thread(apply_scheduler_action, job_id, "run")
    if result == "ok":
        return {"status": 0, "msg": "任务已触发"}
    return {"status": 1, "msg": "任务不存在"}


@app.delete("/api/scheduler/jobs/{job_id}", summary="删除任务", tags=SCHEDULER)
async def delete_scheduler_job(
    request: Request,
    job_id: str,
    _user: SessionRecord = Depends(require_admin),
) -> SchedulerActionResponse:
    """删除计划任务。"""
    result = await asyncio.to_thread(apply_scheduler_action, job_id, "delete")
    if result == "ok":
        return {"status": 0, "msg": "任务已删除"}
    return {"status": 1, "msg": "任务不存在"}


@app.post("/api/scheduler/jobs/{job_id}/pause", summary="暂停任务", tags=SCHEDULER)
async def pause_scheduler_job(
    request: Request,
    job_id: str,
    _user: SessionRecord = Depends(require_admin),
) -> SchedulerActionResponse:
    """暂停计划任务。"""
    result = await asyncio.to_thread(apply_scheduler_action, job_id, "pause")
    if result == "ok":
        return {"status": 0, "msg": "任务已暂停"}
    return {"status": 1, "msg": "任务不存在"}


@app.post("/api/scheduler/jobs/{job_id}/resume", summary="恢复任务", tags=SCHEDULER)
async def resume_scheduler_job(
    request: Request,
    job_id: str,
    _user: SessionRecord = Depends(require_admin),
) -> SchedulerActionResponse:
    """恢复已暂停的计划任务。"""
    result = await asyncio.to_thread(apply_scheduler_action, job_id, "resume")
    if result == "ok":
        return {"status": 0, "msg": "任务已启动"}
    return {"status": 1, "msg": "任务不存在"}
