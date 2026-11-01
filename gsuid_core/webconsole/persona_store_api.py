"""人格市场 WebAPI：远程目录浏览 + git 整包导入 / 更新检查 / 卸载。

市场标签只是展示元数据；真正决定能否导入的是仓库内的固定整包结构
（manifest.json + persona.md + 可选资源 + packs/），由 bundle 导入逻辑校验。
"""

from __future__ import annotations

from typing import TypedDict

import httpx
from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.persona import store as persona_store
from gsuid_core.utils.path_safety import is_safe_filename
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.webconsole.session_store import SessionRecord

from ._api_tags import PERSONA


class InstallUrlBody(BaseModel):
    url: str
    branch: str | None = None


class StorePersonaItem(TypedDict):
    id: str
    name: str
    description: str
    link: str
    branch: str
    avatar: str
    cover: str
    type: str
    content: str
    info: str
    installMsg: str
    tags: list[str]
    alias: list[str]
    installed: bool
    persona_name: str | None
    hasUpdate: bool
    status: str


def _str_field(info: dict[str, object], key: str, default: str = "") -> str:
    value = info.get(key)
    return value if isinstance(value, str) else default


def _str_list_field(info: dict[str, object], key: str) -> list[str]:
    value = info.get(key)
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str)]


async def _fetch_remote_personas() -> dict[str, dict[str, object]]:
    """拉取远程人格目录；顶层 personas 非 dict 或条目畸形时降级为空。"""
    async with httpx.AsyncClient() as client:
        response = await client.get(persona_store.PERSONA_STORE_LIB, timeout=15)
        response.raise_for_status()
        raw = response.json()
    personas = raw.get("personas", {}) if isinstance(raw, dict) else {}
    if not isinstance(personas, dict):
        return {}
    return {k: v for k, v in personas.items() if isinstance(v, dict)}


def _persona_item(
    persona_id: str,
    info: dict[str, object],
    installed_by_repo: dict[str, str],
    update_map: dict[str, bool],
) -> StorePersonaItem:
    link = _str_field(info, "link")
    from gsuid_core.utils.plugins_update.git_mirror import _extract_repo_name

    repo_name = _extract_repo_name(link) or persona_id
    persona_name = installed_by_repo.get(repo_name)
    installed = persona_name is not None
    info_text = _str_field(info, "info")
    return StorePersonaItem(
        id=persona_id,
        name=info_text or persona_id,
        description=info_text,
        link=link,
        branch=_str_field(info, "branch", "main"),
        avatar=_str_field(info, "avatar"),
        cover=_str_field(info, "cover"),
        type=_str_field(info, "type", "tip"),
        content=_str_field(info, "content", "普通"),
        info=info_text,
        installMsg=_str_field(info, "installMsg"),
        tags=_str_list_field(info, "tags"),
        alias=_str_list_field(info, "alias"),
        installed=installed,
        persona_name=persona_name,
        hasUpdate=bool(update_map.get(persona_name)) if persona_name else False,
        status="installed" if installed else "not_installed",
    )


@app.get("/api/persona-store/list", summary="获取人格市场列表", tags=PERSONA)
async def persona_store_list(
    tab: str = "all",
    search: str = "",
    limit: int | None = None,
    offset: int = 0,
    check_updates: int = 0,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    try:
        remote = await _fetch_remote_personas()
    except Exception as e:
        logger.warning(t("log.persona.store_fetch_fail", e=e))
        return {"status": 1, "msg": f"获取人格市场列表失败: {e}", "data": None}

    installed = persona_store.installed_git_personas()
    installed_by_repo = {src["repo_name"]: name for name, src in installed.items()}
    update_map = await persona_store.check_all_updates() if check_updates else {}

    items = [_persona_item(persona_id, info, installed_by_repo, update_map) for persona_id, info in remote.items()]

    counts = {
        "all": len(items),
        "installed": sum(1 for p in items if p["installed"]),
        "updates": sum(1 for p in items if p["hasUpdate"]),
    }

    selected = items
    if tab == "installed":
        selected = [p for p in selected if p["installed"]]
    elif tab == "updates":
        selected = [p for p in selected if p["hasUpdate"]]

    keyword = (search or "").strip().lower()
    if keyword:
        selected = [
            p
            for p in selected
            if keyword in p["id"].lower()
            or keyword in p["description"].lower()
            or any(keyword in tag.lower() for tag in p["tags"])
            or any(keyword in alias.lower() for alias in p["alias"])
        ]

    total_count = len(items)
    count = len(selected)
    page_items = selected[offset:] if limit is None else selected[offset : offset + limit]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "personas": page_items,
            "count": count,
            "total_count": total_count,
            "counts": counts,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page_items) < count,
        },
    }


