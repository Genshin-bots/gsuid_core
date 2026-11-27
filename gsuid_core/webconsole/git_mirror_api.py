"""
Git Mirror API
提供 Git 镜像源管理的 RESTful API，支持查看、按需探活和修改所有含 origin 的 Git 插件镜像。
"""

from __future__ import annotations

from typing import Literal, TypedDict

from fastapi import Body, Query, Depends, Request

from gsuid_core.plugin_registry import (
    SSH_PREFIX,
    MirrorType,
    MirrorProbeDict,
    MirrorSwitchStatus,
    probe_to_dict,
    plugin_registry,
    resolve_plugin_on_disk,
    is_builtin_mirror_or_proxy,
    normalize_mirror_config_pair,
    validate_mirror_prefix_async,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
from gsuid_core.utils.plugins_update.git_mirror import (
    PluginGitInfo,
    get_available_mirrors,
    get_current_mirror_mode,
    get_all_plugins_git_info,
    set_plugin_mirror_detail,
    get_current_mirror_config,
    set_all_plugins_mirror_detailed,
)

from ._api_tags import GIT_MIRROR


class _GitMirrorInfoData(TypedDict):
    current_mirror: str
    current_mirror_mode: MirrorType
    available_mirrors: list[dict[str, str]]
    plugins: list[PluginGitInfo]


class _GitMirrorInfoResp(TypedDict):
    status: int
    msg: str
    data: _GitMirrorInfoData


class _GitMirrorProbeData(TypedDict):
    probes: dict[str, list[MirrorProbeDict]]


class _GitMirrorProbeResp(TypedDict):
    status: int
    msg: str
    data: _GitMirrorProbeData


class _GitMirrorConfigData(TypedDict):
    current_mirror: str
    current_mirror_mode: Literal["auto", "proxy", "mirror"]


class _GitMirrorConfigResp(TypedDict):
    status: int
    msg: str
    data: _GitMirrorConfigData | None


class _GitMirrorSwitchItemDict(TypedDict):
    name: str
    success: bool
    status: MirrorSwitchStatus
    message: str


class _GitMirrorSetAllSummary(TypedDict):
    total: int
    success_count: int
    skipped_count: int
    fail_count: int
    config_updated: bool


class _GitMirrorSetAllData(TypedDict):
    results: list[_GitMirrorSwitchItemDict]
    summary: _GitMirrorSetAllSummary


class _GitMirrorSetAllResp(TypedDict):
    status: int
    msg: str
    data: _GitMirrorSetAllData | None


class _GitMirrorSetPluginResp(TypedDict):
    status: int
    msg: str
    data: _GitMirrorSwitchItemDict | None


class _GitMirrorAvailableResp(TypedDict):
    status: int
    msg: str
    data: list[dict[str, str]]


def _normalize_mirror_type(raw: str) -> MirrorType:
    if raw in ("auto", "default", "mirror", "proxy", "ssh"):
        return raw
    return "auto"


@app.get("/api/git-mirror/info", summary="获取 Git 镜像信息", tags=GIT_MIRROR)
async def get_git_mirror_info(
    request: Request,
    fresh: bool = Query(default=False, description="是否现扫磁盘刷新本地插件元信息"),
    probe: bool = Query(default=False, description="是否并发探活官方镜像站可用性"),
    _user: SessionRecord = Depends(require_auth),
) -> _GitMirrorInfoResp:
    """获取所有具备 origin 远端的 Git 插件（含 core 本体）的镜像与上游归属信息。"""
    current_mirror = get_current_mirror_config()
    current_mirror_mode = get_current_mirror_mode()
    available_mirrors = get_available_mirrors()
    plugins_info = await get_all_plugins_git_info(fresh=fresh, probe=probe)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "current_mirror": current_mirror,
            "current_mirror_mode": current_mirror_mode,
            "available_mirrors": available_mirrors,
            "plugins": plugins_info,
        },
    }


@app.post("/api/git-mirror/config", summary="原子保存全局 Git 镜像配置", tags=GIT_MIRROR)
async def save_git_mirror_config_api(
    request: Request,
    mirror_prefix: str = Body(..., embed=True),
    mirror_type: str = Body(default="auto", embed=True),
    _user: SessionRecord = Depends(require_admin),
) -> _GitMirrorConfigResp:
    """原子校验并落盘全局 GitMirror 与 GitMirrorMode，不影响已安装插件。"""
    norm_type = _normalize_mirror_type(mirror_type)
    norm_prefix, saved_mode = normalize_mirror_config_pair(mirror_prefix, norm_type)

    if norm_prefix and norm_prefix != SSH_PREFIX:
        ssrf_err = await validate_mirror_prefix_async(norm_prefix)
        if ssrf_err is not None:
            return {"status": 1, "msg": f"镜像地址非法: {ssrf_err}", "data": None}

    core_plugins_config.set_config("GitMirror", norm_prefix)
    core_plugins_config.set_config("GitMirrorMode", saved_mode)
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "current_mirror": norm_prefix,
            "current_mirror_mode": saved_mode,
        },
    }


