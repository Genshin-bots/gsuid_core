"""Run-once 与 GsCoreAIAgent 共享的纯函数 / 常量（无循环依赖）。

测试可继续 ``from gsuid_core.ai_core.gs_agent import ...``（主模块 re-export）。
"""

from __future__ import annotations

import re
from typing import List, Union, Literal, Sequence

from pydantic_ai.messages import UserContent, ToolCallPart, ToolReturnPart

from gsuid_core.ai_core.rag.tools import NON_SEARCHABLE_TOOL_CATEGORIES

# re-export：settle / prepare / gs_agent 与测试共用
from gsuid_core.ai_core.agent_run.speech_policy import (  # noqa: F401
    _WALL_CLOCK_CLOSE as _WALL_CLOCK_NUDGE,
    STATUS_INQUIRY_HINT as _STATUS_INQUIRY_HINT,
    _REPORT_SPEECH_NUDGE,
    _WALL_CLOCK_PIPELINE,
    _RENDER_DELEGATE_NUDGE,
    _STATUS_ZERO_TOOL_NUDGE,
    wall_clock_nudge_for as _wall_clock_nudge_for,
    looks_like_report_speech as _looks_like_report_speech,
)
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    tool_call_targets_render_agent as _tool_call_targets_render_agent_core,
)

# 假完成闸——**结构判据**：动作完成声明 + 本轮零工具调用。声明的识别只用
# 闭类完成动词 + 第一人称施动锚点（语言学范畴，非业务域词表）
_FAKE_DONE_RE = re.compile(
    r"已经?(帮你|给你|为您?)?[^，。！？,不没难]{0,6}?(设置|设好|改|修改|取消|删除|删掉|暂停|调整|安排)"
    r"|(帮你|给你|为你)[^，。！？]{0,6}(设|改|删|取消|暂停|安排|定|订)好了"
    r"|^[^，。！？]{0,4}[，,]?\s*(改成|改到|改为|换成|定在)[^。！？]{0,16}(提醒|叫你|喊你|通知)"
)
# "搞定/弄好"是生活化动词（角色闲聊"我搞定了午饭"合法）——该支须同句出现可工具化名词才算
_FAKE_DONE_TASK_NOUN_RE = re.compile(r"提醒|闹钟|任务|日程|定时|预约|待办|计划|通知|订阅")
_FAKE_DONE_CASUAL_RE = re.compile(r"(我|已经?|帮你|给你)[^，。！？]{0,4}(搞定|办好|弄好|安排上)了")
# 疑问/揣测句排除：向用户提问（"你安排好了吗"）或不确定表述不是完成声明
_FAKE_DONE_QUESTION_RE = re.compile(
    r"[吗嘛么呢？?]|没有?$|不知道|不清楚|不确定|要不要|帮你查|我?查查|应该|大概|可能|好像"
)
# 第三人称转述排除：声明前紧邻 他/她/你/群主… = 转述别人（或用户自己）做完的事，不是自称执行
_FAKE_DONE_THIRD_SUBJ_RE = re.compile(
    r"(他|她|它|人家|你|大家|群主|管理员|老板|客服|官方|系统)\s*(说|讲|表示|好像|应该)?\s*$"
)


def _claims_fake_done(text: str) -> bool:
    """按句判定"动作完成声明"：命中声明、且该句无疑问/揣测语气、且非第三人称转述才算。"""
    for sent in re.split(r"[。！!\n；;]", text):
        if not sent or _FAKE_DONE_QUESTION_RE.search(sent):
            continue
        m = _FAKE_DONE_RE.search(sent)
        if m is None:
            c = _FAKE_DONE_CASUAL_RE.search(sent)
            if c is not None and _FAKE_DONE_TASK_NOUN_RE.search(sent):
                m = c
        if m is not None and not _FAKE_DONE_THIRD_SUBJ_RE.search(sent[: m.start()]):
            return True
    return False


def _append_user_text(message: Union[str, List["UserContent"]], text: str) -> Union[str, List["UserContent"]]:
    """向 user message（str 或 content 列表）尾部追加一段文本（拷贝后追加，不改原对象）。"""
    if isinstance(message, str):
        return message + text
    out = list(message)
    out.append(text)
    return out


# 交互式主 Agent 的 create_by 集合（交互脚手架/墙钟软预算适用范围；TEST=本地评测端点）
_INTERACTIVE_CREATE_BY = ("Chat", "Agent", "TEST", "CapabilityAgent")
# 主会话才折叠 JSON；CapabilityAgent 必须看完整工具返回
_MAIN_PERSONA_CREATE_BY = frozenset({"Chat", "Agent", "Plan"})

# on_trace 轨迹事件类型：模型推理段 / 工具调用（见 GsCoreAIAgent._emit_trace）
TraceKind = Literal["thinking", "tool"]