@app.get("/api/persona-store/installed", summary="列出已导入的 git 人格", tags=PERSONA)
async def persona_store_installed(_user: SessionRecord = Depends(require_auth)) -> dict[str, object]:
    installed = persona_store.installed_git_personas()
    data = [
        {
            "persona_name": name,
            "repo_url": src["repo_url"],
            "repo_name": src["repo_name"],
            "branch": src["branch"],
            "commit": src["commit"],
            "packs": src["packs"],
            "installed_at": src["installed_at"],
        }
        for name, src in installed.items()
    ]
    return {"status": 0, "msg": "ok", "data": data}


@app.post("/api/persona-store/install/{persona_id}", summary="安装市场中的人格（白名单）", tags=PERSONA)
async def persona_store_install(
    persona_id: str,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    try:
        remote = await _fetch_remote_personas()
    except Exception as e:
        logger.warning(t("log.persona.store_fetch_fail", e=e))
        return {"status": 1, "msg": f"获取人格市场列表失败: {e}", "data": None}
    info = remote.get(persona_id)
    if info is None:
        return {"status": 1, "msg": "人格市场内不存在该条目", "data": None}
    link = info.get("link")
    if not isinstance(link, str) or not link.strip():
        return {"status": 1, "msg": "该条目缺少仓库地址", "data": None}
    branch = info.get("branch")
    result, error = await persona_store.install_persona_from_git(
        link.strip(), branch=branch if isinstance(branch, str) else None
    )
    if error is not None:
        return {"status": 1, "msg": error, "data": None}
    return {"status": 0, "msg": "ok", "data": result}


@app.post("/api/persona-store/install-url", summary="通过 URL 导入人格整包仓库", tags=PERSONA)
async def persona_store_install_url(
    body: InstallUrlBody,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    result, error = await persona_store.install_persona_from_git(body.url, branch=body.branch)
    if error is not None:
        return {"status": 1, "msg": error, "data": None}
    return {"status": 0, "msg": "ok", "data": result}


@app.post("/api/persona-store/check/{persona_name}", summary="检查人格是否有更新", tags=PERSONA)
async def persona_store_check(
    persona_name: str,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    has_update = await persona_store.check_persona_update(persona_name)
    if has_update is None:
        return {"status": 1, "msg": "无法检查更新（非 git 导入人格或仓库不可用）", "data": None}
    return {"status": 0, "msg": "ok", "data": {"hasUpdate": has_update}}


@app.post("/api/persona-store/update/{persona_name}", summary="同步更新人格", tags=PERSONA)
async def persona_store_update(
    persona_name: str,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    if not is_safe_filename(persona_name):
        return {"status": 1, "msg": "人格名无效", "data": None}
    success, message = await persona_store.update_persona_from_git(persona_name)
    return {"status": 0 if success else 1, "msg": message, "data": None}


@app.delete("/api/persona-store/uninstall/{persona_name}", summary="卸载 git 导入的人格", tags=PERSONA)
async def persona_store_uninstall(
    persona_name: str,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    if not is_safe_filename(persona_name):
        return {"status": 1, "msg": "人格名无效", "data": None}
    success, message = await persona_store.uninstall_git_persona(persona_name)
    return {"status": 0 if success else 1, "msg": message, "data": None}
