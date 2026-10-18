"""能力代理 / 主人格出图委派相关的纯函数与契约文案。

与 ``gs_agent`` 解耦，便于单测，避免拉 skills 等重依赖。
"""

from __future__ import annotations

import re
import json
from typing import Any
from collections.abc import Mapping

from gsuid_core.ai_core.control.lines import (
    DELEGATION_FANOUT_RULE,
    PENDING_DELEGATION_HOLD,
    POST_MEME_LINE_CONTRACT,
    POST_TOOL_FAIL_CONTRACT,
    POST_TOOL_OUTPUT_CONTRACT,
    TIMELESS_AGGREGATE_CAVEAT,
    WEB_ONLY_STALENESS_CAVEAT,
    PENDING_DELEGATION_OPENING,
    POST_DISPUTE_SILENCE_CONTRACT,
    POST_DELIVERY_SILENCE_CONTRACT,
    POST_TOOL_FAIL_CONTRACT_RENDER,
    POST_TOOL_OUTPUT_CONTRACT_RENDER,
    POST_TOOL_FAIL_CONTRACT_CAPABILITY,
    POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
    POST_TOOL_OUTPUT_CONTRACT_RENDER_NO_EMBED,
    POST_TOOL_OUTPUT_CONTRACT_RENDER_REQUIRED,
)

__all__ = (
    "DELEGATION_FANOUT_RULE",
    "PENDING_DELEGATION_HOLD",
    "PENDING_DELEGATION_OPENING",
    "POST_DELIVERY_SILENCE_CONTRACT",
    "POST_DISPUTE_SILENCE_CONTRACT",
    "POST_MEME_LINE_CONTRACT",
    "POST_TOOL_FAIL_CONTRACT",
    "POST_TOOL_FAIL_CONTRACT_CAPABILITY",
    "POST_TOOL_FAIL_CONTRACT_RENDER",
    "POST_TOOL_OUTPUT_CONTRACT",
    "POST_TOOL_OUTPUT_CONTRACT_CAPABILITY",
    "POST_TOOL_OUTPUT_CONTRACT_RENDER",
    "POST_TOOL_OUTPUT_CONTRACT_RENDER_REQUIRED",
    "TIMELESS_AGGREGATE_CAVEAT",
    "WEB_ONLY_STALENESS_CAVEAT",
)

# 同一次 run 里 deferred 回执已发出。之后再 create_subagent 只会串行补派。
DELEGATION_INFLIGHT_KEY = "delegation_inflight"


def delegation_is_inflight(extra: Mapping[str, object]) -> bool:
    """本 run 已有 deferred 委派。并列派发窗口已关。"""
    flagged = extra[DELEGATION_INFLIGHT_KEY] if DELEGATION_INFLIGHT_KEY in extra else False
    return flagged is True


def _deferred_ack_speech_rule(spoken: str, opening_missed: bool) -> str:
    if spoken:
        return f"你的开场句已经发给用户：「{spoken}」。对用户只输出 <SILENCE>，不要再说一遍"
    if opening_missed:
        return "本轮你还没对用户开口：用当前人格短说一句接下了、要等一会儿"
    return "对用户默认 <SILENCE>"


def format_deferred_subagent_ack(
    *,
    ordinal: int,
    pid: str,
    handle: str,
    spoken: str = "",
    opening_missed: bool = False,
) -> str:
    """生产路径立刻回灌时的回执。告诉模型不要把 ack 当成下一轮派发信号。

    spoken = 工具已代发的开场句；opening_missed = 该开口但没说出去，允许补一句。
    """
    return (
        f"⏳ 子任务后台执行中（将自动回灌）。task#{ordinal} / {pid} / 句柄 {handle}\n"
        f"本 tool_return 不是终局结论。{_deferred_ack_speech_rule(spoken, opening_missed)}"
        "（禁止过程动词、任务编号、句柄、编排词、叙述第二个执行者）。"
        f"{DELEGATION_FANOUT_RULE}\n"
        "完成后自动回灌。用户之后追问进度时，用 find_tools 召回 check_delegation"
        "（句柄只进工具参数，绝不写进给用户看的台词）。"
    )


# send_message_by_ai 成功回执唯一形态（工具协议的一部分，属结构信号非业务词）。
# loop 侧经 tool_return_is_delivery_success() 消费：交付终局置位 + 终局契约分发。
DELIVERY_SUCCESS_MARK = "消息已发送给用户"

