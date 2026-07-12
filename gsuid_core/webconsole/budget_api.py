"""AI 预算限制 WebConsole API（前缀 `/api/ai/budget`）。

按 Session（群 / 群内成员 / 私聊用户）配置 Token 预算，支持 5 小时 / 天 / 周三档窗口、
白名单突破、主人豁免，并提供用量排行、超额预演（干跑）、手动放行等运维接口。

数据：规则/白名单/账本在 `ai_core/budget/models.py`；全局策略在
`ai_core/budget/config.py`；判定与记账由 `ai_core/budget/manager.py::budget_manager`。
"""

import time
from typing import Any, Dict, List, Optional

from fastapi import Query, Depends
from pydantic import BaseModel

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.budget.config import COUNT_MODES, budget_config
from gsuid_core.ai_core.budget.models import (
    SCOPE_TYPES,
    WINDOW_KEYS,
    AIBudgetRule,
    AIBudgetWhitelist,
)
from gsuid_core.ai_core.budget.manager import RuleStatus, budget_manager

from ._api_tags import BUDGET

_PERIOD_MODES = ("rolling", "fixed")
_DIMENSIONS = ("group", "user", "member")
# 各窗口默认时长（秒），用量排行/快速求和用
_WINDOW_DEFAULT_SECONDS = {"short": 5 * 3600, "day": 86400, "week": 7 * 86400}


# ============ 请求模型 ============


class CreateRuleRequest(BaseModel):
    name: str = ""
    scope_type: str = "global"
    scope_id: str = ""
    member_id: str = ""
    bot_id: str = ""
    enabled: bool = True
    priority: int = 0
    period_mode: str = "rolling"
    short_window_hours: int = 5
    limit_short: int = 0
    limit_day: int = 0
    limit_week: int = 0
    note: str = ""


class UpdateRuleRequest(BaseModel):
    name: Optional[str] = None
    scope_type: Optional[str] = None
    scope_id: Optional[str] = None
    member_id: Optional[str] = None
    bot_id: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    period_mode: Optional[str] = None
    short_window_hours: Optional[int] = None
    limit_short: Optional[int] = None
    limit_day: Optional[int] = None
    limit_week: Optional[int] = None
    note: Optional[str] = None


class CreateWhitelistRequest(BaseModel):
    user_id: str
    group_id: str = ""
    bot_id: str = ""
    enabled: bool = True
    note: str = ""


class UpdateWhitelistRequest(BaseModel):
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    bot_id: Optional[str] = None
    enabled: Optional[bool] = None
    note: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    enable: Optional[bool] = None
    count_mode: Optional[str] = None
    count_exempt_usage: Optional[bool] = None
    exempt_masters: Optional[bool] = None
    notify_on_block: Optional[bool] = None
    notify_cooldown: Optional[int] = None
    block_message: Optional[str] = None


class CheckRequest(BaseModel):
    user_id: str
    group_id: str = ""
    bot_id: str = ""


class ResetRequest(BaseModel):
    scope_type: str
    scope_id: str = ""
    member_id: str = ""
    bot_id: str = ""
    window: str = ""  # short|day|week 或留空(清全部)


# ============ 序列化辅助 ============