@app.post("/api/git-mirror/probe", summary="按需探活镜像源可用性", tags=GIT_MIRROR)
async def probe_git_mirrors_api(
    request: Request,
    plugin_name: str | None = Body(default=None, embed=True),
    force: bool = Body(default=False, embed=True),
    _user: SessionRecord = Depends(require_admin),
) -> _GitMirrorProbeResp:
    """并发探测单个或全部官方插件在 CNB / GitCode 镜像源的同步状态。"""
    grouped = await plugin_registry.probe_mirrors(name=plugin_name, force=force)
    probes: dict[str, list[MirrorProbeDict]] = {
        name: [probe_to_dict(item) for item in items] for name, items in grouped.items()
    }
    return {
        "status": 0,
        "msg": "ok",
        "data": {"probes": probes},
    }


@app.post("/api/git-mirror/set-all", summary="批量设置所有插件的镜像源", tags=GIT_MIRROR)
async def set_all_mirror(
    request: Request,
    mirror_prefix: str = Body(..., embed=True),
    mirror_type: str = Body(default="auto", embed=True),
    _user: SessionRecord = Depends(require_admin),
) -> _GitMirrorSetAllResp:
    """批量将适用 Git 插件切换到目标镜像/反代/SSH，切前强制探活，Fork 与原生源自动跳过。"""
    norm_type = _normalize_mirror_type(mirror_type)
    norm_prefix, saved_mode = normalize_mirror_config_pair(mirror_prefix, norm_type)

    if norm_prefix and norm_prefix != SSH_PREFIX:
        ssrf_err = await validate_mirror_prefix_async(norm_prefix)
        if ssrf_err is not None:
            return {"status": 1, "msg": f"镜像地址非法: {ssrf_err}", "data": None}

    items = await set_all_plugins_mirror_detailed(norm_prefix, mirror_type=norm_type)

    results: list[_GitMirrorSwitchItemDict] = []
    success_count = 0
    skipped_count = 0
    fail_count = 0

    for item in items:
        results.append(
            {
                "name": item.name,
                "success": item.success,
                "status": item.status,
                "message": item.message,
            }
        )
        if item.status in ("switched", "unchanged"):
            success_count += 1
        elif item.status == "skipped":
            skipped_count += 1
        else:
            fail_count += 1

    # 恢复默认、已知内置源无失败项或至少一项切换成功才落盘，避免未探活的自定义死链污染全局配置
    is_known_target = not norm_prefix or norm_prefix == SSH_PREFIX or is_builtin_mirror_or_proxy(norm_prefix)
    config_updated = not norm_prefix or success_count > 0 or (fail_count == 0 and is_known_target)
    if config_updated:
        core_plugins_config.set_config("GitMirror", norm_prefix)
        core_plugins_config.set_config("GitMirrorMode", saved_mode)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "results": results,
            "summary": {
                "total": len(results),
                "success_count": success_count,
                "skipped_count": skipped_count,
                "fail_count": fail_count,
                "config_updated": config_updated,
            },
        },
    }


@app.post("/api/git-mirror/set-plugin/{plugin_name}", summary="设置单个插件的镜像源", tags=GIT_MIRROR)
async def set_single_plugin_mirror(
    request: Request,
    plugin_name: str,
    mirror_prefix: str = Body(..., embed=True),
    mirror_type: str = Body(default="auto", embed=True),
    _user: SessionRecord = Depends(require_admin),
) -> _GitMirrorSetPluginResp:
    """设置单个插件的 Git 镜像源：校验路径安全、严格归属与可拉取预检。"""
    norm_type = _normalize_mirror_type(mirror_type)
    norm_prefix, _ = normalize_mirror_config_pair(mirror_prefix, norm_type)

    if norm_prefix and norm_prefix != SSH_PREFIX:
        ssrf_err = await validate_mirror_prefix_async(norm_prefix)
        if ssrf_err is not None:
            return {"status": 1, "msg": f"镜像地址非法: {ssrf_err}", "data": None}

    found = await resolve_plugin_on_disk(plugin_name, include_core=True, include_buildin=False)
    if found is None:
        return {
            "status": 1,
            "msg": f"插件 {plugin_name} 不存在",
            "data": None,
        }

    item = await set_plugin_mirror_detail(
        found.path,
        norm_prefix,
        mirror_type=norm_type,
        allow_skip=False,
        force_probe=True,
    )

    return {
        "status": 0 if item.success else 1,
        "msg": item.message,
        "data": {
            "name": item.name,
            "success": item.success,
            "status": item.status,
            "message": item.message,
        },
    }


@app.get("/api/git-mirror/available", summary="获取可用镜像源列表", tags=GIT_MIRROR)
async def get_available_mirror_list(
    request: Request,
    _user: SessionRecord = Depends(require_auth),
) -> _GitMirrorAvailableResp:
    return {
        "status": 0,
        "msg": "ok",
        "data": get_available_mirrors(),
    }