# 数据时效契约（方案七）：以「返回体自带结构标记」为凭，不做工具名/业务词特判。
# web 源 + 无 as_of + 无其它成功非 web 返回 → WEB_ONLY_STALENESS_CAVEAT。
FRESH_DATA_MARK = "[as_of="
WEB_SOURCE_MARK = "[source=web"
# 兼容两种时点声明形态：行首标签 [as_of=…] 与 JSON 字段 "as_of": …
_FRESH_MARK_RE = re.compile(r"\[as_of=|\"as_of\"\s*:")

# 路由/装配元返回：有结构信号但不是「实质业务数据」。
# find_tools 的 🔎/🔒/✅ 若被当成 non_web，会污染时效账本，挡住 WEB_ONLY caveat。
_META_TOOL_RETURN_PREFIXES: tuple[str, ...] = (
    "🔎",  # find_tools：未命中但可委派 / 语义兜底
    "🔒",  # find_tools：exclusive 剥离后的委派指引
    "✅ 已加载",  # find_tools：只列了工具名，尚未取数
    "（系统：",  # 框架契约文案误入 ToolReturn 时不计入
)


def tool_return_has_fresh_mark(content: Any) -> bool:
    """ToolReturn 是否自带时点声明（结构化新鲜读数）。"""
    return isinstance(content, str) and _FRESH_MARK_RE.search(content) is not None


def tool_return_has_web_source_mark(content: Any) -> bool:
    """ToolReturn 是否自带 web 滞后来源声明。"""
    return isinstance(content, str) and WEB_SOURCE_MARK in content


def tool_return_is_non_web_data(content: Any) -> bool:
    """成功的非 web **实质数据**：有它则「本轮只有 web」不成立。

    as_of 尚未被各结构化工具普遍落地前，不能把「无 as_of」等同于「无结构化数据」；
    结构化/知识等非 web 成功返回应挡住 WEB_ONLY caveat 的误注入。
    但 find_tools 路由文案（🔎/🔒/已加载）只是装配元信息，不算有数据。
    """
    if not isinstance(content, str):
        return False
    s = content.strip()
    if not s or s in ("[]", "{}", "null", "None", "none"):
        return False
    if tool_return_has_web_source_mark(s):
        return False
    # 与工具层失败文案口径对齐：软失败不算「有数据」
    if s.startswith(("⚠️", "❌")):
        return False
    # 路由/装配元返回：不算实质数据（否则 find_tools→web 路径永远注不进 caveat）
    if s.startswith(_META_TOOL_RETURN_PREFIXES):
        return False
    return True


# 低时效契约：返回内容含气候或历史均值口径时，禁止台词冒充当前实时读数。
_TIMELESS_AGGREGATE_RE = re.compile(
    r"(月均|月度|气候|氣候|常年|历史平均|歷史平均|平均值|多年平均|同期平均|月平均|平均氣溫|平均气温)"
)
# 逐日/逐时读数形态（真表 + 日期列）不算低时效聚合
_DAILY_SERIES_RE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{2}-\d{2}")


def tool_return_is_delivery_success(content: Any) -> bool:
    """ToolReturn 是否为 send_message_by_ai 的成功交付回执。"""
    return isinstance(content, str) and DELIVERY_SUCCESS_MARK in content


def is_timeless_aggregate(content: str) -> bool:
    """返回体是否呈「无当前时点的均值/气候聚合」形态（逐日序列除外）。"""
    body = (content or "").strip()
    if not body or _TIMELESS_AGGREGATE_RE.search(body) is None:
        return False
    return _DAILY_SERIES_RE.search(body) is None


def _count_fact_items(body: str) -> int:
    """事实包条目数（形态计数：表数据行 / 列表项 / 逐行数据行 / 段落）。"""
    lines = [ln for ln in body.splitlines() if ln.strip()]
    table_rows = [ln for ln in lines if "|" in ln and re.fullmatch(r"\|?[\s:|-]+\|?", ln) is None]
    if len(table_rows) >= 2:
        return len(table_rows)
    bullets = [ln for ln in lines if re.match(r"^\s*(?:[-*•]|\d+[.、)])\s+", ln)]
    if bullets:
        return len(bullets)
    # 逐行数据行（含数字的独立行）：天气列表 / 清单形态
    data_lines = [ln for ln in lines if re.search(r"\d", ln)]
    if len(data_lines) >= 3:
        return len(data_lines)
    return len([p for p in re.split(r"\n\s*\n", body) if p.strip()])


