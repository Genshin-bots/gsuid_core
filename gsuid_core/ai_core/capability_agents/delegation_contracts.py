"""能力代理 / 主人格出图委派相关的纯函数与契约文案。

与 ``gs_agent`` 解耦，便于单测，避免拉 skills 等重依赖。
"""

from __future__ import annotations

import re
from typing import Any

# 主人格：长结构化结果 → 委派 render_agent（禁止自渲）；短结论不必出图
POST_TOOL_OUTPUT_CONTRACT = (
    "（系统：本轮已有工具返回。"
    "【工具通道】仅当结果含 markdown 表 / ≥3 段正文 / 多行对比列表 时，"
    'create_subagent(agent_profile="render_agent", task=完整事实包或 res_ 句柄)；'
    "单点结论不要出图；禁止自写 HTML / 直调 render_* / <report>；"
    "委派出图时**不要**对用户说话。"
    "【聊天通道】若尚未说过等待句且任务仍会较久，可补一句「等一下…」；"
    "其余在途 <SILENCE>；发图后至多一句角色口吻；"
    "禁止念节点名/句柄/「让某某出图」；禁止把长数据当台词。）"
)

# 强化版出图契约：已确认本轮工具返回构成多点结构（saw_structured_return）时注入。
# 与基础版的区别：把「出图」从条件句变成**唯一合法下一步**，堵住模型先念长文再被打回的
# 事后纠正循环（2026-08-11 归因：单会话 17/25 次纠正与出图契约相关）。
POST_TOOL_OUTPUT_CONTRACT_RENDER_REQUIRED = (
    "（系统：本轮工具返回已构成多点结构（≥3 条目/表/多段数据）。"
    '**唯一合法下一步**：create_subagent(agent_profile="render_agent", '
    "task=完整事实包或 res_ 句柄) 出图。"
    "禁止先输出任何长正文/标题列表/数据台词——那会被拦截且浪费一轮；"
    "禁止自写 HTML / 直调 render_* / <report>；委派出图时不要对用户说话，"
    "图发出后至多一句角色口吻收尾。）"
)

# 交付已完成（send_message_by_ai 带台词成功回执）→ 终局：只许 SILENCE。
# 取代 POST_TOOL_OUTPUT_CONTRACT——避免交付成功后契约反而提醒模型「再说一句」。
POST_DELIVERY_SILENCE_CONTRACT = (
    "（系统：你已通过发送工具完成交付，本轮任务到此终结。"
    "只输出 <SILENCE>。禁止再输出任何文字——包括「任务已完成 / 图已发送 / "
    "无需追加发言」这类状态汇报；那是系统日志，不是角色台词。）"
)

# 时效提醒：本轮工具返回只有无时点聚合（气候/月均/历史均值）时追加，
# 禁台词冒充实时读数、禁出图当「答案」（4.4）。
TIMELESS_AGGREGATE_CAVEAT = (
    "（系统：本轮工具返回只含「气候 / 月均 / 历史均值」这类无当前时点的聚合数据。"
    "台词禁说成「现在 / 此刻」的读数；用角色口吻说明只是常年大概，"
    "或如实说没翻到实时数；也禁止把它出图当成实时答案。）"
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

WEB_ONLY_STALENESS_CAVEAT = (
    "（系统：本轮只有 web 检索/抓取来源（信息可能滞后数天～数周），"
    "没有任何带 [as_of=…] 时点的结构化实时数据。"
    "涉及价格/点位/数值时：要么不报数，要么明确说出数据来自哪天哪篇资料，"
    "禁止当成「现在/此刻」的实时读数；实时数值请改用结构化数据工具"
    "（find_tools/能力代理）重取，取不到就如实说没查到实时数。）"
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
    "html/body 须不透明实色底；**色板与版式按主题选**（禁止连续任务抄同一暗色模板）；"
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


def tool_call_targets_render_agent(
    *,
    tool_name: str,
    args: dict[str, Any] | str | None,
    args_json: str = "",
) -> bool:
    """create_subagent 的 agent_profile 是否解析到 render_agent。"""
    if tool_name != "create_subagent":
        return False
    raw = args_json or (args if isinstance(args, str) else "")
    if isinstance(raw, str) and "render_agent" in raw.lower():
        return True
    if not isinstance(args, dict) or "agent_profile" not in args:
        return False
    profile = args["agent_profile"]
    if not isinstance(profile, str) or not profile.strip():
        return False
    from gsuid_core.ai_core.agent_node import resolve_node

    return resolve_node(profile) == "render_agent"


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
