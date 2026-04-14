"""
AI Scheduled Task APIs

提供 AI 定时任务的 RESTful APIs，允许前端对 AI 创建的定时/循环任务进行增删改查。
"""

from typing import Any, Dict, Optional
from datetime import datetime

from fastapi import Query, Depends
from pydantic import BaseModel

from gsuid_core.aps import scheduler
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

# ============ Request Models ============


class AddTaskRequest(BaseModel):
    """添加任务请求"""

    task_type: str = "once"  # "once" 或 "interval"
    run_time: Optional[str] = None  # 格式 "YYYY-MM-DD HH:MM:SS"，一次性任务使用
    interval_type: Optional[str] = None  # "minutes", "hours", "days"
    interval_value: Optional[int] = None
    task_prompt: str
    max_executions: Optional[int] = 10


class ModifyTaskRequest(BaseModel):
    """修改任务请求"""

    task_prompt: Optional[str] = None
    max_executions: Optional[int] = None


# ============ Response Helpers ============


def task_to_dict(task: AIScheduledTask) -> Dict[str, Any]:
    """将 AIScheduledTask 转换为字典"""
    result = {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "user_id": task.user_id,
        "group_id": task.group_id,
        "bot_id": task.bot_id,
        "bot_self_id": task.bot_self_id,
        "user_type": task.user_type,
        "persona_name": task.persona_name,
        "session_id": task.session_id,
        "task_prompt": task.task_prompt,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "executed_at": task.executed_at.isoformat() if task.executed_at else None,
        "result": task.result,
        "error_message": task.error_message,
    }

    if task.task_type == "once":
        result["trigger_time"] = task.trigger_time.isoformat() if task.trigger_time else None
    else:
        result["interval_seconds"] = task.interval_seconds
        result["max_executions"] = task.max_executions
        result["current_executions"] = task.current_executions
        result["start_time"] = task.start_time.isoformat() if task.start_time else None

    result["next_run_time"] = task.next_run_time.isoformat() if task.next_run_time else None

    return result


# ============ APIs ============


