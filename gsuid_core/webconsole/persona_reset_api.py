"""人格「重新开始」（清空记忆）+ 轻量备份与还原 WebConsole API（全部要求管理员权限）。"""

from __future__ import annotations

from typing import Generic, TypeVar, TypedDict

from fastapi import Depends, HTTPException
from pydantic import Field, BaseModel
from fastapi.responses import FileResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin
from gsuid_core.ai_core.persona_reset import service
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.ai_core.persona_reset.types import (
    JobView,
    BackupInfo,
    ResetPreview,
    RestorePreview,
)

from ._api_tags import PERSONA

_T = TypeVar("_T")


class _ApiResp(TypedDict, Generic[_T]):
    status: int
    msg: str
    data: _T | None


class _JobIdData(TypedDict):
    job_id: str


class PersonaResetRequest(BaseModel):
    items: list[str] = Field(default_factory=list)
    confirm_name: str = ""
    backup: bool = False
    confirm_global: bool = False


class PersonaRestoreRequest(BaseModel):
    items: list[str] = Field(default_factory=list)
    confirm_name: str = ""


def _ok(data: _T) -> _ApiResp[_T]:
    return {"status": 0, "msg": "ok", "data": data}


def _err(msg: str) -> _ApiResp[None]:
    return {"status": 1, "msg": msg, "data": None}


@app.get("/api/persona-reset/jobs/{job_id}", summary="查询人格重置/还原任务进度", tags=PERSONA)
async def get_persona_reset_job(
    job_id: str,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[JobView]:
    view = await service.job_view(job_id)
    if view is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    return _ok(view)


@app.post("/api/persona-reset/jobs/{job_id}/resume", summary="继续未完成的向量重建任务", tags=PERSONA)
async def resume_persona_reset_job(
    job_id: str,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[_JobIdData]:
    res = await service.resume_job(job_id)
    if res["error"]:
        return {"status": 1, "msg": res["error"], "data": None}
    return _ok({"job_id": res["job_id"]})


@app.get("/api/persona/{persona_name}/reset/preview", summary="预览人格清空范围与备份耗时", tags=PERSONA)
async def preview_persona_reset(
    persona_name: str,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[ResetPreview]:
    if not service.persona_exists(persona_name):
        return {"status": 1, "msg": "人格不存在", "data": None}
    return _ok(await service.preview_reset(persona_name))


@app.post("/api/persona/{persona_name}/reset", summary="提交人格清空任务", tags=PERSONA)
async def start_persona_reset(
    persona_name: str,
    req: PersonaResetRequest,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[_JobIdData]:
    res = await service.start_reset(persona_name, req.items, req.confirm_name, req.backup, req.confirm_global)
    if res["error"]:
        return {"status": 1, "msg": res["error"], "data": None}
    return _ok({"job_id": res["job_id"]})


@app.get("/api/persona/{persona_name}/reset/active", summary="查询该人格当前进行中或可续跑的任务", tags=PERSONA)
async def get_persona_active_reset_job(
    persona_name: str,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[JobView]:
    if not service.persona_exists(persona_name):
        return {"status": 1, "msg": "人格不存在", "data": None}
    return {"status": 0, "msg": "ok", "data": await service.active_job_view(persona_name)}


@app.get("/api/persona/{persona_name}/backups", summary="列出该人格的记忆备份", tags=PERSONA)
async def list_persona_backups(
    persona_name: str,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[list[BackupInfo]]:
    if not service.persona_exists(persona_name):
        return {"status": 1, "msg": "人格不存在", "data": None}
    return _ok(await service.list_backups(persona_name))


@app.get("/api/persona/{persona_name}/backups/{file_name}/download", summary="下载人格记忆备份", tags=PERSONA)
async def download_persona_backup(
    persona_name: str,
    file_name: str,
    _: SessionRecord = Depends(require_admin),
) -> FileResponse:
    path = service.backup_file_path(persona_name, file_name)
    if path is None:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return FileResponse(path=str(path), filename=path.name, media_type="application/zip")


@app.delete("/api/persona/{persona_name}/backups/{file_name}", summary="删除人格记忆备份", tags=PERSONA)
async def delete_persona_backup(
    persona_name: str,
    file_name: str,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[None]:
    deleted = await service.delete_backup(persona_name, file_name)
    if not deleted:
        return _err("备份文件不存在")
    return {"status": 0, "msg": "ok", "data": None}


@app.get("/api/persona/{persona_name}/backups/{file_name}/restore/preview", summary="预览人格记忆还原", tags=PERSONA)
async def preview_persona_restore(
    persona_name: str,
    file_name: str,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[RestorePreview]:
    if not service.persona_exists(persona_name):
        return {"status": 1, "msg": "人格不存在", "data": None}
    res = await service.preview_restore(persona_name, file_name)
    if isinstance(res, str):
        return {"status": 1, "msg": res, "data": None}
    return _ok(res)


@app.post("/api/persona/{persona_name}/backups/{file_name}/restore", summary="提交人格记忆还原任务", tags=PERSONA)
async def start_persona_restore(
    persona_name: str,
    file_name: str,
    req: PersonaRestoreRequest,
    _: SessionRecord = Depends(require_admin),
) -> _ApiResp[_JobIdData]:
    res = await service.start_restore(persona_name, file_name, req.items, req.confirm_name)
    if res["error"]:
        return {"status": 1, "msg": res["error"], "data": None}
    return _ok({"job_id": res["job_id"]})
