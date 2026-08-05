"""能力代理 / 主人格出图委派相关的纯函数与契约文案。

与 ``gs_agent`` 解耦，便于单测，避免拉 skills 等重依赖。
"""

from __future__ import annotations

from typing import Any

# 主人格：多项数据 → 委派 render_agent（禁止自渲）
POST_TOOL_OUTPUT_CONTRACT = (
    "（系统：本轮已有工具返回。若结果含多项数据点，必须 "
    'create_subagent(agent_profile="render_agent", task=完整事实包) 出图；'
    "禁止主人格自写 HTML / 直调 render_*；禁止台词复述、禁止 <report>。"
    "台词只留一两句角色化引导。）"
)

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
    "写成**一份**高密度竖长 HTML，只调用一次 render_html_to_image；"
    "html/body 须不透明实色底（暗色或浅色成套 token，非写死单一色）；"
    "暗底须浅字；研报级内容禁止压成少字海报。"
    "若已成功出图：停止再调 render_*，只交 1～3 句摘要。"
    "禁止为好看删硬信息；禁止拆多张连渲；禁止只交 HTML 源码；禁止 web 再检索与编造数字。）"
)

POST_TOOL_FAIL_CONTRACT = (
    "（系统：本轮工具返回失败或空结果。禁止用角色懒惰结束本轮。"
    "立刻换路：优先 web_search_tool 再取数；或 find_tools 后换工具。"
    "取到多项数据后 create_subagent(render_agent) 出图。只有换路后仍无果才可角色化短句说明。）"
)

POST_TOOL_FAIL_CONTRACT_CAPABILITY = (
    "（系统：本轮工具返回失败或空结果。禁止只回过程句结束。"
    "立刻换路：换 query / 换工具再取数；仍无果则在事实包里明确写「无检索结果：原因=…」。"
    "禁止 create_subagent / render_html_* / 插件终局直发出图（出图归 render_agent）。）"
)

POST_TOOL_FAIL_CONTRACT_RENDER = (
    "（系统：渲染失败或空结果。精简 HTML 后重试 render_*；仍失败则短摘要说明原因，禁止把长 HTML/数据当交付正文。）"
)

# 与 subagent._main_persona_receipt_hint(image_likely=True) 对齐
RENDER_DONE_RECEIPT_MARK = "图若已由渲染工具下发"


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
    """Kanban/transient 回执是否走「图已下发」口吻（非图 artifact 不得触发）。"""
    return pid == "render_agent" or has_image_art


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