_FAKE_DONE_NUDGE = (
    "（系统校验：你上一条回复声称已完成某个操作，但本轮没有任何工具调用记录，该声明是编造的。"
    "现在立即调用对应工具真正执行（改/取消既有安排先用列表类工具定位目标）；若确实做不到，"
    "就如实向用户说明「刚才说错了，还没有做」。绝不允许再输出不带工具调用支撑的完成话术。"
    "本校验轮禁止抱怨式闲聊；做不到就角色短句或 <SILENCE>。）"
)

# 结构假完成：被呼叫 + 池内有工具 + 零调用 + 非沉默/非极短寒暄（不解析用户话题词）
_STRUCTURAL_ZERO_TOOL_NUDGE = (
    "（系统校验：本轮你被直接呼叫（或同人省略续聊），且工具池非空，但你没有调用任何工具就结束了。"
    "先判断你刚才的回答是否已经**完整**解决用户：若是纯概念/常识解释且你有把握、"
    "或纯寒暄——只输出 <SILENCE>，不要重复或补充。"
    "若用户在让你办事/查询/看图/出图/设安排而尚未办到——现在立即调对应工具；"
    "缺具体参数时也先用上文实体或记忆/查询/搜索工具尝试一次，禁止只用澄清收束；"
    "禁止假装已经查过或记过。）"
)


def _correction_nudge_markers() -> tuple[str, ...]:
    """全部 settle 纠正 nudge 文案集合（懒导入 speech_policy 避免环）。

    纠正轮结束后统一从持久 history 剥掉这些 user turn：它们是框架内部指令，
    累积会污染上下文并破坏 provider 前缀缓存（方案四/五）。
    """

    return (
        _FAKE_DONE_NUDGE,
        _STRUCTURAL_ZERO_TOOL_NUDGE,
        _RENDER_DELEGATE_NUDGE,
        _REPORT_SPEECH_NUDGE,
        _STATUS_ZERO_TOOL_NUDGE,
    )


_RENDER_TOOL_NAMES = frozenset({"render_html_to_image", "render_card", "render_markdown_to_image"})
# find_tools 空转阈值更严（同工具连打）
_FIND_TOOLS_THRASH_LIMIT = 2
# 搜索/拉取类返回「够长+多行」即视为可出图材料（不靠业务词）
_SEARCHISH_TOOL_HINTS = ("search", "web_", "fetch", "knowledge")

# 同工具空转熔断（形状信号，非业务词）：
# - **跨轮**计数：同一 ModelResponse 内并行多次同名工具（多 query 检索）只计 1 轮
# - 阈值 4：连续 ≥4 轮只打同一工具才注入收敛（避免误伤 research 并行 web_search）
_THRASH_SAME_TOOL_LIMIT = 4
_THRASH_FUSE_NUDGE = (
    "（系统校验：你已跨多轮连续只重复同一工具，仍无新进展。立即停止再连打该工具；"
    "换另一路径（如 find_tools / 其它工具）或基于已有结果交付结论，禁止空转。）"
)


def _update_thrash_streak_for_response(
    tool_names: Sequence[str],
    *,
    prev_name: str,
    prev_streak: int,
) -> tuple[str, int]:
    """根据本 ModelResponse 的工具名列表更新 thrash 状态。

    - 无工具调用：保持原 streak（纯文本轮不重置、也不累加）
    - 本响应混用多种工具：重置
    - 本响应仅一种工具（含并行多 call）：同名则 +1 轮，换名则 streak=1
    """
    if not tool_names:
        return prev_name, prev_streak
    unique = {n for n in tool_names if n}
    if len(unique) != 1:
        return "", 0
    name = next(iter(unique))
    if name == prev_name:
        return name, prev_streak + 1
    return name, 1


def _matched_delegation_only_profile(query: str) -> str:
    """用户意图是否命中某个"工具对主人格隐藏、只能委派"的能力代理画像。

    返回命中的 ``profile_id``；无命中返回 ``""``。判定：画像的 ``match_keywords`` /
    ``profile_id`` 命中 ``query``，且该画像 ``tool_names`` 引用了
    ``NON_SEARCHABLE_TOOL_CATEGORIES`` 分类里的工具——这些工具既不在保底池
    (self/buildin)、也永不被向量检索召回（见 ``rag.tools``），即主人格自己根本
    够不到。命中时调用方会给主人格补 ``create_subagent`` 作为委派入口——否则会
    复现实测问题：主人格想干"写插件"这类活，却既没有对应工具、又没有委派入口，
    只能放弃或拿碎片工具硬拼。
    """
    h = (query or "").strip().lower()
    if not h:
        return ""

    from gsuid_core.ai_core.register import get_registered_tools
    from gsuid_core.ai_core.agent_node import list_nodes

    registered = get_registered_tools()
    hidden_names: set[str] = set()
    for cat in NON_SEARCHABLE_TOOL_CATEGORIES:
        if cat in registered:
            hidden_names.update(registered[cat].keys())
    if not hidden_names:
        return ""

    for node in list_nodes():
        matched = node.node_id.lower() in h or any(kw.lower() in h for kw in node.match_keywords)
        if matched and any(tn in hidden_names for tn in node.tool_names):
            return node.node_id
    return ""


