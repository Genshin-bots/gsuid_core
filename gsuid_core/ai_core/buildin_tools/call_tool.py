"""零缓存击穿元执行器（call_tool）与紧凑工具参数契约格式化。

主会话 ``tools[]`` 在整条会话内 100% 永冻，不因 ``find_tools`` 动态改写工具数组，
从而彻底避免 KV 前缀缓存从位置 0 失效。长尾工具由 ``find_tools`` 在 ``ToolReturnPart``
中返回紧凑参数契约，再由本工具校验并执行，内层工具独立享有各自的超时、熔断与证据追踪。
"""

from __future__ import annotations

import json
import inspect
from typing import Literal
from dataclasses import replace

from pydantic import JsonValue
from pydantic_ai import ModelRetry, RunContext, ToolReturn
from pydantic_ai.tools import Tool

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.register import ai_tools, find_tool_base, _mark_evidence_error

_FORBIDDEN_DIRECT_NAMES: frozenset[str] = frozenset(
    {"call_tool", "find_tools", "create_subagent", "run_code", "send_message_by_ai"}
)
_CORE_PLUGINS: frozenset[str] = frozenset({"", "core", "gsuid_core", "buildin", "unknown"})
_MAIN_FORBIDDEN_CATEGORIES: frozenset[str] = frozenset({"media", "plugin_dev", "meta"})
_FALLBACK_INNER_TIMEOUT_S: float = 60.0
EPHEMERAL_TOOLS_EXTRA_KEY = "ephemeral_call_tools"


def _resolve_ref(prop: dict[str, object], defs: dict[str, object]) -> dict[str, object]:
    if "$ref" in prop and isinstance(prop["$ref"], str):
        ref = prop["$ref"]
        if ref.startswith("#/$defs/"):
            key = ref[len("#/$defs/") :]
            if key in defs:
                target = defs[key]
                if isinstance(target, dict):
                    return {str(k): v for k, v in target.items()}
    return prop


def _compact_param_type(prop: dict[str, object], defs: dict[str, object] | None = None) -> str:
    defs_map = defs if defs is not None else {}
    resolved = _resolve_ref(prop, defs_map)
    if "enum" in resolved and isinstance(resolved["enum"], list) and resolved["enum"]:
        vals = [str(v) for v in resolved["enum"][:6]]
        return "enum(" + "/".join(vals) + ")"
    if "type" in resolved:
        raw_t = resolved["type"]
        if raw_t == "array":
            items_raw = resolved["items"] if "items" in resolved else None
            if isinstance(items_raw, dict):
                return f"array[{_compact_param_type(items_raw, defs_map)}]"
            return "array"
        if raw_t == "object" and "properties" in resolved and isinstance(resolved["properties"], dict):
            sub_keys = [k for k in resolved["properties"] if isinstance(k, str)][:4]
            if sub_keys:
                return "object{" + ",".join(sub_keys) + "}"
            return "object"
        if isinstance(raw_t, str):
            return raw_t
        if isinstance(raw_t, list):
            parts_t = [str(item) for item in raw_t if isinstance(item, str) and item != "null"]
            if parts_t:
                return "|".join(parts_t)
    if "anyOf" in resolved and isinstance(resolved["anyOf"], list):
        parts: list[str] = []
        for item in resolved["anyOf"]:
            if not isinstance(item, dict):
                continue
            item_res = _resolve_ref(item, defs_map)
            if "type" in item_res and item_res["type"] == "null":
                continue
            t_label = _compact_param_type(item_res, defs_map)
            if t_label and t_label not in parts:
                parts.append(t_label)
        if parts:
            return "|".join(parts)
    return "value"


