"""CNB 表情包市场 WebAPI：远程目录浏览、免 clone 预览代理、单分支安装、更新、卸载与人格挂载。"""

from __future__ import annotations

from typing import TypedDict

from fastapi import Depends
from pydantic import BaseModel
from fastapi.responses import Response, FileResponse

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.meme import store as meme_store
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.ai_core.meme.pack_model import PackStore
from gsuid_core.webconsole.session_store import SessionRecord

from ._api_tags import MEME


class MemeStoreInstallBody(BaseModel):
    persona_names: list[str] = []


class MemeStoreInstallUrlBody(BaseModel):
    url: str
    branch: str | None = None
    pack_name: str | None = None
    persona_names: list[str] = []


class PackPersonasBody(BaseModel):
    persona_names: list[str]


class StoreMemePackItem(TypedDict):
    id: str
    name: str
    series: str
    tags: list[str]
    alias: list[str]
    count: int
    size_kb: int
    repo: str
    branch: str
    commit: str
    has_tags: bool
    cover_url: str
    sheet_url: str
    installed: bool
    local_pack_id: str | None
    local_pack_name: str | None
    bound_personas: list[str]
    hasUpdate: bool
    status: str


class SeriesCountItem(TypedDict):
    name: str
    count: int


def _build_store_item(
    entry: meme_store.CatalogPackEntry,
    installed: dict[str, meme_store.InstalledMemePack],
    bound_by_pack_id: dict[str, list[str]],
) -> StoreMemePackItem:
    inst = installed[entry["id"]] if entry["id"] in installed else None
    is_installed = inst is not None
    local_pack_id = inst["pack_id"] if inst is not None else None
    local_pack_name = inst["pack_name"] if inst is not None else None
    bound = bound_by_pack_id[local_pack_id] if local_pack_id is not None and local_pack_id in bound_by_pack_id else []
    has_update = bool(inst is not None and entry["commit"] and inst["commit"] and entry["commit"] != inst["commit"])
    return StoreMemePackItem(
        id=entry["id"],
        name=entry["name"],
        series=entry["series"],
        tags=entry["tags"],
        alias=entry["alias"],
        count=entry["count"],
        size_kb=entry["size_kb"],
        repo=entry["repo"],
        branch=entry["branch"],
        commit=entry["commit"],
        has_tags=entry["has_tags"],
        cover_url=f"/api/meme-store/asset/cover/{entry['id']}",
        sheet_url=f"/api/meme-store/asset/sheet/{entry['id']}",
        installed=is_installed,
        local_pack_id=local_pack_id,
        local_pack_name=local_pack_name,
        bound_personas=bound,
        hasUpdate=has_update,
        status="installed" if is_installed else "not_installed",
    )


@app.get("/api/meme-store/list", summary="获取 CNB 表情包市场列表", tags=MEME)
async def meme_store_list(
    tab: str = "all",
    series: str = "",
    search: str = "",
    limit: int | None = None,
    offset: int = 0,
    refresh: int = 0,
    check_updates: int = 0,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    force_refresh = bool(refresh or check_updates)
    try:
        catalog = await meme_store.fetch_catalog(force_refresh=force_refresh)
    except Exception as e:
        logger.warning(t("log.meme.store_catalog_fetch_fail", e=e))
        return {"status": 1, "msg": f"获取表情包市场列表失败: {e}", "data": None}

    installed = await meme_store.reconcile_installed()
    summaries = await PackStore.list_summaries()
    bound_by_pack_id = {row.pack_id: list(row.bound_personas) for row in summaries}

    items = [_build_store_item(entry, installed, bound_by_pack_id) for entry in catalog]

    series_counter: dict[str, int] = {}
    for item in items:
        s_name = item["series"]
        if not s_name:
            continue
        series_counter[s_name] = (series_counter[s_name] if s_name in series_counter else 0) + 1
    series_list: list[SeriesCountItem] = [
        SeriesCountItem(name=name, count=cnt)
        for name, cnt in sorted(series_counter.items(), key=lambda pair: (-pair[1], pair[0]))
    ]

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

    series_filter = series.strip()
    if series_filter:
        selected = [p for p in selected if p["series"] == series_filter]

    keyword = search.strip().lower()
    if keyword:
        selected = [
            p
            for p in selected
            if keyword in p["id"].lower()
            or keyword in p["name"].lower()
            or keyword in p["series"].lower()
            or any(keyword in tag.lower() for tag in p["tags"])
            or any(keyword in al.lower() for al in p["alias"])
        ]

    total_count = len(items)
    count = len(selected)
    safe_offset = max(0, offset)
    page_items = selected[safe_offset:] if limit is None else selected[safe_offset : safe_offset + max(1, limit)]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "packs": page_items,
            "series_list": series_list,
            "count": count,
            "total_count": total_count,
            "counts": counts,
            "limit": limit,
            "offset": safe_offset,
            "has_more": safe_offset + len(page_items) < count,
            "repo": meme_store.MEME_STORE_REPO,
        },
    }


