"""零缓存击穿元执行器（call_tool）与紧凑工具参数契约格式化。

主会话 ``tools[]`` 在整条会话内 100% 永冻，不因 ``find_tools`` 动态改写工具数组，
从而彻底避免 KV 前缀缓存从位置 0 失效。长尾工具由 ``find_tools`` 在 ``ToolReturnPart``
中返回紧凑参数契约，再由本工具校验并执行，内层工具独立享有各自的超时、熔断与证据追踪。
"""

from __future__ import annotations

import json
import asyncio
import inspect
from typing import Literal
from dataclasses import replace

from pydantic import JsonValue
from pydantic_ai import ModelRetry, RunContext, ToolReturn
from pydantic_ai.tools import Tool

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.register import ai_tools, find_tool_base

_FORBIDDEN_DIRECT_NAMES: frozenset[str] = frozenset(
    {"call_tool", "find_tools", "create_subagent", "run_code", "send_message_by_ai"}
)
_CORE_PLUGINS: frozenset[str] = frozenset({"", "core", "gsuid_core", "buildin", "unknown"})
_MAIN_FORBIDDEN_CATEGORIES: frozenset[str] = frozenset({"media", "plugin_dev", "meta"})
_FALLBACK_INNER_TIMEOUT_S: float = 60.0
EPHEMERAL_TOOLS_EXTRA_KEY = "ephemeral_call_tools"


def _mark_evidence_error() -> None:
    from gsuid_core.ai_core.evidence.capture import current_evidence_mark

    mark = current_evidence_mark()
    if mark is not None:
        mark.outcome_hint = "error"


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


@ai_tools(category="meta", timeout=None, track_health=False, raw_passthrough=True, code_callable=False)
async def call_tool(
    ctx: RunContext[ToolContext],
    tool_name: str,
    arguments_json: str | dict[str, JsonValue] = "{}",
) -> str | bytes | ToolReturn:
    """按工具名调用经 find_tools 发现的非核心工具（不改会话工具表，保持前缀缓存）。

    Args:
        ctx: 工具执行上下文
        tool_name: 目标工具的准确名称（来自 find_tools 返回名单）
        arguments_json: 参数 JSON 对象或 JSON 对象字符串（如 {"query":"关键词"}），无参传 {}
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

    denied = high_risk_block(ctx.deps.ev, name)
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

    allowed, deny_reason = is_call_tool_allowed(tb, ctx.deps, caller_mode="call_tool")
    if not allowed:
        _mark_evidence_error()
        return deny_reason

    parsed_obj, parse_err = _parse_call_arguments(arguments_json, tb)
    if parsed_obj is None:
        _mark_evidence_error()
        return parse_err

    tool = tb.tool
    max_retries = tool.max_retries if tool.max_retries is not None else 1
    sub_ctx = replace(ctx, tool_name=name, retry=0, max_retries=max_retries)

    bypass_tok = enter_call_tool_direct_invoke()
    try:
        tool_def = await tool.prepare_tool_def(sub_ctx)
    finally:
        exit_call_tool_direct_invoke(bypass_tok)

    if not tool_def:
        _mark_evidence_error()
        return f"⚠️ 工具 `{name}` 在当前轮次或权限条件下不可用。"

    # 纵深兜底：非 @ai_tools/MCP 包装的自定义 ToolBase 仍统一执行 check_func
    if tb.check_func is not None and not tb.check_in_wrapper:
        from gsuid_core.ai_core.register import run_tool_check_func

        is_passed, check_msg = await run_tool_check_func(tb.check_func, sub_ctx)
        if not is_passed:
            _mark_evidence_error()
            return check_msg

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

    if not tb.check_in_wrapper:
        from gsuid_core.ai_core.tool_health import is_tool_frozen

        if is_tool_frozen(name):
            _mark_evidence_error()
            return f"⚠️ 工具 `{name}` 近期连续失败已暂时熔断，请稍后再试或换个方式。"

    logger.debug(t("log.ai.call_tool_dispatch", name=name))
    try:
        if tb.check_in_wrapper:
            # @ai_tools / MCP 内层自带独立超时与健康度；未捕获异常直抛给 tool_safety 统一收口
            result = await tool.function_schema.call(validated, sub_ctx)
        else:
            # to_ai / 自定义裸 ToolBase 无内层超时与健康度，由元执行器补齐闭环
            inner_timeout = tool_def.timeout if tool_def.timeout is not None else _FALLBACK_INNER_TIMEOUT_S
            result = await asyncio.wait_for(tool.function_schema.call(validated, sub_ctx), timeout=inner_timeout)
    except ModelRetry as exc:
        _mark_evidence_error()
        return f"⚠️ 调用 `{name}` 需调整参数后重试：{exc.message}"
    except asyncio.TimeoutError:
        _mark_evidence_error()
        from gsuid_core.ai_core.tool_health import record_tool_failure

        record_tool_failure(name, "timeout")
        return f"⚠️ 工具 {name} 执行超时，请稍后重试或换个方式"

    if not tb.check_in_wrapper:
        from gsuid_core.ai_core.tool_health import record_tool_failure, record_tool_success

        if not isinstance(result, (str, ToolReturn, type(None))):
            from gsuid_core.ai_core.utils import handle_tool_result

            out_str = await handle_tool_result(ctx.deps.bot, result)
            if out_str.lstrip().startswith("❌"):
                record_tool_failure(name, "error_prefix")
            else:
                record_tool_success(name)
            return out_str
        norm_out = _normalize_tool_output(result)
        if isinstance(norm_out, str) and norm_out.lstrip().startswith("❌"):
            record_tool_failure(name, "error_prefix")
        else:
            record_tool_success(name)
        return norm_out
    return _normalize_tool_output(result)
