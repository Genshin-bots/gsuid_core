"""
Scheduler APIs
提供调度器相关的 RESTful APIs
"""

from typing import Dict
from datetime import datetime

from fastapi import Depends, Request

from gsuid_core.aps import scheduler, _get_trigger_description
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


def get_job_description(job) -> str:
    """获取任务的描述信息（从函数的docstring获取）"""
    try:
        func = job.func
        if func and hasattr(func, "__doc__") and func.__doc__:
            # 清理docstring，去除多余空白
            doc = func.__doc__.strip()
            if doc:
                return doc
    except Exception:
        pass
    return ""


def get_trigger_description(job) -> str:
    """获取任务的触发器描述"""
    try:
        return _get_trigger_description(job.trigger)
    except Exception:
        return str(job.trigger)


@app.get("/api/scheduler/jobs")
async def get_scheduler_jobs(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取所有计划任务列表

    返回所有已注册的计划任务信息。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 任务列表，每项包含 id、name、description、next_run_time、trigger、paused
    """
    jobs = []
    if scheduler:
        for job in scheduler.get_jobs():
            # 检查任务是否被暂停
            job_state = scheduler.get_job(job.id)
            if job_state and hasattr(job_state, "next_run_time"):
                # 如果next_run_time为None，可能是被暂停了
                next_run = str(job.next_run_time) if job.next_run_time else None
            else:
                next_run = None

            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "description": get_job_description(job),
                    "next_run_time": next_run,
                    "trigger": str(job.trigger),
                    "trigger_description": get_trigger_description(job),
                    "paused": next_run is None,
                }
            )

    return {"status": 0, "msg": "ok", "data": jobs}


@app.post("/api/scheduler/jobs/{job_id}/run")
async def run_scheduler_job(request: Request, job_id: str, _user: Dict = Depends(require_auth)):
    """
    手动触发计划任务

    立即执行指定任务，忽略其调度周期。

    Args:
        request: FastAPI 请求对象
        job_id: 任务 ID
        _user: 认证用户信息

    Returns:
        status: 0成功，1任务不存在或调度器未启动
        msg: 操作结果信息
    """
    if scheduler:
        job = scheduler.get_job(job_id)
        if job:
            job.modify(next_run_time=datetime.now())
            return {"status": 0, "msg": "任务已触发"}
        return {"status": 1, "msg": "任务不存在"}

    return {"status": 1, "msg": "调度器未启动"}


@app.delete("/api/scheduler/jobs/{job_id}")
async def delete_scheduler_job(request: Request, job_id: str, _user: Dict = Depends(require_auth)):
    """
    删除计划任务

    Args:
        request: FastAPI 请求对象
        job_id: 任务 ID
        _user: 认证用户信息

    Returns:
        status: 0成功，1调度器未启动
        msg: 操作结果信息
    """
    if scheduler:
        scheduler.remove_job(job_id)
        return {"status": 0, "msg": "任务已删除"}

    return {"status": 1, "msg": "调度器未启动"}


@app.post("/api/scheduler/jobs/{job_id}/pause")
async def pause_scheduler_job(request: Request, job_id: str, _user: Dict = Depends(require_auth)):
    """
    暂停计划任务

    Args:
        request: FastAPI 请求对象
        job_id: 任务 ID
        _user: 认证用户信息

    Returns:
        status: 0成功，1任务不存在或调度器未启动
        msg: 操作结果信息
    """
    if scheduler:
        job = scheduler.get_job(job_id)
        if job:
            job.pause()
            return {"status": 0, "msg": "任务已暂停"}
        return {"status": 1, "msg": "任务不存在"}

    return {"status": 1, "msg": "调度器未启动"}


@app.post("/api/scheduler/jobs/{job_id}/resume")
async def resume_scheduler_job(request: Request, job_id: str, _user: Dict = Depends(require_auth)):
    """
    恢复已暂停的计划任务

    Args:
        request: FastAPI 请求对象
        job_id: 任务 ID
        _user: 认证用户信息

    Returns:
        status: 0成功，1任务不存在或调度器未启动
        msg: 操作结果信息
    """
    if scheduler:
        job = scheduler.get_job(job_id)
        if job:
            job.resume()
            return {"status": 0, "msg": "任务已启动"}
        return {"status": 1, "msg": "任务不存在"}

    return {"status": 1, "msg": "调度器未启动"}