@app.get("/api/ai/scheduled_tasks")
async def get_scheduled_tasks(
    user_id: Optional[str] = Query(None, description="按用户ID筛选"),
    status: Optional[str] = Query(None, description="按状态筛选"),
    task_type: Optional[str] = Query(None, description="按任务类型筛选"),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 AI 定时任务列表

    支持按 user_id、status、task_type 筛选。

    Returns:
        status: 0成功
        data: 任务列表
    """
    # 构建查询条件
    query_tasks = {}

    if user_id:
        query_tasks["user_id"] = user_id
    if status:
        query_tasks["status"] = status
    if task_type:
        query_tasks["task_type"] = task_type

    tasks = await AIScheduledTask.select_rows(**query_tasks)

    return {
        "status": 0,
        "msg": "ok",
        "data": [task_to_dict(t) for t in tasks],
    }


@app.get("/api/ai/scheduled_tasks/{task_id}")
async def get_scheduled_task(
    task_id: str,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定任务的详细信息

    Args:
        task_id: 任务 ID

    Returns:
        status: 0成功，1任务不存在
        data: 任务详情
    """
    tasks = await AIScheduledTask.select_rows(task_id=task_id)

    if not tasks:
        return {"status": 1, "msg": "任务不存在", "data": None}

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    return {
        "status": 0,
        "msg": "ok",
        "data": task_to_dict(task),
    }


@app.post("/api/ai/scheduled_tasks")
async def create_scheduled_task(
    body: AddTaskRequest,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    创建新的 AI 定时任务

    注意：此 API 主要用于前端展示，实际任务创建应通过 AI 的 manage_scheduled_task 工具。

    Args:
        body: 任务创建请求

    Returns:
        status: 0成功，1失败
        data: 新建任务的 ID
    """
    try:
        # 验证参数
        if body.task_type == "once" and not body.run_time:
            return {"status": 1, "msg": "一次性任务需要提供 run_time", "data": None}

        if body.task_type == "interval":
            if not body.interval_type or not body.interval_value:
                return {"status": 1, "msg": "循环任务需要提供 interval_type 和 interval_value", "data": None}

        # 构建任务信息
        task_info = {
            "task_id": f"manual_task_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "bot_id": "webconsole",
            "user_id": "webconsole_user",
            "task_type": body.task_type,
            "task_prompt": body.task_prompt,
            "status": "pending",
            "max_executions": body.max_executions or 10,
        }

        if body.task_type == "once":
            if not body.run_time:
                return {"status": 1, "msg": "一次性任务需要提供 run_time", "data": None}
            try:
                trigger_time = datetime.strptime(body.run_time, "%Y-%m-%d %H:%M:%S")
                task_info["trigger_time"] = trigger_time
                task_info["next_run_time"] = trigger_time
            except ValueError:
                return {"status": 1, "msg": "时间格式错误", "data": None}
        else:
            # 计算间隔秒数
            if not body.interval_type or not body.interval_value:
                return {"status": 1, "msg": "循环任务需要提供 interval_type 和 interval_value", "data": None}
            if body.interval_type == "minutes":
                interval_seconds = body.interval_value * 60
            elif body.interval_type == "hours":
                interval_seconds = body.interval_value * 3600
            elif body.interval_type == "days":
                interval_seconds = body.interval_value * 86400
            else:
                return {"status": 1, "msg": "未知间隔类型", "data": None}

            task_info["interval_seconds"] = interval_seconds
            task_info["current_executions"] = 0
            task_info["start_time"] = datetime.now()
            task_info["next_run_time"] = datetime.now()

        # 存入数据库
        await AIScheduledTask.full_insert_data(**task_info)

        return {
            "status": 0,
            "msg": "任务创建成功",
            "data": {"task_id": task_info["task_id"]},
        }

    except Exception as e:
        return {"status": 1, "msg": f"创建任务失败: {str(e)}", "data": None}


@app.put("/api/ai/scheduled_tasks/{task_id}")
async def modify_scheduled_task(
    task_id: str,
    body: ModifyTaskRequest,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    修改 AI 定时任务

    Args:
        task_id: 任务 ID
        body: 修改内容

    Returns:
        status: 0成功，1任务不存在或修改失败
    """
    tasks = await AIScheduledTask.select_rows(task_id=task_id)

    if not tasks:
        return {"status": 1, "msg": "任务不存在", "data": None}

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    # 构建更新数据
    update_data = {}
    if body.task_prompt is not None:
        update_data["task_prompt"] = body.task_prompt
    if body.max_executions is not None:
        if body.max_executions <= 0 or body.max_executions > 10:
            return {"status": 1, "msg": "max_executions 必须在 1-10 之间", "data": None}
        update_data["max_executions"] = body.max_executions

    if not update_data:
        return {"status": 1, "msg": "未提供任何修改内容", "data": None}

    try:
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data=update_data,
        )
        return {"status": 0, "msg": "任务修改成功", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"修改任务失败: {str(e)}", "data": None}


@app.delete("/api/ai/scheduled_tasks/{task_id}")
async def delete_scheduled_task(
    task_id: str,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除 AI 定时任务

    Args:
        task_id: 任务 ID

    Returns:
        status: 0成功，1任务不存在
    """
    tasks = await AIScheduledTask.select_rows(task_id=task_id)

    if not tasks:
        return {"status": 1, "msg": "任务不存在", "data": None}

    try:
        # 从 APScheduler 移除
        if scheduler.get_job(task_id):
            scheduler.remove_job(task_id)

        # 删除数据库记录（这里用 update 模拟删除，将状态改为 cancelled）
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={"status": "cancelled"},
        )

        return {"status": 0, "msg": "任务已取消", "data": None}

    except Exception as e:
        return {"status": 1, "msg": f"删除任务失败: {str(e)}", "data": None}


@app.post("/api/ai/scheduled_tasks/{task_id}/pause")
async def pause_scheduled_task(
    task_id: str,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    暂停 AI 定时任务（仅循环任务）

    Args:
        task_id: 任务 ID

    Returns:
        status: 0成功，1任务不存在或非循环任务
    """
    tasks = await AIScheduledTask.select_rows(task_id=task_id)

    if not tasks:
        return {"status": 1, "msg": "任务不存在", "data": None}

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.task_type != "interval":
        return {"status": 1, "msg": "只有循环任务可以暂停", "data": None}

    if task.status != "pending":
        return {"status": 1, "msg": f"任务状态为 {task.status}，无法暂停", "data": None}

    try:
        # 从 APScheduler 暂停
        if scheduler.get_job(task_id):
            scheduler.pause_job(task_id)

        # 更新数据库
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={"status": "paused"},
        )

        return {"status": 0, "msg": "任务已暂停", "data": None}

    except Exception as e:
        return {"status": 1, "msg": f"暂停任务失败: {str(e)}", "data": None}


@app.post("/api/ai/scheduled_tasks/{task_id}/resume")
async def resume_scheduled_task(
    task_id: str,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    恢复已暂停的 AI 定时任务

    Args:
        task_id: 任务 ID

    Returns:
        status: 0成功，1任务不存在或非暂停状态
    """
    tasks = await AIScheduledTask.select_rows(task_id=task_id)

    if not tasks:
        return {"status": 1, "msg": "任务不存在", "data": None}

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.status != "paused":
        return {"status": 1, "msg": f"任务状态为 {task.status}，无法恢复", "data": None}

    try:
        # 重新注册到 APScheduler
        if scheduler.get_job(task_id):
            scheduler.remove_job(task_id)

        from gsuid_core.ai_core.scheduled_task.executor import execute_scheduled_task

        scheduler.add_job(
            func=execute_scheduled_task,
            trigger="interval",
            seconds=task.interval_seconds,
            start_date=datetime.now(),
            args=[task_id],
            id=task_id,
            replace_existing=True,
        )

        # 更新数据库
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={
                "status": "pending",
                "next_run_time": datetime.now(),
            },
        )

        return {"status": 0, "msg": "任务已恢复", "data": None}

    except Exception as e:
        return {"status": 1, "msg": f"恢复任务失败: {str(e)}", "data": None}


@app.get("/api/ai/scheduled_tasks/stats/overview")
async def get_scheduled_tasks_stats(
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 AI 定时任务统计概览

    Returns:
        status: 0成功
        data: 统计数据
    """
    all_tasks = await AIScheduledTask.select_rows()

    stats = {
        "total": len(all_tasks),
        "pending": 0,
        "paused": 0,
        "executed": 0,
        "failed": 0,
        "cancelled": 0,
        "interval_count": 0,
        "once_count": 0,
    }

    for task in all_tasks:
        if isinstance(task, AIScheduledTask):
            status = task.status
            task_type = task.task_type
        else:
            status = task.get("status")
            task_type = task.get("task_type")

        if status in stats:
            stats[status] += 1
        if task_type == "interval":
            stats["interval_count"] += 1
        elif task_type == "once":
            stats["once_count"] += 1

    return {
        "status": 0,
        "msg": "ok",
        "data": stats,
    }
