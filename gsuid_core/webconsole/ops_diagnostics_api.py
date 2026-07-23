"""运维诊断 API — 控制台「AI 运维中心」后端。

覆盖：
- WS Bot 实时看板 / 存活 AI Session 注册表
- 续聊窗口 / 多模态摄入健康度 / 记忆生命周期报告
- 意图分类 / 触发路径回放 / OOC+输出归一化试跑
- 工具池运行时拓扑 / 配置快照导入导出 / 插件加载诊断
- AI 黑白名单与安全输出策略便捷读写
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import Depends
from pydantic import Field, BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import AGENT_DEBUG

_OPS = AGENT_DEBUG  # 挂在 Agent 调试分组下，避免再开标签

# 安全 / 输出策略相关 ai_config keys（控制台专用面板）
_SECURITY_OUTPUT_KEYS = [
    "content_guard_enable",
    "output_firewall_enable",
    "output_firewall_extra_terms",
    "memory_sensitive_extra_terms",
    "render_long_markdown_as_image",
    "markdown_image_min_chars",
    "markdown_image_max_width",
    "history_merge_window",
    "follow_up_window",
    "follow_up_max_total",
    "tool_search_recall",
    "tool_extra_pool_max",
    "tool_context_window",
]


def _cfg_get(key: str, default: Any = None) -> Any:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    try:
        return ai_config.get_config(key).data
    except Exception:
        return default


def _cfg_set(key: str, value: Any) -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    try:
        return bool(ai_config.set_config(key, value))
    except Exception:
        return False


# ──────────────────── Bots / Sessions ────────────────────


@app.get("/api/ops/bots", summary="WS Bot 实时看板", tags=_OPS)
async def ops_bots(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.gss import gss

    items: List[Dict[str, Any]] = []
    for ws_bot_id, bot in list(getattr(gss, "active_bot", {}).items()):
        connected = ws_bot_id in getattr(gss, "active_ws", {})
        items.append(
            {
                "ws_bot_id": ws_bot_id,
                "bot_id": getattr(bot, "bot_id", ws_bot_id),
                "bot_self_id": getattr(bot, "bot_self_id", None),
                "connected": connected,
                "has_ws": connected,
            }
        )
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": len(items),
            "connected_count": sum(1 for x in items if x["connected"]),
            "items": items,
            "ts": time.time(),
        },
    }


@app.get("/api/ops/sessions", summary="存活 AI Session 注册表", tags=_OPS)
async def ops_sessions(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    registry = get_ai_session_registry()
    hm = get_history_manager()
    sessions = registry.get_all_ai_sessions()
    # HistoryManager 没有 get_session_meta；用 get_all_sessions_info 按 session_id 查 last_access
    try:
        all_session_info = hm.get_all_sessions_info()
    except Exception:
        all_session_info = {}
    items: List[Dict[str, Any]] = []
    now = time.time()
    for sid, agent in sessions.items():
        last_access = None
        try:
            meta = all_session_info.get(sid)
            if isinstance(meta, dict):
                last_access = meta.get("last_access") or meta.get("last_access_time")
        except Exception:
            pass
        hist_len = 0
        try:
            hist = getattr(agent, "history", None) or getattr(agent, "_history", None)
            if hist is not None:
                hist_len = len(hist)
        except Exception:
            pass
        items.append(
            {
                "session_id": sid,
                "persona_name": getattr(agent, "persona_name", None),
                "create_by": getattr(agent, "create_by", None),
                "history_length": hist_len,
                "model_config_name": getattr(agent, "model_config_name", None),
                "last_access": last_access,
                "idle_seconds": (round(now - float(last_access), 1) if isinstance(last_access, (int, float)) else None),
            }
        )
    items.sort(key=lambda x: x.get("idle_seconds") or 0)
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": len(items),
            "idle_threshold": getattr(registry, "IDLE_THRESHOLD", 1800),
            "max_ai_history": getattr(registry, "MAX_AI_HISTORY_LENGTH", 30),
            "items": items,
            "ts": now,
        },
    }


# ──────────────────── Followup / Multimodal / Lifecycle ────────────────────


@app.get("/api/ops/followup", summary="续聊窗口列表", tags=_OPS)
async def ops_followup(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.ai_core.followup_window import list_active_windows

    window = int(_cfg_get("follow_up_window", 0) or 0)
    ceiling = int(_cfg_get("follow_up_max_total", 0) or 0)
    items = list_active_windows(window, ceiling)
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "window_seconds": window,
            "max_total_seconds": ceiling,
            "active_count": sum(1 for x in items if x.get("active")),
            "items": items,
            "ts": time.time(),
        },
    }


@app.get("/api/ops/multimodal", summary="多模态摄入健康度", tags=_OPS)
async def ops_multimodal(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    try:
        from gsuid_core.ai_core.memory.ingestion.multimodal import get_multimodal_health

        data = get_multimodal_health()
    except Exception as e:
        return {"status": 1, "msg": str(e), "data": None}
    data["ts"] = time.time()
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/ops/lifecycle", summary="记忆生命周期最近报告", tags=_OPS)
async def ops_lifecycle_get(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.ai_core.memory.lifecycle.consolidation_worker import (
        get_last_lifecycle_report,
    )

    report = get_last_lifecycle_report()
    return {"status": 0, "msg": "ok", "data": {"report": report, "ts": time.time()}}


@app.post("/api/ops/lifecycle/run", summary="立即执行记忆生命周期维护", tags=_OPS)
async def ops_lifecycle_run(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.ai_core.memory.lifecycle.consolidation_worker import (
        get_last_lifecycle_report,
        run_lifecycle_maintenance,
    )

    try:
        await run_lifecycle_maintenance()
    except Exception as e:
        return {"status": 1, "msg": f"lifecycle run failed: {e}", "data": None}
    return {
        "status": 0,
        "msg": "ok",
        "data": {"report": get_last_lifecycle_report(), "ts": time.time()},
    }


# ──────────────────── Intent / Trigger / Output ────────────────────


class IntentRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@app.post("/api/ops/intent", summary="意图分类试跑", tags=_OPS)
async def ops_intent(body: IntentRequest, _user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    try:
        from gsuid_core.ai_core.classifier.mode_classifier import classifier_service

        result = await classifier_service.predict_async(body.text.strip())
        return {"status": 0, "msg": "ok", "data": result}
    except Exception as e:
        return {"status": 1, "msg": str(e), "data": None}


class TriggerReplayRequest(BaseModel):
    text: str = Field(..., min_length=0, max_length=2000)
    user_id: str = Field(default="ops_user", max_length=64)
    group_id: Optional[str] = Field(default="ops_group", max_length=64)
    bot_id: str = Field(default="ops_bot", max_length=64)
    bot_self_id: str = Field(default="ops_self", max_length=64)
    is_tome: bool = False
    is_private: bool = False
    at_list: List[str] = Field(default_factory=list)
    persona_name: Optional[str] = None


@app.post("/api/ops/trigger-replay", summary="触发路径回放（干跑）", tags=_OPS)
async def ops_trigger_replay(
    body: TriggerReplayRequest,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """不真正调用 LLM / 不发消息，只复现 handle_event 后半段 AI 分流判定。"""
    steps: List[Dict[str, Any]] = []
    from gsuid_core.ai_core.configs.ai_config import ai_config

    enable_ai = bool(ai_config.get_config("enable").data)
    steps.append({"step": "enable_ai", "pass": enable_ai, "detail": enable_ai})
    if not enable_ai:
        return {
            "status": 0,
            "msg": "ok",
            "data": {"outcome": "blocked", "reason": "ai_disabled", "steps": steps},
        }

    black = list(ai_config.get_config("black_list").data or [])
    white = list(ai_config.get_config("white_list").data or [])
    black = [x for x in set(black) if x]
    white = [x for x in set(white) if x]
    in_black = body.user_id in black or (body.group_id and body.group_id in black)
    steps.append(
        {
            "step": "blacklist",
            "pass": not bool(black and in_black),
            "detail": {"black_list": black, "hit": in_black},
        }
    )
    if black and in_black:
        return {
            "status": 0,
            "msg": "ok",
            "data": {"outcome": "blocked", "reason": "blacklist", "steps": steps},
        }

    in_white = body.user_id in white or (body.group_id and body.group_id in white)
    white_ok = (not white) or in_white
    steps.append(
        {
            "step": "whitelist",
            "pass": white_ok,
            "detail": {"white_list": white, "hit": in_white},
        }
    )
    if not white_ok:
        return {
            "status": 0,
            "msg": "ok",
            "data": {"outcome": "blocked", "reason": "whitelist", "steps": steps},
        }

    from gsuid_core.ai_core.persona.config import persona_config_manager

    # 构造伪 session_id
    if body.is_private or not body.group_id:
        session_id = f"ops:{body.bot_id}:{body.bot_self_id}:private:{body.user_id}"
    else:
        session_id = f"ops:{body.bot_id}:{body.bot_self_id}:group:{body.group_id}"

    persona_name = body.persona_name or persona_config_manager.get_persona_for_session(session_id)
    # get_persona_for_session 可能依赖真实 group——若失败用 global persona
    if persona_name is None:
        try:
            persona_name = persona_config_manager.get_global_persona()
        except Exception:
            persona_name = None
    steps.append(
        {
            "step": "persona_match",
            "pass": persona_name is not None,
            "detail": {"session_id": session_id, "persona_name": persona_name},
        }
    )
    if persona_name is None:
        return {
            "status": 0,
            "msg": "ok",
            "data": {"outcome": "blocked", "reason": "no_persona", "steps": steps},
        }

    persona_config = persona_config_manager.get_config(persona_name)
    ai_mode = persona_config.get_config("ai_mode").data
    keywords = persona_config.get_config("keywords").data or []
    steps.append({"step": "ai_mode", "pass": True, "detail": {"ai_mode": ai_mode, "keywords": keywords}})

    trigger_type = ""
    soft_triggered = False
    should_respond = False
    if "提及应答" in (ai_mode or []):
        should_respond = bool(body.is_tome or body.is_private)
        trigger_type = "mention" if should_respond else ""
        msg_text = body.text or ""
        if not should_respond and keywords:
            if any(kw in msg_text for kw in keywords):
                should_respond = True
                trigger_type = "keyword"
        from gsuid_core.ai_core.followup_window import in_followup_window

        _fw = int(ai_config.get_config("follow_up_window").data or 0)
        _fmax = int(ai_config.get_config("follow_up_max_total").data or 0)
        if not should_respond and _fw and body.group_id and not body.at_list:
            if in_followup_window(session_id, body.user_id, _fw, _fmax):
                should_respond = True
                soft_triggered = True
                trigger_type = "followup"
        steps.append(
            {
                "step": "mention_mode",
                "pass": should_respond,
                "detail": {
                    "should_respond": should_respond,
                    "trigger_type": trigger_type,
                    "soft_triggered": soft_triggered,
                    "is_tome": body.is_tome,
                    "is_private": body.is_private,
                },
            }
        )
    else:
        steps.append(
            {
                "step": "mention_mode",
                "pass": False,
                "detail": "ai_mode 不含「提及应答」——干跑仅覆盖提及应答路径",
            }
        )

    intent = None
    if should_respond and body.text.strip():
        try:
            from gsuid_core.ai_core.classifier.mode_classifier import classifier_service

            intent = await classifier_service.predict_async(body.text.strip())
            steps.append({"step": "intent", "pass": True, "detail": intent})
        except Exception as e:
            steps.append({"step": "intent", "pass": False, "detail": str(e)})

    outcome = "would_enter_ai" if should_respond else "no_response"
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "outcome": outcome,
            "trigger_type": trigger_type,
            "soft_triggered": soft_triggered,
            "persona_name": persona_name,
            "session_id": session_id,
            "intent": intent,
            "steps": steps,
        },
    }


class OutputPreviewRequest(BaseModel):
    text: str = Field(..., min_length=0, max_length=20000)
    user_text: str = Field(default="", max_length=2000)
    tier: str = Field(default="roleplay")


@app.post("/api/ops/output-preview", summary="OOC / 输出归一化试跑", tags=_OPS)
async def ops_output_preview(
    body: OutputPreviewRequest,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    from gsuid_core.ai_core import output_firewall
    from gsuid_core.ai_core.utils import (
        _strip_persona_markdown,
        _strip_resource_handles,
        _normalize_html_linebreaks,
        _strip_tool_call_artifacts,
        _strip_special_control_tokens,
    )

    raw = body.text or ""
    hit = output_firewall.check_ooc(raw, tier=body.tier, user_text=body.user_text or "")
    stages: Dict[str, str] = {"raw": raw}
    t1 = _strip_tool_call_artifacts(raw)
    stages["after_strip_tool_call"] = t1
    t2 = _strip_special_control_tokens(t1)
    stages["after_strip_control_tokens"] = t2
    t3 = _strip_resource_handles(t2)
    stages["after_strip_handles"] = t3
    t4 = _normalize_html_linebreaks(t3)
    stages["after_normalize_br"] = t4
    t5 = _strip_persona_markdown(t4)
    stages["after_strip_markdown"] = t5

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "firewall_enabled": output_firewall.is_enabled(),
            "ooc_hit": (None if hit is None else {"category": hit.category, "matched": list(hit.matched)}),
            "stages": stages,
            "final": t5,
        },
    }


# ──────────────────── Tool topology ────────────────────


@app.get("/api/ops/tool-topology", summary="工具池运行时拓扑", tags=_OPS)
async def ops_tool_topology(
    persona_name: Optional[str] = None,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    from gsuid_core.ai_core.register import get_registered_tools
    from gsuid_core.ai_core.rag.tools import get_main_agent_tools
    from gsuid_core.ai_core.persona.config import persona_config_manager

    core_tools = await get_main_agent_tools()
    by_cat = get_registered_tools()
    category_counts = {k: len(v) for k, v in by_cat.items()}

    tool_packs: List[str] = ["dynamic"]
    tool_names: List[str] = []
    if persona_name:
        try:
            cfg = persona_config_manager.get_config(persona_name)
            tool_packs = list(cfg.get_config("tool_packs").data or ["dynamic"])
            tool_names = list(cfg.get_config("tool_names").data or [])
        except Exception:
            pass

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "persona_name": persona_name,
            "tool_packs": tool_packs,
            "tool_names": tool_names,
            "core_pool": [{"name": t.name, "plugin": getattr(t, "plugin", None)} for t in core_tools],
            "core_pool_size": len(core_tools),
            "category_counts": category_counts,
            "tool_search_recall": _cfg_get("tool_search_recall"),
            "tool_extra_pool_max": _cfg_get("tool_extra_pool_max"),
            "tool_context_window": _cfg_get("tool_context_window"),
            "ts": time.time(),
        },
    }


# ──────────────────── Config snapshot ────────────────────


@app.get("/api/ops/config-snapshot", summary="导出配置快照", tags=_OPS)
async def ops_config_snapshot_export(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.ai_core.persona import list_available_personas
    from gsuid_core.ai_core.memory.config import memory_config
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.configs.ai_config import ai_config

    ai_dump: Dict[str, Any] = {}
    try:
        for k in getattr(ai_config, "config_list", {}) or {}:
            try:
                ai_dump[k] = ai_config.get_config(k).data
            except Exception:
                pass
    except Exception:
        pass

    mem_dump = {
        "observer_enabled": memory_config.observer_enabled,
        "observer_blacklist": list(memory_config.observer_blacklist or []),
        "ingestion_enabled": memory_config.ingestion_enabled,
        "enable_retrieval": memory_config.enable_retrieval,
        "enable_system2": memory_config.enable_system2,
        "idle_flush_seconds": getattr(memory_config, "idle_flush_seconds", None),
        "batch_max_size": memory_config.batch_max_size,
        "batch_interval_seconds": memory_config.batch_interval_seconds,
    }

    personas: Dict[str, Any] = {}
    for name in list_available_personas():
        try:
            personas[name] = persona_config_manager.get_persona_config_dict(name)
        except Exception:
            personas[name] = None

    snapshot = {
        "version": 1,
        "exported_at": time.time(),
        "ai_config": ai_dump,
        "memory_config": mem_dump,
        "personas": personas,
        "security_keys": {k: _cfg_get(k) for k in _SECURITY_OUTPUT_KEYS},
        "access": {
            "black_list": list(_cfg_get("black_list", []) or []),
            "white_list": list(_cfg_get("white_list", []) or []),
        },
    }
    return {"status": 0, "msg": "ok", "data": snapshot}


class ConfigSnapshotImport(BaseModel):
    snapshot: Dict[str, Any]
    apply_ai_config: bool = True
    apply_access: bool = True
    apply_security: bool = True
    apply_memory: bool = False  # 默认不改记忆运行时，避免误伤


@app.post("/api/ops/config-snapshot/import", summary="导入配置快照", tags=_OPS)
async def ops_config_snapshot_import(
    body: ConfigSnapshotImport,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    snap = body.snapshot or {}
    applied: List[str] = []
    skipped: List[str] = []

    if body.apply_ai_config and isinstance(snap.get("ai_config"), dict):
        for k, v in snap["ai_config"].items():
            if _cfg_set(k, v):
                applied.append(f"ai_config.{k}")
            else:
                skipped.append(f"ai_config.{k}")

    if body.apply_security and isinstance(snap.get("security_keys"), dict):
        for k, v in snap["security_keys"].items():
            if _cfg_set(k, v):
                applied.append(f"security.{k}")
            else:
                skipped.append(f"security.{k}")

    if body.apply_access and isinstance(snap.get("access"), dict):
        acc = snap["access"]
        if "black_list" in acc and _cfg_set("black_list", acc["black_list"]):
            applied.append("access.black_list")
        if "white_list" in acc and _cfg_set("white_list", acc["white_list"]):
            applied.append("access.white_list")

    if body.apply_memory and isinstance(snap.get("memory_config"), dict):
        from gsuid_core.ai_core.memory.config import memory_config

        for k, v in snap["memory_config"].items():
            if hasattr(memory_config, k):
                try:
                    setattr(memory_config, k, v)
                    applied.append(f"memory.{k}")
                except Exception:
                    skipped.append(f"memory.{k}")
            else:
                skipped.append(f"memory.{k}")

    return {
        "status": 0,
        "msg": "ok",
        "data": {"applied": applied, "skipped": skipped, "applied_count": len(applied)},
    }


# ──────────────────── Access / Security panels ────────────────────


@app.get("/api/ops/access", summary="AI 黑白名单", tags=_OPS)
async def ops_access_get(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "black_list": list(_cfg_get("black_list", []) or []),
            "white_list": list(_cfg_get("white_list", []) or []),
        },
    }


class AccessUpdate(BaseModel):
    black_list: Optional[List[str]] = None
    white_list: Optional[List[str]] = None


@app.put("/api/ops/access", summary="更新 AI 黑白名单", tags=_OPS)
async def ops_access_put(body: AccessUpdate, _user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    if body.black_list is not None:
        _cfg_set("black_list", [x for x in body.black_list if str(x).strip()])
    if body.white_list is not None:
        _cfg_set("white_list", [x for x in body.white_list if str(x).strip()])
    return await ops_access_get(_user)


@app.get("/api/ops/security-output", summary="安全与输出策略", tags=_OPS)
async def ops_security_get(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    data = {k: _cfg_get(k) for k in _SECURITY_OUTPUT_KEYS}
    return {"status": 0, "msg": "ok", "data": data}


class SecurityOutputUpdate(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


@app.put("/api/ops/security-output", summary="更新安全与输出策略", tags=_OPS)
async def ops_security_put(
    body: SecurityOutputUpdate,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    for k, v in (body.values or {}).items():
        if k in _SECURITY_OUTPUT_KEYS:
            _cfg_set(k, v)
    return await ops_security_get(_user)


# ──────────────────── Plugin diagnostics ────────────────────


@app.get("/api/ops/plugins-diagnostics", summary="插件依赖/加载诊断", tags=_OPS)
async def ops_plugins_diagnostics(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.gss import gss
    from gsuid_core.config import plugin_config_store

    plugins_cfg = {}
    try:
        plugins_cfg = plugin_config_store.get_all() or {}
    except Exception:
        plugins_cfg = {}

    # 已 import 的模块缓存
    module_cache_size = 0
    try:
        from gsuid_core import server as server_mod

        module_cache_size = len(getattr(server_mod, "_module_cache", {}) or {})
    except Exception:
        pass

    items: List[Dict[str, Any]] = []
    for name, cfg in plugins_cfg.items():
        if not isinstance(cfg, dict):
            continue
        enabled = True
        try:
            enabled = bool(cfg.get("enabled", True))
        except Exception:
            pass
        raw_sv = cfg.get("sv")
        sv: Dict[str, Any] = raw_sv if isinstance(raw_sv, dict) else {}
        items.append(
            {
                "name": name,
                "enabled": enabled,
                "sv_count": len(sv),
                "has_config": True,
                "keys": list(cfg.keys())[:20],
            }
        )
    items.sort(key=lambda x: x["name"])

    bots = list(getattr(gss, "active_bot", {}).keys())
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "plugin_count": len(items),
            "module_cache_size": module_cache_size,
            "active_bots": bots,
            "items": items,
            "ts": time.time(),
        },
    }