def fact_pack_is_multi_point(content: str, *, threshold: int = 3) -> bool:
    """事实包是否多点（≥threshold 条目）——单点结论不该武装出图纠正。"""
    return _count_fact_items((content or "").strip()) >= threshold


# 与 subagent._main_persona_receipt_hint(image_likely=True) 对齐（仅真实图片 artifact）
RENDER_DONE_RECEIPT_MARK = "有真实图片产物时用发送工具把图发出（参数里用句柄，勿写入台词）"


def is_render_capability_agent(
    *,
    capability_node_id: str = "",
    session_id: str = "",
) -> bool:
    """是否 render_agent 会话。

    显式 ``capability_node_id`` 非空时**只信 node_id**（避免被 session 字符串误导）；
    未传 node_id 时才用 session 前缀 ``capagent_render_agent_`` 兜底。
    """
    nid = (capability_node_id or "").strip()
    if nid:
        return nid == "render_agent"
    sid = session_id or ""
    return sid == "capagent_render_agent" or sid.startswith("capagent_render_agent_")


def receipt_image_likely(*, pid: str, has_image_art: bool) -> bool:
    """Kanban/transient 回执是否走「可发图」口吻（必须有 image/* artifact）。"""
    _ = pid  # 保留关键字兼容；是否可发图只看 has_image_art
    return has_image_art


def _agent_profile_from_tool_args(
    args: dict[str, Any] | str | None,
    args_json: str,
) -> str:
    """只取 agent_profile 字段；task 正文里的节点名不算。"""
    blob: dict[str, Any] | None = None
    if isinstance(args, dict):
        blob = args
    else:
        raw = args if isinstance(args, str) and args.strip() else args_json
        if raw and raw.lstrip()[:1] == "{":
            try:
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                blob = parsed
    if blob is None or "agent_profile" not in blob:
        return ""
    profile = blob["agent_profile"]
    if not isinstance(profile, str):
        return ""
    return profile.strip()


def tool_call_targets_render_agent(
    *,
    tool_name: str,
    args: dict[str, Any] | str | None,
    args_json: str = "",
) -> bool:
    """create_subagent 的 agent_profile 是否解析到 render_agent。"""
    if tool_name != "create_subagent":
        return False
    profile = _agent_profile_from_tool_args(args, args_json)
    if not profile:
        return False
    from gsuid_core.ai_core.agent_node import resolve_node

    return resolve_node(profile) == "render_agent"


def inflight_after_create_subagent_return(
    *,
    failed: bool,
    async_ack: bool,
    render_done: bool,
    ack_seen: bool,
    pending_async: bool,
    delegated_render: bool,
    speech_policy: str,
    is_framework: bool,
) -> tuple[bool, bool, str, bool]:
    """ToolReturn 后的在途静默。(pending, delegated, policy, ack_seen)。

    ToolCall 可能已抢先静默；失败且尚未 ack 则回滚，避免整轮哑火。
    """
    if failed and not ack_seen:
        policy = speech_policy
        if speech_policy == "silence_only":
            policy = "framework_deliver" if is_framework else "free"
        return False, False, policy, False
    if render_done:
        return pending_async, True, speech_policy, True
    if async_ack:
        policy = speech_policy if speech_policy == "delivered" else "silence_only"
        return True, delegated_render, policy, True
    return pending_async, delegated_render, speech_policy, ack_seen


def post_tool_contracts_for(
    create_by: str,
    *,
    session_id: str = "",
    capability_node_id: str = "",
) -> tuple[str, str]:
    """主人格推委派 render_agent；render_agent 推自渲；其它能力代理推事实包。"""
    if create_by == "CapabilityAgent":
        if is_render_capability_agent(
            capability_node_id=capability_node_id,
            session_id=session_id,
        ):
            from gsuid_core.ai_core.configs.ai_config import render_embed_images_enabled

            if render_embed_images_enabled():
                return POST_TOOL_OUTPUT_CONTRACT_RENDER, POST_TOOL_FAIL_CONTRACT_RENDER
            return POST_TOOL_OUTPUT_CONTRACT_RENDER_NO_EMBED, POST_TOOL_FAIL_CONTRACT_RENDER
        return POST_TOOL_OUTPUT_CONTRACT_CAPABILITY, POST_TOOL_FAIL_CONTRACT_CAPABILITY
    return POST_TOOL_OUTPUT_CONTRACT, POST_TOOL_FAIL_CONTRACT