def format_compact_tool_schema_from_tool(tool: Tool[ToolContext]) -> str:
    """从 pydantic-ai ``Tool`` 生成单行紧凑参数契约（支持注册工具与当轮临时旁白工具）。"""
    schema = tool.function_schema.json_schema
    if not isinstance(schema, dict):
        return "{}"
    defs_raw = schema["$defs"] if "$defs" in schema and isinstance(schema["$defs"], dict) else {}
    props_raw = schema["properties"] if "properties" in schema else None
    req_raw = schema["required"] if "required" in schema else None
    required: set[str] = set()
    if isinstance(req_raw, list):
        for item in req_raw:
            if isinstance(item, str):
                required.add(item)
    if not isinstance(props_raw, dict) or not props_raw:
        return "{}（无参数）"
    fields: list[str] = []
    for pname, pdef in props_raw.items():
        if not isinstance(pname, str) or not isinstance(pdef, dict):
            continue
        ptype = _compact_param_type(pdef, defs_raw)
        flag = "必填" if pname in required else "可选"
        default_note = ""
        if pname not in required and "default" in pdef:
            dval = pdef["default"]
            if isinstance(dval, (str, int, float, bool)) and len(str(dval)) <= 16:
                default_note = f"={dval!r}"
        desc_raw = pdef["description"] if "description" in pdef and isinstance(pdef["description"], str) else ""
        desc = " ".join(desc_raw.split())[:64]
        entry = f'"{pname}": "{ptype}({flag}{default_note})' + (f" - {desc}" if desc else "") + '"'
        fields.append(entry)
    return "{" + ", ".join(fields) + "}"


def format_compact_tool_schema(tb: ToolBase) -> str:
    """将目标工具的 JSON Schema 压成一行高可读参数契约，供 find_tools 回执与报错提示。"""
    return format_compact_tool_schema_from_tool(tb.tool)


def _normalize_tool_output(raw: object) -> str | bytes | ToolReturn:
    if raw is None:
        return "（工具已执行，无文本返回）"
    if isinstance(raw, (str, bytes, ToolReturn)):
        return raw
    return str(raw)


def _parse_call_arguments(
    arguments_json: object,
    tb: ToolBase,
) -> tuple[dict[str, object] | None, str]:
    if arguments_json is None:
        return {}, ""
    if isinstance(arguments_json, dict):
        return dict(arguments_json), ""
    if not isinstance(arguments_json, str):
        schema_hint = format_compact_tool_schema(tb)
        return None, f"⚠️ arguments_json 必须是 JSON 对象或 JSON 字符串（{{...}}）。参数契约：{schema_hint}"
    raw_json = arguments_json.strip() or "{}"
    try:
        parsed_obj = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        schema_hint = format_compact_tool_schema(tb)
        return None, f"⚠️ arguments_json 不是合法 JSON（{exc.msg}）。请按以下参数契约重传：{schema_hint}"
    if not isinstance(parsed_obj, dict):
        schema_hint = format_compact_tool_schema(tb)
        return None, f"⚠️ arguments_json 必须是 JSON 对象（{{...}}）。参数契约：{schema_hint}"
    return dict(parsed_obj), ""