@app.get("/api/meme-store/installed", summary="列出通过表情包市场安装的包", tags=MEME)
async def meme_store_installed(_user: SessionRecord = Depends(require_auth)) -> dict[str, object]:
    installed = await meme_store.reconcile_installed()
    summaries = await PackStore.list_summaries()
    bound_by_pack_id = {row.pack_id: list(row.bound_personas) for row in summaries}
    data = [
        {
            **item,
            "bound_personas": bound_by_pack_id[item["pack_id"]] if item["pack_id"] in bound_by_pack_id else [],
        }
        for item in installed.values()
    ]
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/meme-store/detail/{store_id}", summary="免克隆获取市场表情包全部预览清单", tags=MEME)
async def meme_store_detail(
    store_id: str,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    detail, error = await meme_store.fetch_pack_detail(store_id)
    if error is not None or detail is None:
        return {"status": 1, "msg": error or "获取详情失败", "data": None}
    return {"status": 0, "msg": "ok", "data": detail}


@app.get("/api/meme-store/asset/cover/{store_id}", summary="代理缓存市场封面图", tags=MEME)
async def meme_store_asset_cover(
    store_id: str,
    _user: SessionRecord = Depends(require_auth),
) -> Response:
    path, mime, error = await meme_store.fetch_preview_asset("cover", store_id)
    if error is not None or path is None:
        return Response(content=error or "not found", status_code=404, media_type="text/plain")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/meme-store/asset/sheet/{store_id}", summary="代理缓存整包预览图", tags=MEME)
async def meme_store_asset_sheet(
    store_id: str,
    _user: SessionRecord = Depends(require_auth),
) -> Response:
    path, mime, error = await meme_store.fetch_preview_asset("sheet", store_id)
    if error is not None or path is None:
        return Response(content=error or "not found", status_code=404, media_type="text/plain")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/meme-store/asset/item/{store_id}/{filename}", summary="代理缓存单张预览表情", tags=MEME)
async def meme_store_asset_item(
    store_id: str,
    filename: str,
    _user: SessionRecord = Depends(require_auth),
) -> Response:
    path, mime, error = await meme_store.fetch_preview_asset("item", store_id, filename)
    if error is not None or path is None:
        return Response(content=error or "not found", status_code=404, media_type="text/plain")
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=604800"})


@app.post("/api/meme-store/install/{store_id}", summary="安装市场中的表情包", tags=MEME)
async def meme_store_install(
    store_id: str,
    body: MemeStoreInstallBody | None = None,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    personas = body.persona_names if body is not None else []
    result, error = await meme_store.install_pack_from_store(store_id, persona_names=personas)
    if error is not None or result is None:
        return {"status": 1, "msg": error or "安装失败", "data": None}
    return {"status": 0, "msg": "ok", "data": result}


@app.post("/api/meme-store/install-url", summary="通过自定义 Git URL 导入表情包", tags=MEME)
async def meme_store_install_url(
    body: MemeStoreInstallUrlBody,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    result, error = await meme_store.install_pack_from_url(
        body.url,
        branch=body.branch,
        pack_name=body.pack_name,
        persona_names=body.persona_names,
    )
    if error is not None or result is None:
        return {"status": 1, "msg": error or "导入失败", "data": None}
    return {"status": 0, "msg": "ok", "data": result}


@app.post("/api/meme-store/update/{store_id}", summary="同步更新市场表情包", tags=MEME)
async def meme_store_update(
    store_id: str,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    result, error = await meme_store.update_store_pack(store_id)
    if error is not None or result is None:
        return {"status": 1, "msg": error or "更新失败", "data": None}
    return {"status": 0, "msg": "ok", "data": result}


@app.delete("/api/meme-store/uninstall/{store_id}", summary="卸载市场表情包", tags=MEME)
async def meme_store_uninstall(
    store_id: str,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, object]:
    result, error = await meme_store.uninstall_store_pack(store_id)
    if error is not None or result is None:
        return {"status": 1, "msg": error or "卸载失败", "data": None}
    return {"status": 0, "msg": "ok", "data": result}


@app.put("/api/meme-store/packs/{pack_id}/personas", summary="批量设置表情包挂载的人格列表", tags=MEME)
async def meme_store_set_pack_personas(
    pack_id: str,
    body: PackPersonasBody,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    result, error = await meme_store.set_pack_personas(pack_id, body.persona_names)
    if error is not None or result is None:
        return {"status": 1, "msg": error or "设置挂载失败", "data": None}
    return {"status": 0, "msg": "ok", "data": result}