def _rule_to_dict(rule: AIBudgetRule) -> Dict[str, Any]:
    return {
        "id": rule.id,
        "name": rule.name,
        "scope_type": rule.scope_type,
        "scope_id": rule.scope_id,
        "member_id": rule.member_id,
        "bot_id": rule.bot_id,
        "enabled": rule.enabled,
        "priority": rule.priority,
        "period_mode": rule.period_mode,
        "short_window_hours": rule.short_window_hours,
        "limit_short": rule.limit_short,
        "limit_day": rule.limit_day,
        "limit_week": rule.limit_week,
        "note": rule.note,
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


def _whitelist_to_dict(w: AIBudgetWhitelist) -> Dict[str, Any]:
    return {
        "id": w.id,
        "user_id": w.user_id,
        "group_id": w.group_id,
        "bot_id": w.bot_id,
        "enabled": w.enabled,
        "note": w.note,
        "created_at": w.created_at,
    }


def _rule_status_to_dict(status: RuleStatus) -> Dict[str, Any]:
    return {
        "rule_id": status.rule_id,
        "rule_name": status.rule_name,
        "scope_type": status.scope_type,
        "scope_label": status.scope_label,
        "period_mode": status.period_mode,
        "blocked": status.blocked,
        "windows": [
            {
                "window": w.window,
                "window_seconds": w.window_seconds,
                "limit": w.limit,
                "used": w.used,
                "remaining": w.remaining,
                "over": w.over,
                "reset_at": w.reset_at,
            }
            for w in status.windows
        ],
    }


def _config_to_dict() -> Dict[str, Any]:
    return {
        "enable": bool(budget_config.get_config("enable").data),
        "count_mode": str(budget_config.get_config("count_mode").data),
        "count_exempt_usage": bool(budget_config.get_config("count_exempt_usage").data),
        "exempt_masters": bool(budget_config.get_config("exempt_masters").data),
        "notify_on_block": bool(budget_config.get_config("notify_on_block").data),
        "notify_cooldown": int(budget_config.get_config("notify_cooldown").data),
        "block_message": str(budget_config.get_config("block_message").data),
    }


def _validate_rule_fields(
    scope_type: Optional[str],
    scope_id: Optional[str],
    member_id: Optional[str],
    period_mode: Optional[str],
    short_window_hours: Optional[int],
) -> Optional[str]:
    """返回错误信息字符串，合法则返回 None。"""
    if scope_type is not None:
        if scope_type not in SCOPE_TYPES:
            return f"scope_type 非法，应为 {SCOPE_TYPES} 之一"
        if scope_type in ("group", "member", "user") and not (scope_id or "").strip():
            return f"scope_type={scope_type} 时 scope_id 必填"
        if scope_type == "member" and not (member_id or "").strip():
            return "scope_type=member 时 member_id 必填"
    if period_mode is not None and period_mode not in _PERIOD_MODES:
        return f"period_mode 非法，应为 {_PERIOD_MODES} 之一"
    if short_window_hours is not None and not (1 <= short_window_hours <= 168):
        return "short_window_hours 应在 1~168 之间"
    return None


# ============ 全局配置 ============


@app.get("/api/ai/budget/config", summary="获取全局配置", tags=BUDGET)
async def get_budget_config(_user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """获取预算系统全局配置。"""
    return {"status": 0, "msg": "ok", "data": _config_to_dict()}


@app.put("/api/ai/budget/config", summary="更新全局配置", tags=BUDGET)
async def update_budget_config(
    body: ConfigUpdateRequest,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """更新预算系统全局配置（部分更新，带类型/取值校验）。"""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"status": 1, "msg": "未提供任何修改内容", "data": None}
    if "count_mode" in updates and updates["count_mode"] not in COUNT_MODES:
        return {"status": 1, "msg": f"count_mode 非法，应为 {COUNT_MODES} 之一", "data": None}
    if "notify_cooldown" in updates and int(updates["notify_cooldown"]) < 0:
        return {"status": 1, "msg": "notify_cooldown 不能为负", "data": None}

    bool_keys = {"enable", "count_exempt_usage", "exempt_masters", "notify_on_block"}
    for key, value in updates.items():
        if key in bool_keys:
            value = bool(value)
        elif key == "notify_cooldown":
            value = int(value)
        else:
            value = str(value)
        if not budget_config.set_config(key, value):
            return {"status": 1, "msg": f"配置项 {key} 写入失败", "data": None}

    budget_manager.invalidate()
    return {"status": 0, "msg": "配置已更新", "data": _config_to_dict()}


# ============ 规则 ============


@app.get("/api/ai/budget/rules", summary="规则列表", tags=BUDGET)
async def list_rules(
    scope_type: Optional[str] = Query(None, description="按维度筛选 global/group/member/user"),
    enabled: Optional[bool] = Query(None, description="按启用状态筛选"),
    q: Optional[str] = Query(None, description="按名称/对象ID模糊筛选"),
    with_usage: bool = Query(False, description="是否附带每条规则的实时用量状态"),
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """获取预算规则列表，可选附带实时用量。"""
    rules = await AIBudgetRule.get_all_rules()
    if scope_type:
        rules = [r for r in rules if r.scope_type == scope_type]
    if enabled is not None:
        rules = [r for r in rules if r.enabled == enabled]
    if q:
        ql = q.lower()
        rules = [r for r in rules if ql in r.name.lower() or ql in r.scope_id.lower() or ql in r.member_id.lower()]

    data: List[Dict[str, Any]] = []
    for r in rules:
        item = _rule_to_dict(r)
        if with_usage:
            status = await budget_manager.rule_live_status(r, with_reset=True)
            item["usage"] = _rule_status_to_dict(status)
        data.append(item)
    return {"status": 0, "msg": "ok", "data": data}


@app.post("/api/ai/budget/rules", summary="创建规则", tags=BUDGET)
async def create_rule(
    body: CreateRuleRequest,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """创建一条预算规则。"""
    err = _validate_rule_fields(
        body.scope_type, body.scope_id, body.member_id, body.period_mode, body.short_window_hours
    )
    if err:
        return {"status": 1, "msg": err, "data": None}
    if body.limit_short <= 0 and body.limit_day <= 0 and body.limit_week <= 0:
        return {"status": 1, "msg": "至少需设置一个窗口的 Token 上限（>0）", "data": None}

    now = int(time.time())
    rule_id = await AIBudgetRule.create(
        name=body.name or budget_manager.scope_label(body.scope_type, body.scope_id, body.member_id),
        scope_type=body.scope_type,
        scope_id=body.scope_id.strip(),
        member_id=body.member_id.strip(),
        bot_id=body.bot_id.strip(),
        enabled=body.enabled,
        priority=body.priority,
        period_mode=body.period_mode,
        short_window_hours=body.short_window_hours,
        limit_short=max(0, body.limit_short),
        limit_day=max(0, body.limit_day),
        limit_week=max(0, body.limit_week),
        note=body.note,
        created_at=now,
        updated_at=now,
    )
    budget_manager.invalidate()
    rule = await AIBudgetRule.get_rule(rule_id)
    return {"status": 0, "msg": "规则已创建", "data": _rule_to_dict(rule) if rule else {"id": rule_id}}


@app.get("/api/ai/budget/rules/{rule_id}", summary="规则详情（含实时用量）", tags=BUDGET)
async def get_rule(
    rule_id: int,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """获取单条规则详情（含实时用量状态）。"""
    rule = await AIBudgetRule.get_rule(rule_id)
    if rule is None:
        return {"status": 1, "msg": "规则不存在", "data": None}
    item = _rule_to_dict(rule)
    status = await budget_manager.rule_live_status(rule, with_reset=True)
    item["usage"] = _rule_status_to_dict(status)
    return {"status": 0, "msg": "ok", "data": item}


@app.put("/api/ai/budget/rules/{rule_id}", summary="更新规则", tags=BUDGET)
async def update_rule(
    rule_id: int,
    body: UpdateRuleRequest,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """更新一条规则（部分字段）。"""
    rule = await AIBudgetRule.get_rule(rule_id)
    if rule is None:
        return {"status": 1, "msg": "规则不存在", "data": None}

    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"status": 1, "msg": "未提供任何修改内容", "data": None}

    # 用合并后的最终值校验，未提供的字段沿用原值
    err = _validate_rule_fields(
        updates.get("scope_type", rule.scope_type),
        updates.get("scope_id", rule.scope_id),
        updates.get("member_id", rule.member_id),
        updates.get("period_mode", rule.period_mode),
        updates.get("short_window_hours", rule.short_window_hours),
    )
    if err:
        return {"status": 1, "msg": err, "data": None}

    for key in ("scope_id", "member_id", "bot_id"):
        if key in updates and isinstance(updates[key], str):
            updates[key] = updates[key].strip()
    for key in ("limit_short", "limit_day", "limit_week"):
        if key in updates:
            updates[key] = max(0, int(updates[key]))
    updates["updated_at"] = int(time.time())

    await AIBudgetRule.update_data_by_data(select_data={"id": rule_id}, update_data=updates)
    budget_manager.invalidate()
    updated = await AIBudgetRule.get_rule(rule_id)
    return {"status": 0, "msg": "规则已更新", "data": _rule_to_dict(updated) if updated else None}


@app.post("/api/ai/budget/rules/{rule_id}/toggle", summary="启用/停用规则（快捷开关）", tags=BUDGET)
async def toggle_rule(
    rule_id: int,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """快速启用/停用一条规则。"""
    rule = await AIBudgetRule.get_rule(rule_id)
    if rule is None:
        return {"status": 1, "msg": "规则不存在", "data": None}
    new_state = not rule.enabled
    await AIBudgetRule.update_data_by_data(
        select_data={"id": rule_id},
        update_data={"enabled": new_state, "updated_at": int(time.time())},
    )
    budget_manager.invalidate()
    return {"status": 0, "msg": "已启用" if new_state else "已停用", "data": {"id": rule_id, "enabled": new_state}}


@app.delete("/api/ai/budget/rules/{rule_id}", summary="删除规则", tags=BUDGET)
async def delete_rule(
    rule_id: int,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """删除一条规则。"""
    rule = await AIBudgetRule.get_rule(rule_id)
    if rule is None:
        return {"status": 1, "msg": "规则不存在", "data": None}
    await AIBudgetRule.delete_row(id=rule_id)
    budget_manager.invalidate()
    return {"status": 0, "msg": "规则已删除", "data": {"id": rule_id}}


# ============ 白名单 ============


@app.get("/api/ai/budget/whitelist", summary="白名单列表", tags=BUDGET)
async def list_whitelist(
    user_id: Optional[str] = Query(None, description="按用户ID筛选"),
    group_id: Optional[str] = Query(None, description="按群号筛选"),
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """获取白名单列表。"""
    entries = await AIBudgetWhitelist.get_all_entries()
    if user_id:
        entries = [e for e in entries if e.user_id == user_id]
    if group_id is not None:
        entries = [e for e in entries if e.group_id == group_id]
    return {"status": 0, "msg": "ok", "data": [_whitelist_to_dict(e) for e in entries]}


@app.post("/api/ai/budget/whitelist", summary="新增白名单", tags=BUDGET)
async def create_whitelist(
    body: CreateWhitelistRequest,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """新增一条白名单（user_id 必填；group_id 为空表示全局豁免）。"""
    if not body.user_id.strip():
        return {"status": 1, "msg": "user_id 必填", "data": None}
    entry_id = await AIBudgetWhitelist.create(
        user_id=body.user_id.strip(),
        group_id=body.group_id.strip(),
        bot_id=body.bot_id.strip(),
        enabled=body.enabled,
        note=body.note,
        created_at=int(time.time()),
    )
    budget_manager.invalidate()
    entry = await AIBudgetWhitelist.get_entry(entry_id)
    return {"status": 0, "msg": "白名单已添加", "data": _whitelist_to_dict(entry) if entry else {"id": entry_id}}


@app.put("/api/ai/budget/whitelist/{entry_id}", summary="更新 / 删除白名单", tags=BUDGET)
async def update_whitelist(
    entry_id: int,
    body: UpdateWhitelistRequest,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """更新一条白名单。"""
    entry = await AIBudgetWhitelist.get_entry(entry_id)
    if entry is None:
        return {"status": 1, "msg": "白名单不存在", "data": None}
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return {"status": 1, "msg": "未提供任何修改内容", "data": None}
    for key in ("user_id", "group_id", "bot_id"):
        if key in updates and isinstance(updates[key], str):
            updates[key] = updates[key].strip()
    await AIBudgetWhitelist.update_data_by_data(select_data={"id": entry_id}, update_data=updates)
    budget_manager.invalidate()
    updated = await AIBudgetWhitelist.get_entry(entry_id)
    return {"status": 0, "msg": "白名单已更新", "data": _whitelist_to_dict(updated) if updated else None}


@app.delete("/api/ai/budget/whitelist/{entry_id}", summary="更新 / 删除白名单", tags=BUDGET)
async def delete_whitelist(
    entry_id: int,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """删除一条白名单。"""
    entry = await AIBudgetWhitelist.get_entry(entry_id)
    if entry is None:
        return {"status": 1, "msg": "白名单不存在", "data": None}
    await AIBudgetWhitelist.delete_row(id=entry_id)
    budget_manager.invalidate()
    return {"status": 0, "msg": "白名单已删除", "data": {"id": entry_id}}


# ============ 用量 / 监控 ============


@app.get("/api/ai/budget/usage", summary="用量排行（Top 消费者）", tags=BUDGET)
async def get_usage_ranking(
    dimension: str = Query("group", description="聚合维度 group/user/member"),
    window: str = Query("day", description="统计窗口 short/day/week"),
    limit: int = Query(20, ge=1, le=200, description="返回条数"),
    bot_id: Optional[str] = Query(None, description="按平台过滤"),
    include_exempt: bool = Query(True, description="是否包含豁免用户的用量"),
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """按维度统计某窗口内的 Top 消费者（用量排行）。"""
    if dimension not in _DIMENSIONS:
        return {"status": 1, "msg": f"dimension 非法，应为 {_DIMENSIONS} 之一", "data": None}
    if window not in WINDOW_KEYS:
        return {"status": 1, "msg": f"window 非法，应为 {WINDOW_KEYS} 之一", "data": None}
    since = int(time.time()) - _WINDOW_DEFAULT_SECONDS[window]
    # 读内存账本（真值源），不查库。
    rows = budget_manager.top_consumers(
        dimension=dimension,
        since=since,
        limit=limit,
        bot_id=bot_id,
        include_exempt=include_exempt,
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": {"dimension": dimension, "window": window, "since_ts": since, "items": rows},
    }


@app.get("/api/ai/budget/usage/scope", summary="查看某 scope 的逐窗口用量", tags=BUDGET)
async def get_scope_usage(
    scope_type: str = Query(..., description="维度 global/group/member/user"),
    scope_id: str = Query("", description="群号或用户号"),
    member_id: str = Query("", description="member 维度的群内用户号"),
    bot_id: str = Query("", description="平台（可选）"),
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """查看某 scope 的逐窗口用量/上限/剩余/恢复时间（含所有适用规则）。"""
    if scope_type not in SCOPE_TYPES:
        return {"status": 1, "msg": f"scope_type 非法，应为 {SCOPE_TYPES} 之一", "data": None}
    # 构造代表性消息以复用 evaluate（拿到该 scope 下所有适用规则的明细）
    if scope_type == "group":
        rep_group, rep_user = scope_id, ""
    elif scope_type == "member":
        rep_group, rep_user = scope_id, member_id
    elif scope_type == "user":
        rep_group, rep_user = "", scope_id
    else:
        rep_group, rep_user = "", ""

    decision = await budget_manager.evaluate(rep_group, rep_user, bot_id, with_reset=True, force_evaluate=True)
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "member_id": member_id,
            "enabled": decision.enabled,
            "exempt": decision.exempt,
            "exempt_reason": decision.exempt_reason,
            "rules": [_rule_status_to_dict(s) for s in decision.rule_statuses],
        },
    }


@app.post("/api/ai/budget/check", summary="干跑预演（诊断「为什么被限」）", tags=BUDGET)
async def dry_run_check(
    body: CheckRequest,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """干跑：预演「某用户在某会话发消息」是否会被预算拦截，返回完整原因明细。

    供前端做「为什么这个人被限流」的诊断 UX——不产生任何用量、不发送消息。
    """
    decision = await budget_manager.evaluate(
        body.group_id, body.user_id, body.bot_id, with_reset=True, force_evaluate=True
    )
    data = decision.to_dict()
    return {"status": 0, "msg": "ok", "data": data}


@app.post("/api/ai/budget/reset", summary="手动放行（清除用量）", tags=BUDGET)
async def reset_scope_usage(
    body: ResetRequest,
    _user: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """手动清除某 scope 的用量流水（立即放行）。window 留空=清全部。"""
    if body.scope_type not in SCOPE_TYPES:
        return {"status": 1, "msg": f"scope_type 非法，应为 {SCOPE_TYPES} 之一", "data": None}
    if body.window and body.window not in WINDOW_KEYS:
        return {"status": 1, "msg": f"window 非法，应为 {WINDOW_KEYS} 之一或留空", "data": None}
    if body.scope_type in ("group", "member", "user") and not body.scope_id.strip():
        return {"status": 1, "msg": f"scope_type={body.scope_type} 时 scope_id 必填", "data": None}
    if body.scope_type == "member" and not body.member_id.strip():
        return {"status": 1, "msg": "scope_type=member 时 member_id 必填", "data": None}

    deleted = await budget_manager.reset_scope(
        scope_type=body.scope_type,
        scope_id=body.scope_id.strip(),
        member_id=body.member_id.strip(),
        bot_id=body.bot_id.strip(),
        window=body.window,
    )
    return {"status": 0, "msg": f"已清除 {deleted} 条用量记录", "data": {"deleted": deleted}}


@app.get("/api/ai/budget/overview", summary="看板汇总", tags=BUDGET)
async def get_overview(_user: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """看板汇总：开关、规则/白名单数、近 24h 总 Token、当前超限规则、Top 消费者。"""
    rules = await AIBudgetRule.get_all_rules()
    whitelist = await AIBudgetWhitelist.get_all_entries()
    enabled_rules = [r for r in rules if r.enabled]

    now = int(time.time())
    since_24h = now - 86400
    # 全部读内存账本（真值源），不查库。
    total_24h = budget_manager.usage_total(since_24h)

    blocked: List[Dict[str, Any]] = []
    for r in enabled_rules:
        status = await budget_manager.rule_live_status(r, with_reset=True)
        if status.blocked:
            blocked.append(_rule_status_to_dict(status))

    top_groups = budget_manager.top_consumers("group", since_24h, limit=5)
    top_users = budget_manager.top_consumers("user", since_24h, limit=5)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "enabled": bool(budget_config.get_config("enable").data),
            "rule_count": len(rules),
            "enabled_rule_count": len(enabled_rules),
            "whitelist_count": len(whitelist),
            "total_tokens_24h": total_24h,
            "blocked_rules": blocked,
            "top_groups_24h": top_groups,
            "top_users_24h": top_users,
        },
    }


logger.info(t("💰 [WebConsole] AI 预算限制 API 已注册 (/api/ai/budget)"))
