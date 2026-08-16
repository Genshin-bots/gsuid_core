"""能力代理 / 主人格出图委派相关的纯函数与契约文案。

与 ``gs_agent`` 解耦，便于单测，避免拉 skills 等重依赖。
"""

from __future__ import annotations

import re
import json
from typing import Any

# 主人格：有工具返回后的软提示。出图/再搜/短答由模型自己选，不锁死下一步。
POST_TOOL_OUTPUT_CONTRACT = (
    "（系统：本轮已有工具返回。"
    "长对照/多日清单适合委派 render_agent 出图；一两句能说清就直接答。"
    "结果不对或不够新，可以换描述再 find_tools，或换 query 再搜。"
    "不要自写 HTML / 直调 render_* / 输出 <report>；"
    "委派出图时不必对用户说话；不要把整表当台词念。）"
)

# 旧名保留给引用方；语义与基础契约相同，不再把出图写成「唯一合法下一步」。
POST_TOOL_OUTPUT_CONTRACT_RENDER_REQUIRED = POST_TOOL_OUTPUT_CONTRACT

# 交付已完成（send_message_by_ai 带台词成功回执）→ 终局：只许 SILENCE。
# 取代 POST_TOOL_OUTPUT_CONTRACT——避免交付成功后契约反而提醒模型「再说一句」。
POST_DELIVERY_SILENCE_CONTRACT = (
    "（系统：你已通过发送工具完成交付，本轮任务到此终结。"
    "只输出 <SILENCE>。禁止再输出任何文字——包括「任务已完成 / 图已发送 / "
    "无需追加发言」这类状态汇报；那是系统日志，不是角色台词。）"
)

# 时效提醒文案保留给测试/调用方；主路径不再往请求里追加（避免和出图提示打架）。
TIMELESS_AGGREGATE_CAVEAT = "（系统：返回体看起来没有当前时点。别说成「现在/此刻」；不够可以换路再查，短答也可以。）"

# send_message_by_ai 成功回执唯一形态（工具协议的一部分，属结构信号非业务词）。
# loop 侧经 tool_return_is_delivery_success() 消费：交付终局置位 + 终局契约分发。
DELIVERY_SUCCESS_MARK = "消息已发送给用户"

# 数据时效契约（方案七）：以「返回体自带结构标记」为凭，不做工具名/业务词特判。
# web 源 + 无 as_of + 无其它成功非 web 返回 → WEB_ONLY_STALENESS_CAVEAT。
FRESH_DATA_MARK = "[as_of="
WEB_SOURCE_MARK = "[source=web"
# 兼容两种时点声明形态：行首标签 [as_of=…] 与 JSON 字段 "as_of": …
_FRESH_MARK_RE = re.compile(r"\[as_of=|\"as_of\"\s*:")

WEB_ONLY_STALENESS_CAVEAT = (
    "（系统：本轮只有网页来源，可能滞后。"
    "报数时带上出处；不够新就换工具或换 query 再查，取不到就如实说。"
    "结构化数据工具（find_tools / 能力代理）往往更靠谱。）"
)

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
    行情/知识等非 web 成功返回应挡住 WEB_ONLY caveat 的误注入。
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


# 时效形态：返回体自带「均值/气候/历史」口径而无当前时点读数（结构判据，域无关）。
# 命中 → 失败/低时效契约分支：台词禁与「现在/此刻」共现，禁冒充实时读数。
# 兼容繁简（气候/氣候、历史/歷史）。
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


# 能力代理（非 render）：只交 Markdown/JSON 事实包；出图归 render_agent
POST_TOOL_OUTPUT_CONTRACT_CAPABILITY = (
    "（系统：本轮已有工具返回。你是能力代理——必须把结果整理成 **Markdown 或 JSON 事实包** "
    "交付主人格（条目/日期/数字/来源/时点/依据），或 artifact_put 持久化。"
    "有搜索/查询结果时**禁止**只回过程句（如「下面再搜」「停止重复」「然后渲染」）。"
    "**禁止** create_subagent / render_html_to_image / render_card / "
    "render_markdown_to_image / 插件终局直发出图工具"
    "（出图由主人格再委派 render_agent）。）"
)

POST_TOOL_OUTPUT_CONTRACT_RENDER = (
    "（系统：本轮已有工具返回。你是 render_agent——"
    "若尚未成功出图：事实包**尽量全文上图**（数字/表/论据/风险/时点勿删），"
    "写成**一份**高密度 HTML（竖/横按内容），只调用一次 render_html_to_image；"
    "html/body 须不透明实色底；**先抽四配方之一**（双栏简报/时间轴脊/对比棚/纸感档案），"
    "禁止连续任务抄同一暗色编号竖卡；"
    "≥3 个可比数值须先 render_chart_spec 嵌 SVG；"
    "多实体对比必须 series（每实体一个 name）+ 图例；有正负含义才 signed；"
    "禁止把身份拍扁进单柱 label，禁止用升/降色区分系列；"
    "字重 330–700（勿写 800/900）；"
    "逻辑宽≤1000，正文≥16px、badge≥13px；"
    "事实包有 https 配图则用 <img src=该URL>（系统自动下载嵌图），禁止纯文字墙顶替已有图；"
    "暗底须浅字、浅底须深字；长文禁止压成少字海报。"
    "出图工具**只登记 artifact / 返回句柄**，禁止对用户会话直发。"
    "若已成功出图：停止再调 render_*，只交 1～3 句摘要 + 图片 res_ 句柄。"
    "禁止为好看删硬信息；禁止拆多张连渲；禁止只交 HTML 源码；禁止 web 再检索与编造数字。）"
)

POST_TOOL_FAIL_CONTRACT = (
    "（系统：本轮工具返回失败或空结果。禁止用角色懒惰结束本轮。"
    "立刻换路：优先 web_search_tool 再取数；或 find_tools 后换工具。"
    "仅当结果已是长结构化内容时再 create_subagent(render_agent) 出图。"
    "只有换路后仍无果才可角色化短句说明。）"
)

POST_TOOL_FAIL_CONTRACT_CAPABILITY = (
    "（系统：本轮工具返回失败或空结果。禁止只回过程句结束。"
    "立刻换路：换 query / 换工具再取数；仍无果则在事实包里明确写「无检索结果：原因=…」。"
    "禁止 create_subagent / render_html_* / 插件终局直发出图（出图归 render_agent）。）"
)

POST_TOOL_FAIL_CONTRACT_RENDER = (
    "（系统：渲染失败或空结果。精简 HTML 后重试 render_*；仍失败则短摘要说明原因，禁止把长 HTML/数据当交付正文。）"
)

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
            return POST_TOOL_OUTPUT_CONTRACT_RENDER, POST_TOOL_FAIL_CONTRACT_RENDER
        return POST_TOOL_OUTPUT_CONTRACT_CAPABILITY, POST_TOOL_FAIL_CONTRACT_CAPABILITY
    return POST_TOOL_OUTPUT_CONTRACT, POST_TOOL_FAIL_CONTRACT