def is_call_tool_allowed(
    tb: ToolBase,
    deps: ToolContext,
    *,
    caller_mode: Literal["call_tool", "run_code"] = "call_tool",
) -> tuple[bool, str]:
    """统一准入校验：对齐 find_tools_rank，区分主人格与能力代理/子代理边界。"""
    from gsuid_core.ai_core.agent_run.support import _capability_exclusive_tool_names
    from gsuid_core.ai_core.control.outbound_detectors import EXPOSED_TOOLS_EXTRA_KEY

    name = tb.name
    if tb.category == "meta":
        return False, f"⚠️ 工具 `{name}` 不允许经 {caller_mode} 直接调用。"

    # 会话显式禁用名单（如 hook drop / 远端搜索互斥）最高优先级，不可被 alias 解锁绕过
    if name in deps.blocked_tool_names:
        return False, f"⚠️ 工具 `{name}` 在当前会话被禁用。"

    scope = deps.tool_scope
    if scope is not None and not scope.is_open and not scope.tool_enabled(name):
        return False, f"⚠️ 当前人格作用域未启用工具 `{name}`。"

    from gsuid_core.ai_core.autonomy.tools import AUTONOMY_CALL_TOOL_GUARD_KEY, autonomy_tool_allowed

    if AUTONOMY_CALL_TOOL_GUARD_KEY in deps.extra and bool(deps.extra[AUTONOMY_CALL_TOOL_GUARD_KEY]):
        if not autonomy_tool_allowed(name):
            return False, f"⚠️ 工具 `{name}` 不在自主行动允许清单内。"
        return True, ""

    unlocked = deps.alias_callable_tools
    if deps.allow_user_outbound:
        # 主人格隔离：禁调能力代理专属、hide_from_main、核心内置 default 沙箱工具及 media/plugin_dev
        is_core_default = tb.category == "default" and tb.plugin in _CORE_PLUGINS
        if (tb.category in _MAIN_FORBIDDEN_CATEGORIES or is_core_default) and name not in unlocked:
            return False, f"⚠️ 工具 `{name}` 不允许在主人格直接调用，请改用 create_subagent 委派对应代理。"
        if tb.hide_from_main and name not in unlocked:
            return False, f"⚠️ 工具 `{name}` 属于内部或能力代理专属工具，请改用 create_subagent 委派。"
        if name in _capability_exclusive_tool_names() and name not in unlocked:
            return False, f"⚠️ 工具 `{name}` 属于能力代理专属工具，请改用 create_subagent 委派对应代理。"
        return True, ""

    # 子代理 / 能力代理边界：run_code 允许调用自身的 plugin_dev；call_tool 仅允许调用已授权集合内工具
    if caller_mode == "call_tool":
        if tb.category == "plugin_dev":
            return False, f"⚠️ 工具 `{name}` 不允许经 call_tool 直接调用。"
        exposed_raw = deps.extra[EXPOSED_TOOLS_EXTRA_KEY] if EXPOSED_TOOLS_EXTRA_KEY in deps.extra else None
        if isinstance(exposed_raw, (list, set, frozenset, tuple)) and exposed_raw:
            allowed_sub = {x for x in exposed_raw if isinstance(x, str)} | deps.dynamic_tool_names | unlocked
            if name not in allowed_sub:
                return False, f"⚠️ 工具 `{name}` 不在当前子代理授权工具清单内。"
    return True, ""


def _clean_uid_arg(raw: str) -> str:
    val = (raw or "").strip()
    val = val[1:-1].strip() if val.startswith("<") and val.endswith(">") else val
    return val[1:].strip() if val.startswith("@") else val


def _bind_caller_deps(
    deps: ToolContext, *, source_seq: int, at_user_id: str = "", act_as_user_id: str = ""
) -> tuple[ToolContext | None, str, str, int, str]:
    """按 source_seq / at_user_id / act_as_user_id 构造隔离的调用上下文，并返回真实请求人 (uid, seq)。"""
    from copy import deepcopy

    from gsuid_core.bot import Bot

    distinct_uids = {str(ev.user_id) for ev in deps.events_by_seq.values() if ev.user_id}
    resolved_seq = source_seq
    if source_seq > 0:
        if source_seq not in deps.events_by_seq:
            valid = ", ".join(f"#{s}" for s in sorted(deps.events_by_seq)) or "无"
            return None, "", "", 0, f"⚠️ 无效的 source_seq=#{source_seq}（当前轮可用序号：{valid}）。"
        base_ev = deps.events_by_seq[source_seq]
    elif len(distinct_uids) > 1:
        valid = ", ".join(f"#{s}({deps.events_by_seq[s].user_id})" for s in sorted(deps.events_by_seq))
        return (
            None,
            "",
            "",
            0,
            f"⚠️ 当前轮含多名群友发言（{valid}），调工具须传 source_seq 指明处理哪条消息，严禁串用身份。",
        )
    else:
        base_ev = deps.ev
        if resolved_seq <= 0 and deps.events_by_seq:
            resolved_seq = next(iter(deps.events_by_seq))

    caller_uid = str(base_ev.user_id or "") if base_ev is not None else ""
    clean_at, clean_act = _clean_uid_arg(at_user_id), _clean_uid_arg(act_as_user_id)
    if clean_act and (base_ev is None or base_ev.user_pm != 0):
        return (
            None,
            "",
            "",
            0,
            "❌ 权限不足：普通群友禁冒用他人身份；代查公开面板请改传 at_user_id，仅主人可传 act_as_user_id。",
        )
    if base_ev is None:
        return deps, clean_at, caller_uid, resolved_seq, ""

    eff_ev = deepcopy(base_ev)
    eff_extra = {k: v for k, v in deps.extra.items() if k != "act_as_user_id"}
    if clean_act:
        eff_ev.user_id, eff_ev.sender, eff_ev.at, eff_ev.at_list = clean_act, {}, None, []
        eff_extra["act_as_user_id"] = clean_act
    if clean_at and clean_at != str(eff_ev.bot_self_id):
        eff_ev.at = clean_at
        eff_ev.at_list = [clean_at, *[u for u in eff_ev.at_list if u != clean_at]]
    eff_bot = Bot(deps.bot.bot, eff_ev) if isinstance(deps.bot, Bot) else deps.bot
    return replace(deps, ev=eff_ev, bot=eff_bot, extra=eff_extra), clean_at, caller_uid, resolved_seq, ""