def _pool_overlaps_capability_agent(tool_names_in_pool: set[str]) -> str:
    """当前工具池是否包含某个已注册能力代理（非 persona）覆盖的专业域工具。

    返回命中的 ``node_id``；无命中返回 ``""``。

    背景（OOC 分析 RC-0）：插件注册的专业工具若直接在主人格工具池中可调用，但
    TOOL_ORCHESTRATION_CONSTRAINTS §3.1 要求 B 类查询（需要分析/推荐/评估）必须
    委派给 create_subagent(agent_profile=...)。旧逻辑只在
    NON_SEARCHABLE_TOOL_CATEGORIES 命中时注入 create_subagent，导致专业域工具
    在池中但委派入口缺失——模型被迫直调，结构化长数据污染主人格上下文 → OOC。

    本函数扩展委派保障：只要池中有能力代理覆盖的工具，就注入 create_subagent，
    让模型可以按 §3.1 自主决定直接调还是委派。代价仅 +1 个工具 schema。
    """
    if not tool_names_in_pool:
        return ""

    from gsuid_core.ai_core.agent_node import list_nodes, resolve_pack_tool_names

    for node in list_nodes():
        # 跳过 persona 投影和内部评估器——它们不是可委派的能力代理
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        # 该能力代理的工具集（packs 展开 + 显式白名单）
        agent_tool_names = set(resolve_pack_tool_names(node.tool_packs) + node.tool_names)
        if not agent_tool_names:
            continue
        overlap = agent_tool_names & tool_names_in_pool
        if overlap:
            return node.node_id
    return ""


def _capability_exclusive_tool_names() -> set[str]:
    """能力代理**专属**工具名（不含主人格保底/日常基建）。

    主人格交互会话应剥离专业域工具，只留 create_subagent 委派入口——否则模型永远
    走「直接调专业工具」捷径。共享集合 = task_basics + 保底分类(self/buildin/meta)
    + 与主人格日常对话重叠的 common 基建（提醒管理/审批/表情等）——
    能力代理可复用这些工具，但不得把它们从主人格池里「独占剥离」。
    """
    from gsuid_core.ai_core.register import get_registered_tools
    from gsuid_core.ai_core.agent_node import TASK_BASICS_PACK, list_nodes, resolve_pack_tool_names

    shared: set[str] = set(resolve_pack_tool_names([TASK_BASICS_PACK]))
    registered = get_registered_tools()
    # 保底分类永不独占剥离；common 只保留日常基建（有域白名单 / 无域=通用基建）
    for cat in ("self", "buildin", "meta"):
        if cat in registered:
            shared.update(registered[cat].keys())
    _daily_common_domains = frozenset({"定时任务", "审批交互", "表情", "用户档案"})
    if "common" in registered:
        for _name, _tb in registered["common"].items():
            _dom = _tb.capability_domain or ""
            if not _dom or _dom in _daily_common_domains:
                shared.add(_name)

    exclusive: set[str] = set()
    for node in list_nodes():
        if node.source == "persona" or node.node_id == "capability_evaluator":
            continue
        owned = set(resolve_pack_tool_names(node.tool_packs) + list(node.tool_names))
        exclusive |= owned - shared
    return exclusive


def _format_capability_roster() -> str:
    """兼容旧调用点；实现已迁到 agent_node.registry.format_capability_roster。"""
    from gsuid_core.ai_core.agent_node.registry import format_capability_roster

    return format_capability_roster()


# 工具返回后的输出契约：事件驱动（本轮出现过 ToolReturn），不认业务关键词
# 契约常量见 capability_agents.delegation_contracts


def _tool_call_targets_render_agent(part: ToolCallPart) -> bool:
    """create_subagent 的 agent_profile 是否解析到 render_agent。"""
    return _tool_call_targets_render_agent_core(
        tool_name=part.tool_name,
        args=part.args_as_dict(),
        args_json=part.args_as_json_str(),
    )


def _tool_return_looks_failed(part: ToolReturnPart) -> bool:
    """结构判据：outcome 非 success，或内容为空/空容器。不扫正文业务词。"""
    if part.outcome != "success":
        return True
    content = part.content
    if content is None:
        return True
    if isinstance(content, str):
        s = content.strip()
        if not s:
            return True
        # 结构化空结果：list/dict/null 字面量（record_list 等常返回 "[]"）
        if s in ("[]", "{}", "null", "None", "none"):
            return True
    if isinstance(content, (list, tuple, set, dict)) and len(content) == 0:
        return True
    if isinstance(content, bytes) and len(content) == 0:
        return True
    return False


def _tool_return_is_async_pending(part: ToolReturnPart) -> bool:
    """异步子任务 ack：非终态，不得触发出图/事实包契约。"""
    content = part.content
    if not isinstance(content, str):
        return False
    body = content
    return "后台执行" in body or "自动回灌" in body or "仍在执行" in body