def bind_run_context_caller(
    ctx: RunContext[ToolContext], source_seq: int = 0, *, neutral_group_fallback: bool = False
) -> tuple[RunContext[ToolContext] | None, str]:
    """供直接核心工具按 source_seq 绑定说话人身份；只读群工具可启用 neutral_group_fallback 防泄漏。"""
    from copy import deepcopy

    deps = ctx.deps
    distinct_uids = {str(ev.user_id) for ev in deps.events_by_seq.values() if ev.user_id}
    if source_seq <= 0 and len(distinct_uids) > 1 and neutral_group_fallback and deps.ev is not None:
        neutral_ev = deepcopy(deps.ev)
        neutral_ev.user_id, neutral_ev.user_pm, neutral_ev.sender = "__group_shared__", 6, {}
        eff_extra = {**deps.extra, "multi_speaker_unscoped": True}
        return replace(ctx, deps=replace(deps, ev=neutral_ev, extra=eff_extra)), ""
    eff_deps, _, _, _, err = _bind_caller_deps(deps, source_seq=source_seq)
    if eff_deps is None:
        return None, err
    return (replace(ctx, deps=eff_deps) if eff_deps is not deps else ctx), ""


@ai_tools(category="meta", timeout=None, track_health=False, raw_passthrough=True, code_callable=False)
async def call_tool(
    ctx: RunContext[ToolContext],
    tool_name: str,
    arguments_json: str | dict[str, JsonValue] = "{}",
    source_seq: int = 0,
    at_user_id: str = "",
    act_as_user_id: str = "",
) -> str | bytes | ToolReturn:
    """按工具名调用经 find_tools 发现的非核心工具（保持前缀缓存，支持多消息轮次与 @他人代查）。

    Args:
        ctx: 工具执行上下文
        tool_name: 目标工具的准确名称（来自 find_tools 返回名单）
        arguments_json: 参数 JSON 对象或 JSON 对象字符串（如 {"query":"关键词"}），无参传 {}
        source_seq: 多人合批轮必填：触发本次工具调用的消息序号（如 122），自动绑定该发言人的真实身份与权限
        at_user_id: 可选，当发言人说「帮 @某人 / 帮某群友查面板/签到」时填入目标群友的用户ID（写入 ev.at）
        act_as_user_id: 仅主人(pm=0)可用：对不支持 @ 代查的本人专属命令（如部分签到），以目标群友用户ID代为执行
    """
    from gsuid_core.ai_core.tool_risk import TOOLS_EXECUTION_GATED_KEY, high_risk_block
    from gsuid_core.ai_core.control.lines import TOOLS_EXECUTION_GATED_DENY
    from gsuid_core.ai_core.buildin_tools.visibility import (
        exit_call_tool_direct_invoke,
        enter_call_tool_direct_invoke,
    )

    name = (tool_name or "").strip()
    if not name:
        _mark_evidence_error()
        return "⚠️ tool_name 不能为空。请先调用 find_tools 获取准确工具名。"
    if name in _FORBIDDEN_DIRECT_NAMES:
        _mark_evidence_error()
        return f"⚠️ 核心工具 `{name}` 已在当前工具列表中，请直接调用，不要通过 call_tool 包装调用。"

    if TOOLS_EXECUTION_GATED_KEY in ctx.deps.extra and bool(ctx.deps.extra[TOOLS_EXECUTION_GATED_KEY]):
        _mark_evidence_error()
        return TOOLS_EXECUTION_GATED_DENY

    eff_deps, clean_at, caller_uid, caller_seq, bind_err = _bind_caller_deps(
        ctx.deps,
        source_seq=source_seq,
        at_user_id=at_user_id,
        act_as_user_id=act_as_user_id,
    )
    if eff_deps is None:
        _mark_evidence_error()
        return bind_err

    denied = high_risk_block(eff_deps.ev, name)
    if denied is not None:
        _mark_evidence_error()
        return denied

    tb = find_tool_base(name)
    if tb is None:
        eph_raw = ctx.deps.extra[EPHEMERAL_TOOLS_EXTRA_KEY] if EPHEMERAL_TOOLS_EXTRA_KEY in ctx.deps.extra else None
        if isinstance(eph_raw, dict) and name in eph_raw and isinstance(eph_raw[name], Tool):
            eph_tool = eph_raw[name]
            tb = ToolBase(
                name=name,
                description=eph_tool.description or "",
                plugin="ephemeral",
                tool=eph_tool,
                category="common",
                check_in_wrapper=False,
            )
    if tb is None:
        _mark_evidence_error()
        return f"⚠️ 未找到工具 `{name}`。请先调用 find_tools 查询准确工具名。"

    allowed, deny_reason = is_call_tool_allowed(tb, eff_deps, caller_mode="call_tool")
    if not allowed:
        _mark_evidence_error()
        return deny_reason

    parsed_obj, parse_err = _parse_call_arguments(arguments_json, tb)
    if parsed_obj is None:
        _mark_evidence_error()
        return parse_err
    if clean_at and tb.category == "by_trigger" and "at_user_id" not in parsed_obj:
        parsed_obj["at_user_id"] = clean_at

    tool = tb.tool
    max_retries = tool.max_retries if tool.max_retries is not None else 1
    sub_ctx = replace(ctx, deps=eff_deps, tool_name=name, retry=0, max_retries=max_retries)

    bypass_tok = enter_call_tool_direct_invoke()
    try:
        tool_def = await tool.prepare_tool_def(sub_ctx)
    finally:
        exit_call_tool_direct_invoke(bypass_tok)

    if not tool_def:
        _mark_evidence_error()
        return f"⚠️ 工具 `{name}` 在当前轮次或权限条件下不可用。"

    from pydantic_core import ValidationError

    try:
        validated = tool.function_schema.validator.validate_python(parsed_obj)
    except ValidationError as exc:
        _mark_evidence_error()
        schema_hint = format_compact_tool_schema(tb)
        err_brief = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])
        return f"⚠️ 调用 `{name}` 参数校验失败（{err_brief}）。正确参数契约：{schema_hint}"

    if not isinstance(validated, dict):
        _mark_evidence_error()
        return f"⚠️ 调用 `{name}` 参数解析异常，请传入 JSON 对象。"

    if tool.args_validator is not None:
        try:
            v_res = tool.args_validator(sub_ctx, validated)
            if inspect.isawaitable(v_res):
                await v_res
        except ModelRetry as exc:
            _mark_evidence_error()
            return f"⚠️ 调用 `{name}` 参数校验未通过：{exc.message}"

    logger.debug(t("log.ai.call_tool_dispatch", name=name))
    from gsuid_core.ai_core.evidence.capture import set_evidence_requester, reset_evidence_requester

    req_tok = set_evidence_requester(caller_uid, caller_seq)
    try:
        if tb.check_in_wrapper:
            result = await tool.function_schema.call(validated, sub_ctx)
            return _normalize_tool_output(result)
        from gsuid_core.ai_core.register import execute_guarded_tool

        inner_timeout = tool_def.timeout if tool_def.timeout is not None else _FALLBACK_INNER_TIMEOUT_S
        return await execute_guarded_tool(
            name,
            sub_ctx,
            lambda: tool.function_schema.call(validated, sub_ctx),
            check_func=tb.check_func,
            timeout=inner_timeout,
            track_health=True,
        )
    except ModelRetry as exc:
        _mark_evidence_error()
        return f"⚠️ 调用 `{name}` 需调整参数后重试：{exc.message}"
    finally:
        reset_evidence_requester(req_tok)
