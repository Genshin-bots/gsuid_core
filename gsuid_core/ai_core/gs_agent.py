"""
PydanticAI Agent 核心模块
基于 pydantic_ai 实现的轻量级 Agent
"""

import re
import time
import uuid
import base64
import asyncio
import contextvars
from typing import Any, List, Tuple, Union, Literal, TypeVar, Callable, Optional, Sequence, overload

import httpx
from pydantic_ai import Agent
from pydantic_graph import End
from sqlalchemy.exc import SQLAlchemyError
from pydantic_ai.agent import CallToolsNode, ModelRequestNode
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    VideoUrl,
    UserContent,
    ModelMessage,
    ModelRequest,
    ThinkingPart,
    ToolCallPart,
    UploadedFile,
    BinaryContent,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core import wall_clock, output_gate, output_firewall, angle_bracket_guard, interaction_scaffold
from gsuid_core.ai_core.const import (
    _SKILLS_CREATE_BY,
    _AGENTIC_CREATE_BY,
    _STICKY_FAMILY_TURNS,
    STALE_CHAT_REQUEST_TTL,
    ENABLE_PROGRESSIVE_TOOLS,
    _PROGRESSIVE_TOOLS_SKIP_INTENTS,
)
from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    SILENCE_MARKERS,
    ERROR_TIMEOUT_TEXT,
    ERROR_RESULT_PREFIX,
    ERROR_CONTENT_REJECTED,
    send_chat_result,
    _relean_user_turn,
    fetch_video_bytes,
    _extract_run_context,
    _is_content_rejected,
    materialize_image_url,
    _split_embedded_thinking,
    _drop_orphan_tool_results,
    _truncate_message_for_log,
    _is_retryable_client_error,
    _is_non_retryable_model_error,
    notify_master_of_budget_block,
    _compact_report_blocks_in_history,
    _strip_remote_images_from_history,
    _truncate_tool_returns_in_history,
    _truncate_history_with_tool_safety,
    _canonicalize_tool_call_args_in_parts,
    _sanitize_tool_call_artifacts_in_parts,
)
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.skills import skills_toolset
from gsuid_core.ai_core.register import find_tool_base, get_tools_by_capability_domain
from gsuid_core.ai_core.rag.tools import (
    NON_SEARCHABLE_TOOL_CATEGORIES,
    ToolList,
    get_main_agent_tools,
    get_scope_context_tags,
    expand_tools_to_families,
    get_tools_by_context_tags,
    search_tools_with_entity_routing,
)
from gsuid_core.ai_core.configs.models import (
    AnyModel,
    get_model_for_task,
    get_model_by_full_name,
    get_config_name_for_task,
    get_2nd_config_name_for_task,
    get_model_fingerprint_for_task,
)
from gsuid_core.ai_core.session_logger import AISessionLogger, ProactiveSource
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.dynamic_toolset import RetrievableToolset
from gsuid_core.ai_core.persona.prompts import INNER_OS_MARKER, CHARACTER_BUILDING_TEMPLATE
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.configs.provider_router import (
    provider_router,
    looks_like_provider_failure,
)

_T = TypeVar("_T")

# 历史裁剪低水位比例：超过 max_history 时一次裁到 max_history * 该比例。
# 比例越高裁剪越温和，保留更多可缓存前缀（0.85 = 仅丢弃 15% 溢出量）。
_HISTORY_TRIM_RATIO = 0.85

# 父 run 把本次归属 scope 写入此 contextvar，途中 spawn 的嵌套子 agent 自动继承记账： await 的子协程共享 Context
# create_task 复制创建时 Context，两条 spawn 路径都覆盖。
_current_budget_scope: contextvars.ContextVar[Optional[Tuple[str, str, str]]] = contextvars.ContextVar(
    "gs_budget_scope", default=None
)


def _budget_scope_from_event(ev: Event) -> Tuple[str, str, str]:
    """从 Event 取预算 scope 三元组 (group_id, user_id, bot_id)。私聊 group_id 为空串。"""
    return (str(ev.group_id) if ev.group_id else "", str(ev.user_id), ev.bot_id or "")


def set_budget_scope_context(scope: Optional[Tuple[str, str, str]]) -> contextvars.Token:
    """为后台自主 LLM 调用设置「当前预算归属 scope」。

    记忆摄入 / 群组认知等后台 worker 既不经 Event、也不显式 `bind_budget_scope`，其
    `create_agent().run()` 默认落到「无归属」的全局统计、不计入任何 Session 额度。worker
    在处理某 scope 的数据期间用本函数设置 contextvar，则其间所有 run 经 `_resolve_budget_scope`
    回退到此 scope 记账（只记账、不触发闸门）。返回的 token 必须在结束时交回
    `reset_budget_scope_context` 还原，避免泄漏到上层调用栈。
    """
    return _current_budget_scope.set(scope)


def reset_budget_scope_context(token: contextvars.Token) -> None:
    """还原 `set_budget_scope_context` 设置的 contextvar。"""
    _current_budget_scope.reset(token)


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

# on_trace 轨迹事件类型：模型推理段 / 工具调用（见 GsCoreAIAgent._emit_trace）
TraceKind = Literal["thinking", "tool"]
# C-4 墙钟软预算阈值走 ai_config `scaffold_wall_clock_budget`（秒），可在线调
_WALL_CLOCK_NUDGE = (
    "（系统提示：本轮处理耗时已超预算。立即基于已有信息用角色口吻给出最终回复，"
    "不要再发起任何新的工具调用；信息不全就如实说明现状，绝不编造。）"
)

_FAKE_DONE_NUDGE = (
    "（系统校验：你上一条回复声称已完成某个操作，但本轮没有任何工具调用记录，该声明是编造的。"
    "现在立即调用对应工具真正执行（改/取消既有安排先用列表类工具定位目标）；若确实做不到，"
    "就如实向用户说明「刚才说错了，还没有做」。绝不允许再输出不带工具调用支撑的完成话术。）"
)

# 结构假完成：被呼叫 + 池内有工具 + 零调用 + 非沉默/非极短寒暄（不解析用户话题词）
_STRUCTURAL_ZERO_TOOL_NUDGE = (
    "（系统校验：本轮你被直接呼叫（或同人省略续聊），且工具池非空，但你没有调用任何工具就结束了。"
    "若用户在让你办事/查询/看图/出图/设安排——现在立即调对应工具；"
    "缺具体参数时也先用上文实体或记忆/查询/搜索工具尝试一次，禁止只用澄清收束；"
    "若只是纯寒暄，用一句角色短回即可，不要假装已经查过或记过。）"
)

_RENDER_TOOL_NAMES = frozenset({"render_html_to_image", "render_card", "render_markdown_to_image"})
_RENDER_DATA_NUDGE = (
    "（系统校验：本轮工具已返回多项/结构化数据，但尚未调用渲染出图工具。"
    "请立即用 render_html_to_image（或 render_card / render_markdown_to_image）出图，"
    "台词只留一两句角色引导，禁止把多条数据念成台词；渲染失败也只许短结论，禁止长列表凑数。）"
)
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
# 主人格 / 评测：多项数据 → 工具出图
_POST_TOOL_OUTPUT_CONTRACT = (
    "（系统：本轮已有工具返回。若结果含多项数据点，必须 "
    "render_html_to_image 自写 HTML 出图（也可用 render_card / render_markdown_to_image）；"
    "渲染工具自动发图，禁止台词复述、禁止 <report> 文本块。台词只留一两句角色化引导。）"
)

# 能力代理：只交 Markdown/JSON 事实包；出图归主人格
_POST_TOOL_OUTPUT_CONTRACT_CAPABILITY = (
    "（系统：本轮已有工具返回。你是能力代理——必须把结果整理成 **Markdown 或 JSON 事实包** "
    "交付主人格（条目/日期/数字/来源/依据），或 artifact_put 持久化。"
    "有搜索/查询结果时**禁止**只回过程句（如「下面再搜」「停止重复」「然后渲染」）。"
    "**禁止**调用 render_html_to_image / render_card / render_markdown_to_image（出图由主人格负责）。）"
)

# 工具失败/空结果：强制换路（只看 outcome / 空内容，禁止扫正文关键词）
_POST_TOOL_FAIL_CONTRACT = (
    "（系统：本轮工具返回失败或空结果。禁止用角色懒惰结束本轮。"
    "立刻换路：优先 web_search_tool 再取数；或 find_tools 后换工具。"
    "取到多项数据必须 render_html_to_image 出图。只有换路后仍无果才可角色化短句说明。）"
)

_POST_TOOL_FAIL_CONTRACT_CAPABILITY = (
    "（系统：本轮工具返回失败或空结果。禁止只回过程句结束。"
    "立刻换路：换 query / 换工具再取数；仍无果则在事实包里明确写「无检索结果：原因=…」。"
    "禁止 render_html_*（出图归主人格）。）"
)


def _post_tool_contracts_for(create_by: str) -> tuple[str, str]:
    """主人格推 render；能力代理推事实包交付。"""
    if create_by == "CapabilityAgent":
        return _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY, _POST_TOOL_FAIL_CONTRACT_CAPABILITY
    return _POST_TOOL_OUTPUT_CONTRACT, _POST_TOOL_FAIL_CONTRACT


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


# OOC 修复 5.5：角色锚定消息提取
# 结构化格式特征（markdown 表格、编号列表、加粗标题）——命中即非"在角色内"
_STRUCTURED_FORMAT_RE = re.compile(
    r"^\s*\|.*\|.*\|"  # markdown 表格行
    r"|^\s*\d+[\.\)、]\s"  # 编号列表
    r"|^\s*\*\*[^*]+\*\*\s*[:：]?"  # 加粗标题
    r"|^\s*[-•]\s+\*\*",  # 加粗列表项
    re.MULTILINE,
)


def _extract_character_anchors(history: list[ModelMessage], count: int = 2) -> list[ModelMessage]:
    """从历史中提取最早的、最符合角色设定的 assistant 文本回复作为"锚定消息"。

    选择标准（轻量规则，无 LLM）：
    - 是 ModelResponse 且包含 TextPart（非纯 ToolCallPart）
    - 文本长度 ≤ 150 字（角色短句）
    - 不含结构化格式（表格/编号/加粗标题）
    - 优先选择含语气词/省略号/角色动作描写的回复

    返回最多 ``count`` 条 ModelResponse 消息（保持原始顺序）。
    """
    from pydantic_ai.messages import TextPart as _TP, ModelResponse as _MR

    anchors = []
    for msg in history:
        if len(anchors) >= count:
            break
        if not isinstance(msg, _MR):
            continue
        # 提取文本内容
        text_parts = [p.content for p in msg.parts if isinstance(p, _TP) and p.content.strip()]
        if not text_parts:
            continue
        text = text_parts[0]
        # 跳过过长回复（OOC 特征）
        if len(text) > 150:
            continue
        # 跳过含结构化格式的回复
        if _STRUCTURED_FORMAT_RE.search(text):
            continue
        # 跳过 <SILENCE> 和系统标记
        if text.strip() in ("<SILENCE>", ""):
            continue
        anchors.append(msg)
    return anchors


# scope_key（记忆 scope，见 memory/scope.py）→ 可嵌进 session_id 的一段指向标识： group:789012 →
# group-789012 / user_global:12345 → uglobal-12345 /
_SCOPE_SEG_CODE: dict[str, str] = {
    "group": "group",
    "user_global": "uglobal",
    "user_in_group": "uingroup",
    "self": "self",
}


def _scope_id_segment(scope_key: Optional[str]) -> str:
    """把 scope_key 压成 session_id 里的一段（无法解析 / 未提供时返回空串）。"""
    if not scope_key:
        return ""
    prefix, _, rest = scope_key.partition(":")
    code = _SCOPE_SEG_CODE.get(prefix)
    if not code or not rest:
        return ""
    return f"{code}-{rest}"


class GsCoreAIAgent:
    """
    基于 PydanticAI 的 Agent 封装类

    Attributes:
        model_name: 模型名称
        api_key: API 密钥
        base_url: API 基础 URL
        max_tokens: 最大输出 token 数
        system_prompt: 系统提示词
    """

    def __init__(
        self,
        openai_chat_model: Optional[AnyModel] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        max_iterations: Optional[int] = None,
        persona_name: Optional[str] = None,
        max_history: Optional[int] = None,
        create_by: str = "LLM",
        task_level: Literal["high", "low"] = "high",
        session_id: Optional[str] = None,
        is_subagent: bool = False,
        dynamic_tools: Optional[bool] = None,
        scope_key: Optional[str] = None,
        wall_clock_budget: Optional[float] = None,
        on_trace: Optional[Callable[[TraceKind, str], None]] = None,
    ):
        # max_tokens / max_history 未显式传入时落到全局配置（主对话等走默认的路径据此可调）
        _max_history: int = max_history if max_history is not None else ai_config.get_config("agent_max_history").data
        _max_tokens: int = max_tokens if max_tokens is not None else ai_config.get_config("agent_max_tokens").data
        self.history: List[ModelMessage] = []
        self.max_history = _max_history
        self.system_prompt = system_prompt
        # 稳定前缀构建时刻：ai_router 按 TTL 原地刷新 system_prompt（O-3 慢变上下文防僵化）
        self.system_prompt_built_at: float = time.time()
        self.persona_name = persona_name  # 用于热重载检查
        # 用于串行执行 run 方法的锁
        self._run_lock = asyncio.Lock()
        # A: 同 Session 新消息抢答时 set，当前 generation 在节点间隙 abort
        self._cancel_generation = asyncio.Event()
        self.max_tokens = _max_tokens
        self.max_iterations = max_iterations  # 自定义迭代次数限制，None时使用配置默认值
        # C-4 墙钟软预算(秒)覆写：None=沿用全局 scaffold_wall_clock_budget；<=0=本 Agent 关闭软预算。
        # 长流程入口（画布编排/Agent编排等，一轮几十次工具调用 + 等人确认）必须放宽，否则永远跑不到终态。
        self.wall_clock_budget = wall_clock_budget
        # 轨迹观察者：让宿主（画布/Agent编排前端的"思考过程"折叠块等）看见模型推理与工具调用， 不必去翻 session log。
        # 契约见 _emit_trace。
        self.on_trace = on_trace
        self.task_level: Literal["high", "low"] = task_level  # 任务级别，用于选择对应的模型配置

        self.create_by = create_by
        # 未显式给 session_id 的来源（能力评估 / meme 打标 / 记忆摄入·检索等后台 LLM
        # 调用）自动派生一个一次性 subagent id——这样"所有调用来源都写 session log"
        if session_id is None:
            # 传了 scope_key 的后台调用（记忆抽取 / 归类 / 群摘要 / 节点选择等）把"针对哪个群/用户"
            # 编进 id，让 webconsole 能显示指向，而不再是一串无差别的 auto_XXX_hash。
            _seg = _scope_id_segment(scope_key)
            _suffix = uuid.uuid4().hex[:8]
            session_id = f"auto_{create_by}_{_seg}_{_suffix}" if _seg else f"auto_{create_by}_{_suffix}"
            is_subagent = True
        self.session_id: str = session_id
        self.is_subagent: bool = is_subagent
        # 五层自动装配（dynamic 能力族）开关：True=每轮装配并与显式 tools 合并；
        # False=永不装配；None=沿用旧门（create_by ∈ _AGENTIC_CREATE_BY 且未传 tools）。
        self.dynamic_tools: Optional[bool] = dynamic_tools
        # 预算归属 scope：(group_id, user_id, bot_id)。ev 缺失的自主入口经 bind_budget_scope
        # 显式绑定，使 Token 记入对应 Session 额度并受闸门约束；None=未绑定，回退 contextvar。
        self._budget_scope: Optional[Tuple[str, str, str]] = None

        # 连续无工具调用计数：连续多轮只输出文本、不调用任何工具时， 下一轮注入强制提醒，
        self._consecutive_no_tool_rounds: int = 0

        # L3 会话驻留：最近使用过的能力族 → 剩余可常驻轮数（每轮递减）。
        # 兜底"刚用过某能力、紧接着的追问语义却召不回该工具"的场景。
        self._recent_tool_families: dict[str, int] = {}
        # 本轮实际装配（保底 + 附加）工具的能力域集合，run() 装配后回填。
        self._last_assembled_domains: set[str] = set()
        # L5 上下文增强检索：最近几轮用户原话，拼进工具向量检索 query，
        self._recent_user_texts: List[str] = []
        # by_bot 单轮已发送文本去重集合：弱模型常跨轮重复同一段最终答复，叠加瞬时 故障重试重发，
        self._run_sent_texts: set[str] = set()
        # 最近一次 attempt 内已执行的工具名（与 _execute_run_once 的局部列表同引用）：
        self._last_attempt_tool_calls: List[str] = []
        # C-2 漂移预算的上轮计数：只在计数**增加**时注入提醒，防一次 push 滞留
        # recent 窗口导致后续每轮重复唠叨（会话级状态，正是"预算"的容器）。
        self._last_drift_push_count: int = 0

        self.model: Optional[AnyModel] = openai_chat_model
        # 记录本会话激活配置全名（provider++name）与内容指纹，仅自动解析模型时记录；显式传
        # model 的会话（如固定模型 SubAgent）保持 None 不参与热替换，详见 refresh_model_if_changed。
        self.model_config_name: Optional[str] = None
        self.model_config_fingerprint: Optional[str] = None
        if self.model is None:
            self.model = get_model_for_task(task_level)
            self.model_config_name = get_config_name_for_task(task_level)
            self.model_config_fingerprint = get_model_fingerprint_for_task(task_level)

        # 初始化会话日志记录器：所有 Agent 恒有 logger（session_id 已在上方自动派生
        # 兜底），因此 _session_logger 非 Optional，run() 中不再需要 None 守卫。
        self._session_logger: AISessionLogger = AISessionLogger(
            session_id=session_id,
            system_prompt=system_prompt,
            persona_name=persona_name,
            create_by=create_by,
            is_subagent=is_subagent,
        )

    @property
    def last_run_sent_visible_reply(self) -> bool:
        """本轮 run 是否已向用户发出过可见文本。

        by_bot 模式成功时 run 返回空串，调用方（如 handle_ai 的好感度有效互动判定）
        不能以返回值判断"本轮说过话"，须读本属性（评审修复 F1）。
        """
        return bool(self._run_sent_texts)

    def _emit_trace(self, kind: TraceKind, text: str) -> None:
        """把模型思考 / 工具调用轨迹推给观察者（``on_trace``）。

        ``kind="tool"`` 的 text 形如 ``"<工具名>|<参数JSON>"``。

        宿主可据此把"Agent 在想什么、调了什么工具"实时呈现给用户（画布前端的
        「思考过程」折叠块就是消费方），而不必去翻 session log 文件。

        观察者是**旁路**：任何异常都吞掉并降级为 debug 日志——展示用的钩子
        绝不能把一次真实的 Agent run 带崩。
        """
        if self.on_trace is None or not text:
            return
        try:
            self.on_trace(kind, text)
        except Exception as e:  # noqa: BLE001
            logger.debug(i18n_t("log.ai_agent.on_trace_observer_fail", error=str(e)))

    def get_assembled_capability_domains(self) -> list[str]:
        """返回**上一轮 run() 实际装配工具**的能力域列表（"装配后回传"）。

        供 handle_ai 偏好注入做精确能力域过滤：相比按 query 子串近似匹配能力域，本集合是
        gs_agent 真实装配（保底 + 状态驱动 + 会话驻留 + 向量召回族展开）后的工具能力域，
        更贴合"本轮可用工具"。首轮（尚未装配）为空，handle_ai 据此退化为仅 query 近似。
        """
        return list(self._last_assembled_domains)

    def append_proactive_assistant_turn(
        self,
        content: str,
        source: ProactiveSource,
        trigger_reason: str,
        generator_log_files: Optional[List[str]] = None,
    ) -> None:
        """把一条主动消息以 assistant-only ModelMessage 形式追加进 history。

        语义：Heartbeat / ScheduledTask / Kanban / 工具主动 send 等"框架在 LLM
        run 之外注入的输出"——它们没有配对的 ModelRequest（pydantic_ai 允许这种
        assistant-only turn 出现在 message_history 里）。本方法保证：
        1. 下一轮用户搭话时 pydantic_ai 的 message_history 内能看到这条输出，
           主 Agent 不会"对自己刚说过的话失忆"。
        2. 同步在 session_logger 记一条 `proactive_emission` entry，前端可按
           source 分桶展示。
        3. 调用 extract_history()，复用 `_drop_orphan_tool_results` 兜底，
           防止裸 TextPart 触发 pydantic_ai message_history 自洽性问题。

        参考：plans/proactive_message_session_unification_20260529.md §3.5
        """
        if not content:
            return
        self.history.append(ModelResponse(parts=[TextPart(content=content)]))
        self._session_logger.log_proactive_emission(
            source=source,
            content=content,
            trigger_reason=trigger_reason,
            generator_log_files=generator_log_files,
        )
        # 复用现有清理逻辑：纯 TextPart 不会被孤儿工具结果清理误伤，但顺手
        # 保证下次 _agent.iter(message_history=self.history) 入参自洽。
        self.extract_history()

    def extract_history(self):
        if self.max_history <= 0:
            self.history = []
            return

        before: int = len(self.history)
        truncated: bool = before > self.max_history
        if truncated:
            # 高低水位惰性裁剪：超过 max_history 才裁、一次裁到低水位。旧行为"超 1 条裁 1 条"
            # 让历史头部每轮都变，provider 前缀缓存永不命中（§25 命中率卡 54% 的直接原因）。
            low_target: int = max(1, int(self.max_history * _HISTORY_TRIM_RATIO))

            # OOC 修复 5.5：compact 时保留 1-2 条"角色锚定消息"（最早的、最符合人设的 assistant 文本回复）。
            # 早期在角色内的回复被丢弃后，模型失去"我应该是这样
            _anchor_msgs: list[ModelMessage] = []
            if self.persona_name:
                _anchor_msgs = _extract_character_anchors(self.history, count=2)

            self.history = _truncate_history_with_tool_safety(
                self.history,
                low_target,
            )

            # 将锚定消息插回历史头部（截断后的最早消息之前）
            if _anchor_msgs:
                # 去重：如果锚定消息已经在截断后的历史中，不重复插入
                _existing_ids = {id(m) for m in self.history}
                _to_insert = [m for m in _anchor_msgs if id(m) not in _existing_ids]
                if _to_insert:
                    self.history = _to_insert + self.history
                    logger.debug(
                        i18n_t(
                            "log.agent.compact_retained",
                            p0=len(_to_insert),
                        )
                    )

        # 兜底：无论是否截断，都做一次孤儿工具结果清理，确保历史对 API 自洽
        self.history = _drop_orphan_tool_results(self.history)
        after: int = len(self.history)
        # 仅「因超长主动裁剪且确有条目被丢弃」才打 auto_compact（供 webconsole 画独立色块）；
        # 纯孤儿清理属结构性整理、stateless 模式每轮清空，均不打标以免噪声。
        if truncated and after < before:
            self._session_logger.log_history_reset("auto_compact", {"before": before, "after": after})
        logger.debug(i18n_t("log.agent.history_processed_entries", p0=len(self.history)))

    async def refresh_model_if_changed(self) -> bool:
        """运行期检测：本会话 task_level 对应的激活模型配置变化时，就地热替换 self.model。

        解决"网页控制台改模型后必须 coreclear 清空会话才生效"的问题：存活会话在下一次 run
        时即时换到新模型。与 Persona 热重载不同——换模型不应丢失对话历史，因此这里**只替换
        模型对象、保留 self.history**（仅换"大脑"不换"记忆"），并关闭旧客户端释放连接池。

        变化判定用「全名 + 内容指纹」双键：既覆盖"切到另一个配置文件"（全名变），也覆盖
        "原地改当前配置文件字段(含 request_method/base_url 等)"（全名不变但指纹变）。

        仅对"按 task_level 自动解析模型"的会话生效（``model_config_name`` 非 None）；显式绑定
        固定模型的会话（如后台 SubAgent）不受影响。新配置加载失败时沿用原模型，不打断会话。

        Returns:
            是否发生了热替换
        """
        if self.model_config_name is None:
            return False

        current = get_config_name_for_task(self.task_level)
        # 配置被清空（current 为空）时不动：避免把仍可用的会话打成不可用。
        if not current:
            return False

        current_fp = get_model_fingerprint_for_task(self.task_level)
        # 全名与内容指纹都未变才视为无变化；任一变化都触发热替换。
        if current == self.model_config_name and current_fp == self.model_config_fingerprint:
            return False

        # 仅捕获配置非法（空名/未知 provider）这一可预期失败：沿用原模型不打断会话；
        # 其余意外错误照常抛出，符合 §1.1 不吞噬非预期异常。
        try:
            new_model = get_model_for_task(self.task_level)
        except ValueError as e:
            logger.warning(
                i18n_t(
                    "log.agent.level_config_change_load",
                    p0=self.task_level,
                    current=current,
                    e=e,
                )
            )
            return False

        old = self.model_config_name
        # 旧模型不关底层 client：本项目所有模型共享 pydantic-ai 进程级缓存 httpx 客户端
        # close 会拖垮全进程会话（曾致所有请求报 client has been closed），交给 GC 即可。
        self.model = new_model
        self.model_config_name = current
        self.model_config_fingerprint = current_fp
        # 全名变=换配置文件；全名同指纹变=原地改了当前配置文件字段。
        change_desc = f"{old} → {current}" if old != current else f"{current}（配置内容已更新）"
        logger.info(
            i18n_t(
                "log.agent.level_config_change_desc",
                p0=self.task_level,
                change_desc=change_desc,
                p1=self.session_id,
            )
        )
        return True

    @staticmethod
    def _is_video_item(item: UserContent) -> bool:
        """内容项是否为视频（VideoUrl 或视频类 BinaryContent）。"""
        if isinstance(item, VideoUrl):
            return True
        return isinstance(item, BinaryContent) and str(item.media_type or "").startswith("video/")

    async def _video_item_to_bytes(self, item: UserContent) -> tuple[bytes, str]:
        """视频内容项 → (字节, mime)。"""
        if isinstance(item, BinaryContent):
            return item.data, str(item.media_type or "video/mp4")
        assert isinstance(item, VideoUrl)
        return await fetch_video_bytes(item.url)

    async def _prepare_video_content(
        self,
        content_list: list[UserContent],
        model_support: str,
    ) -> list[UserContent]:
        """视频内容项的三分支兼容处理（在图片分支**之前**执行）。

        pydantic_ai 的 OpenAI/Anthropic 模型不接受 VideoUrl——若原样留在
        message_history 里，请求时直接抛错且每轮重发都会复现。因此视频项必须
        在入历史前就地转换为该 provider 可消费的形式：

        - **gemini + 支持 video**：经 Gemini File API 上传到 Google 服务器，
          转为 ``UploadedFile(file_id=<file_uri>, provider_name="google-gla")``
          按引用传递（文件在 Google 侧保留 48h，超长会话中过期后重发会报错）；
          已是 Files API URI 的 VideoUrl 直接转引用，不重复上传。
        - **非 gemini + 支持 video（且支持 image）**：本地 ffmpeg 每 2 秒抽一帧，
          转成 ImageUrl(base64 DataURI) 列表塞进 messages（帧数上限见
          ``frame_extract.DEFAULT_MAX_FRAMES``，超限等距采样）。
        - **不支持 video**：替换为文本占位说明，模型至少知道"这里有个视频"。

        任一视频处理失败只影响该视频（替换为失败说明文本），不阻断整条消息。
        """
        if not any(self._is_video_item(item) for item in content_list):
            return content_list

        from gsuid_core.ai_core.configs.models import get_provider_for_task
        from gsuid_core.ai_core.multimodal.gemini_files import (
            is_gemini_file_uri,
            upload_media_for_task,
        )

        provider = get_provider_for_task(self.task_level)
        supports_video = "video" in model_support
        supports_image = "image" in model_support

        result: list[UserContent] = []
        video_idx = 0
        for item in content_list:
            if not self._is_video_item(item):
                result.append(item)
                continue
            video_idx += 1

            if not supports_video:
                logger.warning(i18n_t("log.agent.declare_video_analysis_capability"))
                result.append(f"--- 视频{video_idx}: [当前模型不支持视频分析，无法查看该视频内容] ---")
                continue

            try:
                if provider == "gemini":
                    # ⚠️ media_type 必传：Files API URI 无扩展名，pydantic_ai 猜不出
                    # mime 会按 application/octet-stream 发送，Gemini 直接 400
                    if isinstance(item, VideoUrl) and is_gemini_file_uri(item.url):
                        # 已是 Files API 引用：直接转 UploadedFile，不重复上传
                        result.append(
                            UploadedFile(file_id=item.url, provider_name="google-gla", media_type="video/mp4")
                        )
                        continue
                    data, mime = await self._video_item_to_bytes(item)
                    file_uri = await upload_media_for_task(data, mime, self.task_level)
                    result.append(UploadedFile(file_id=file_uri, provider_name="google-gla", media_type=mime))
                    continue

                # 非 gemini：抽帧兼容路径要求模型至少能看图
                if not supports_image:
                    logger.warning(i18n_t("log.agent.declared_video_image_frame"))
                    result.append(f"--- 视频{video_idx}: [当前模型不支持图片，无法用抽帧方式分析该视频] ---")
                    continue

                from gsuid_core.ai_core.multimodal.frame_extract import extract_frames_ffmpeg

                data, mime = await self._video_item_to_bytes(item)
                video_format = mime.split("/")[-1] or "mp4"
                frames = await extract_frames_ffmpeg(data, video_format=video_format, interval_seconds=2.0)
                result.append(f"--- 视频{video_idx} 抽帧（每 2 秒 1 帧，共 {len(frames)} 帧，按时间顺序排列）---")
                for frame in frames:
                    b64 = base64.b64encode(frame).decode("ascii")
                    result.append(ImageUrl(url=f"data:image/jpeg;base64,{b64}"))
                logger.info(
                    i18n_t(
                        "log.agent.video_frame_sampled_images",
                        p0=video_idx,
                        p1=len(frames),
                    )
                )
            except Exception as e:
                logger.error(i18n_t("log.agent.process_video", p0=video_idx, e=e))
                result.append(f"--- 视频{video_idx}: [视频处理失败: {e}] ---")
        return result

    async def _prepare_user_message(
        self,
        content_list: list[UserContent],
    ) -> Union[str, list[UserContent]]:
        """处理用户消息中的图片/视频内容

        当 user_message 为 Sequence[UserContent] 时，检查其中是否包含多模态内容。
        视频项先经 :meth:`_prepare_video_content` 三分支转换（gemini 直传 /
        抽帧兼容 / 占位说明）；随后根据当前模型的 model_support 配置处理图片：
        - 模型支持图片：保留 ImageUrl，返回 list[UserContent]
        - 模型不支持图片：调用 understand_image 将图片转述为文本，合并到文本消息中

        Args:
            content_list: 用户消息内容列表

        Returns:
            处理后的消息，可能是 str 或 list[UserContent]
        """
        from gsuid_core.ai_core.configs.models import get_model_config_for_task
        from gsuid_core.ai_core.image_understand import understand_image

        model_config = get_model_config_for_task(self.task_level)
        model_support: str = model_config.get_config("model_support").data

        # 视频先行转换——OpenAI/Anthropic 模型不接受 VideoUrl，必须在入历史前处理掉
        content_list = await self._prepare_video_content(content_list, model_support)

        # 分离文本和图片
        text_parts: list[str] = []
        image_urls: list[str] = []
        for item in content_list:
            if isinstance(item, ImageUrl):
                image_urls.append(item.url)
            elif isinstance(item, str):
                text_parts.append(item)

        if "image" in model_support:
            # 模型支持图片，保留原始内容；
            result: list[UserContent] = []
            for item in content_list:
                if isinstance(item, str):
                    result.append(f"[用户发言]\n{item}")
                elif isinstance(item, ImageUrl):
                    # Fix-07 兜底：入历史前再次确认远程 URL 已物化为 base64；
                    # 若物化失败（仍为 http(s) URL），跳过该图片，避免把过期
                    url = await materialize_image_url(item.url)
                    if url.startswith(("http://", "https://")):
                        logger.warning(i18n_t("log.agent.image_materialization_adding_history", p0=item.url[:120]))
                        continue
                    result.append(ImageUrl(url=url))
                else:
                    result.append(item)
            return result

        # 模型不支持图片，调用图片理解模块转述
        if image_urls:
            logger.info(i18n_t("log.agent.imgund_images_image_paraphrasing", p0=len(image_urls)))
            # 用户问题：用于把冗长的图片描述按需精简到与问题相关的部分
            user_question = "\n".join(text_parts).strip()
            descriptions: list[str] = []
            for idx, url in enumerate(image_urls):
                try:
                    description = await understand_image(image_url=url, parent_session_id=self.session_id)
                    description = await self._summarize_image_description(description, user_question)
                    descriptions.append(f"图片{idx + 1}: {description}")
                except Exception as e:
                    logger.error(i18n_t("log.agent.imgund_understand_image", p0=idx + 1, e=e))
                    descriptions.append(f"图片{idx + 1}: [图片理解失败]")

            if descriptions:
                image_text = "--- 图片内容描述 ---\n" + "\n".join(descriptions)
                text_parts.append(image_text)

        combined = "\n".join(text_parts) if text_parts else ""
        return f"[用户发言]\n{combined}"

    async def _summarize_image_description(
        self,
        description: str,
        user_question: str,
    ) -> str:
        """对冗长的图片理解结果做二次摘要，只保留与用户问题直接相关的信息。

        图片理解的完整描述常常长达上千字（含大量与当前问题无关的细节），
        直接塞入上下文会严重浪费 Token。此处用低成本模型做一次聚焦摘要。

        描述较短（不超过 400 字）时直接返回原文，不额外调用模型。
        """
        SUMMARY_THRESHOLD = 400
        if not description or len(description) <= SUMMARY_THRESHOLD:
            return description

        try:
            prompt = (
                "以下是一张图片的完整描述。"
                f"用户正在问：「{user_question or '（无明确问题）'}」。\n"
                "请从图片描述中提取与用户问题直接相关的信息，用 1-3 句话概括，"
                "无关信息完全省略。若用户没有明确问题，则用一句话概括图片主旨。\n\n"
                f"【图片完整描述】\n{description}"
            )
            # 二次摘要也是一次真实 LLM 调用：走 create_agent 自动派生
            # auto_ImageDescSummary_* 的 subagent 日志，并 link 到当前调用方
            summary_agent = create_agent(
                system_prompt="你是一个图片信息提炼助手，只输出精简摘要，不输出多余解释。",
                max_tokens=500,
                max_iterations=1,
                create_by="ImageDescSummary",
                task_level="low",
                is_subagent=True,
            )
            try:
                summary = str(await summary_agent.run(prompt, return_mode="return")).strip()
            finally:
                self._session_logger.link_agent(
                    agent_session_id=summary_agent.session_id,
                    agent_session_uuid=summary_agent._session_logger.session_uuid,
                    agent_type="sub_agent",
                    create_by="ImageDescSummary",
                    log_file=str(summary_agent._session_logger._file_path),
                )
                summary_agent._session_logger.close()
            if summary:
                logger.debug(i18n_t("log.agent.imgund_image_description_second", p0=len(description), p1=len(summary)))
                return summary
        except Exception as e:
            logger.debug(i18n_t("log.agent.imgund_image_description_second_fail", e=e))
        return description

    def bind_budget_scope(self, ev: Optional[Event]) -> None:
        """显式绑定本会话的预算归属 scope。

        供 `ev` 缺失但仍应计入某 Session 额度的自主入口（巡检 / proactive / 用户绑定的
        持久会话）使用：绑定后该 agent 的每次 run 都按此 scope 记账，并在 `budget_gate=True`
        时受闸门约束。传 None 解除绑定。
        """
        self._budget_scope = _budget_scope_from_event(ev) if ev is not None else None

    def _resolve_budget_scope(self, ev: Optional[Event]) -> Optional[Tuple[str, str, str]]:
        """解析本次 run 的预算归属 scope。

        优先级：显式 `ev` > 实例绑定（`_budget_scope`，巡检 / proactive / 用户绑定会话）>
        contextvar（父 run 透传给在途嵌套子 agent）。全为空时返回 None——纯后台、无 scope
        的调用只可能受 global 规则约束、不写 Session 账本。
        """
        if ev is not None:
            return _budget_scope_from_event(ev)
        if self._budget_scope is not None:
            return self._budget_scope
        return _current_budget_scope.get()

    @overload
    async def _execute_run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
        output_type: None = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[Any] = None,
        cheap_gate: Optional[Any] = None,
    ) -> str: ...

    @overload
    async def _execute_run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
        output_type: type[_T] = ...,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[Any] = None,
        cheap_gate: Optional[Any] = None,
    ) -> _T: ...

    async def _execute_run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
        output_type: Optional[type] = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[Any] = None,
        cheap_gate: Optional[Any] = None,
    ) -> Union[str, Any]:
        """核心回复请求的瞬时失败重试包装。

        把单次执行交给 ``_execute_run_once``；网络/超时/5xx/529 等瞬时故障会以异常
        冒泡到这里，等待 ``agent_run_retry_delay`` 秒后重试，至多 ``agent_max_run_attempts`` 次，
        全部失败才按异常类型记录统计并返回错误文案。``UsageLimitExceeded`` 已在
        ``_execute_run_once`` 内走专属兜底总结、不会传到这里，故不会被重试。
        每次重试都复用未被改写的 ``self.history``（成功后才追加），从干净状态重跑。
        """
        from gsuid_core.ai_core.statistics import statistics_manager

        # 跨重试共享、按用户轮次重置：重试重跑 _execute_run_once 不会重发已送达的段；
        # 新一轮 run 则允许合法地再说同样的话。
        self._run_sent_texts = set()

        max_attempts: int = ai_config.get_config("agent_max_run_attempts").data
        retry_delay: float = ai_config.get_config("agent_run_retry_delay").data

        # 非内容审核的 4xx 允许一次干净历史重试：模型退化产生的畸形请求是随机性的
        # 从未被污染的 self.history 重跑大概率成功（见 plans/prod_session_review §2）。
        client_error_retry_used = False

        def _fail(text: str) -> str:
            # 错误路径也要闭合 run：否则 session 日志留下悬空 run_start（webconsole 无法渲染结束）
            self._session_logger.log_run_end()
            self._session_logger.log_result(text, [])
            return text

        attempt = 0
        total_attempts = max_attempts
        while attempt < total_attempts:
            attempt += 1
            try:
                return await self._execute_run_once(
                    user_message=user_message,
                    bot=bot,
                    ev=ev,
                    rag_context=rag_context,
                    tools=tools,
                    return_mode=return_mode,
                    output_type=output_type,
                    intent=intent,
                    has_active_task=has_active_task,
                    budget_gate=budget_gate,
                    suppress_intermediate_text=suppress_intermediate_text,
                    turn_graph=turn_graph,
                    cheap_gate=cheap_gate,
                )
            except Exception as e:
                err_str = str(e)
                # 自愈：过期远程图片导致的下载失败会让后续每轮都 500，先剥离历史里的 过期远程图片，
                if "download image" in err_str.lower():
                    stripped = _strip_remote_images_from_history(self.history)
                    if stripped:
                        logger.warning(
                            i18n_t(
                                "log.agent.image_download_stripped_expired",
                                stripped=stripped,
                            )
                        )

                # 永久性 4xx（内容审核拦截 / 请求非法等）：重试必复现，直接 fail-fast， 不再消耗剩余重试次数。
                non_retryable = _is_non_retryable_model_error(e)

                # 例外：非内容审核的 4xx 给一次干净历史重试（模型退化畸形请求是随机性的）
                if non_retryable and not client_error_retry_used and _is_retryable_client_error(e):
                    client_error_retry_used = True
                    # 不占常规重试预算：末次 attempt 命中时也真的会重跑（评审修复 F6）
                    total_attempts += 1
                    if self._last_attempt_tool_calls:
                        logger.warning(
                            i18n_t(
                                "log.agent.pydanticai_tools_clean_retry_fail",
                                p0=", ".join(self._last_attempt_tool_calls),
                            )
                        )
                    logger.warning(
                        i18n_t(
                            "log.agent.pydanticai_client_suspected_degraded_fail",
                            e=e,
                        )
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                if attempt < total_attempts and not non_retryable:
                    logger.warning(
                        i18n_t(
                            "log.agent.pydanticai_core_request_attempt_fail",
                            attempt=attempt,
                            max_attempts=max_attempts,
                            retry_delay=retry_delay,
                            e=e,
                        )
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                # 永久性客户端错误是上游对本次输入的明确拒绝（非本服务 bug）：只打一行
                # warning（不刷 traceback），按内容审核 / 其他客户端错误分类记账并返回友好文案。
                if non_retryable:
                    assert isinstance(e, ModelHTTPError)  # 见 _is_non_retryable_model_error
                    if _is_content_rejected(e):
                        logger.warning(
                            i18n_t(
                                "log.agent.pydanticai_refused_process_input",
                                p0=e.status_code,
                                err_str=err_str,
                            )
                        )
                        statistics_manager.record_error(error_type="content_rejected")
                        self._session_logger.log_error("content_rejected", err_str)
                        return _fail(f"{ERROR_RESULT_PREFIX}: {ERROR_CONTENT_REJECTED}")
                    logger.warning(
                        i18n_t(
                            "log.agent.pydanticai_client_fail_retry_err",
                            p0=e.status_code,
                            err_str=err_str,
                        )
                    )
                    statistics_manager.record_error(error_type="client_error")
                    self._session_logger.log_error("client_error", err_str)
                    return _fail(f"{ERROR_RESULT_PREFIX}: {err_str}")

                # 已达最大尝试次数：按异常类型记录统计 + 写 session 日志并返回错误文案
                if isinstance(e, httpx.TimeoutException):
                    logger.warning(i18n_t("log.agent.pydanticai_run_fail_request", e=e))
                    statistics_manager.record_error(error_type="timeout")
                    self._session_logger.log_error("timeout", err_str)
                    return _fail(f"{ERROR_RESULT_PREFIX}: {ERROR_TIMEOUT_TEXT}")
                if isinstance(e, httpx.HTTPError):
                    low = err_str.lower()
                    if "rate" in low or "429" in low or "limit" in low:
                        logger.warning(i18n_t("log.agent.pydanticai_run_fail_rate", e=e))
                        statistics_manager.record_error(error_type="rate_limit")
                        self._session_logger.log_error("rate_limit", err_str)
                    else:
                        logger.warning(i18n_t("log.agent.pydanticai_run_fail_network", e=e))
                        statistics_manager.record_error(error_type="network_error")
                        self._session_logger.log_error("network_error", err_str)
                    return _fail(f"{ERROR_RESULT_PREFIX}: {err_str}")

                logger.error(i18n_t("log.agent.pydanticai_run_fail", e=e))
                logger.exception(i18n_t("log.agent.pydanticai_exception_error_details"))
                if "529" in err_str:
                    statistics_manager.record_error(error_type="api_529_error")
                else:
                    statistics_manager.record_error(error_type="agent_error")
                self._session_logger.log_error("agent_error", err_str)
                return _fail(f"{ERROR_RESULT_PREFIX}: {err_str}")

        # while 至少执行一次循环，正常不可达；兜底也必须闭合 run（评审修复 F6）
        return _fail(f"{ERROR_RESULT_PREFIX}: 未知错误")

    async def _lightweight_text_rewrite(
        self,
        rewrite_message: str,
        *,
        max_tokens: Optional[int] = None,
    ) -> str:
        """无工具单轮 Agent 重写；失败或 SILENCE 返回空串。"""
        cap = max_tokens if max_tokens is not None else self.max_tokens
        if cap is None:
            cap = 1024
        try:
            rewrite_agent = Agent(
                model=self.model,
                system_prompt=self.system_prompt or "你是一个智能助手。",
                model_settings={"max_tokens": cap},
                tools=[],
                toolsets=[],
                retries=0,
                output_type=str,
            )
            rewrite_result = await rewrite_agent.run(
                rewrite_message,
                message_history=[],
                usage_limits=UsageLimits(request_limit=1),
            )
            out = str(rewrite_result.output).strip()
        except Exception as e:
            logger.warning(i18n_t("log.agent.firewall_regeneration_fallback", e=e))
            return ""
        if not out or out in SILENCE_MARKERS:
            return ""
        return out

    async def _ooc_rewrite_and_send(
        self,
        blocked: List[Tuple[str, output_firewall.FirewallHit]],
        bot: Bot,
        ev: Optional[Event],
    ) -> None:
        """出戏命中后的重说闭环：轻量重写一次，产物放行；history 脏文替换。"""
        original = "\n\n".join(text for text, _ in blocked)
        first_hit = blocked[0][1]
        rewrite_message = (
            f"{output_firewall.build_rewrite_warning(first_hit)}\n\n"
            f"【被拦下的原文】\n{original}\n\n"
            "请保持原意、用你的角色口吻重写这段话，直接输出重写后的内容，不要解释。"
        )
        rewritten = await self._lightweight_text_rewrite(rewrite_message)
        if not rewritten:
            rewritten = output_firewall.PERSONA_FALLBACK_TEXT
        if first_hit.category in output_firewall.NEVER_RELEASE_CATEGORIES:
            _user_text = ev.raw_text if ev is not None and ev.raw_text else ""
            _recheck = output_firewall.check_ooc(rewritten, user_text=_user_text)
            if _recheck is not None and _recheck.category in output_firewall.NEVER_RELEASE_CATEGORIES:
                logger.warning(i18n_t("log.agent.firewall_rewrite_output_hit_non"))
                rewritten = output_firewall.PERSONA_FALLBACK_TEXT
        if angle_bracket_guard.has_illegal_angle_tags(rewritten):
            rewritten = (
                angle_bracket_guard.sanitize_illegal_angle_tags(rewritten) or output_firewall.PERSONA_FALLBACK_TEXT
            )
        self._session_logger.log_text_output(rewritten)
        try:
            await send_chat_result(bot, rewritten, ev=ev, ooc_check=False)
            self._run_sent_texts.add(rewritten)
        except Exception as e:
            logger.debug(i18n_t("log.agent.agent_event", e=e))
        self._replace_blocked_text_in_history({text for text, _ in blocked}, rewritten)

    async def _angle_bracket_rewrite_loop(
        self,
        original: str,
        *,
        attempts_already: int,
    ) -> Optional[str]:
        """尖括号：轻量重写直到干净或用尽剩余次数；失败返回 None。"""
        remaining = max(0, angle_bracket_guard.MAX_RETRIES - attempts_already)
        current = original
        token_cap = min(int(self.max_tokens or 1024), 1024)
        for i in range(remaining):
            tags = angle_bracket_guard.find_illegal_angle_tags(current)
            if not tags:
                return current
            rewrite_message = angle_bracket_guard.build_rewrite_warning(tags, current)
            rewritten = await self._lightweight_text_rewrite(
                rewrite_message,
                max_tokens=token_cap,
            )
            if not rewritten:
                continue
            if not angle_bracket_guard.has_illegal_angle_tags(rewritten):
                return rewritten
            current = rewritten
            logger.warning(
                i18n_t(
                    "log.ai.output_gate_angle_rewrite_still_dirty",
                    attempt=attempts_already + i + 1,
                    max_retries=angle_bracket_guard.MAX_RETRIES,
                    preview=repr(rewritten[:80]),
                )
            )
        return None

    def _edit_history_tail(
        self,
        *,
        tail_n: int,
        drop_user_markers: Sequence[str] = (),
        drop_text_parts: Optional[set[str]] = None,
        replace_text_parts: Optional[dict[str, str]] = None,
    ) -> tuple[int, int]:
        """历史尾部外科：删带 marker 的 user turn / 丢或替换脏 TextPart。

        闸门 scrub 与假完成 scrub 共用，避免 tail 窗口与匹配规则漂移。
        """
        if tail_n <= 0 or not self.history:
            return 0, 0
        n = min(tail_n, len(self.history))
        tail = self.history[-n:]
        kept: List[ModelMessage] = []
        removed_nudge = 0
        removed_blocked = 0
        drop_set = drop_text_parts if drop_text_parts is not None else set()
        replace_map = replace_text_parts if replace_text_parts is not None else {}
        for msg in tail:
            if (
                drop_user_markers
                and isinstance(msg, ModelRequest)
                and any(
                    isinstance(p, UserPromptPart)
                    and isinstance(p.content, str)
                    and any(m in p.content for m in drop_user_markers)
                    for p in msg.parts
                )
            ):
                removed_nudge += 1
                continue
            if isinstance(msg, ModelResponse) and (drop_set or replace_map):
                new_parts: List[Any] = []
                changed = False
                for p in msg.parts:
                    if not isinstance(p, TextPart):
                        new_parts.append(p)
                        continue
                    key = p.content.strip()
                    if key in drop_set:
                        changed = True
                        continue
                    if key in replace_map:
                        p.content = replace_map[key]
                        changed = True
                    new_parts.append(p)
                if not new_parts:
                    removed_blocked += 1
                    continue
                if changed:
                    removed_blocked += 1
                    msg.parts = new_parts
            kept.append(msg)
        self.history[-n:] = kept
        return removed_nudge, removed_blocked

    def _scrub_gate_history(
        self,
        blocked_texts: set[str],
        *,
        drop_blocked: bool = True,
    ) -> None:
        """裁掉闸门 nudge 的 user turn；可选移除被拦脏 TextPart。"""
        removed_nudge, removed_blocked = self._edit_history_tail(
            tail_n=12,
            drop_user_markers=output_gate.GATE_NUDGE_MARKERS,
            drop_text_parts=blocked_texts if drop_blocked else None,
        )
        if removed_nudge or removed_blocked:
            logger.warning(
                i18n_t(
                    "log.ai.output_gate_scrubbed_history",
                    nudges=removed_nudge,
                    blocked_msgs=removed_blocked,
                )
            )

    def _replace_blocked_text_in_history(self, blocked: set[str], rewritten: str) -> None:
        """重写成功：历史脏 TextPart 换成干净版。"""
        if not blocked:
            return
        mapping = {b: rewritten for b in blocked}
        self._edit_history_tail(tail_n=len(self.history), replace_text_parts=mapping)

    def _ooc_safe_outbound(self, text: str, ev: Optional[Event]) -> str:
        """angle 收尾产物出站前 OOC 复检（angle 短路可能残留出戏）。"""
        if not text or not output_firewall.is_enabled():
            return text
        user_text = ev.raw_text if ev is not None and ev.raw_text else ""
        hit = output_firewall.check_ooc(text, user_text=user_text)
        if hit is None:
            return text
        if hit.category == "machine_dump":
            return output_firewall.MACHINE_FALLBACK_TEXT
        # never-release 与其它 OOC：收尾单次路径用角色兜底，避免 ooc_check=False 漏放
        return output_firewall.PERSONA_FALLBACK_TEXT

    async def _resolve_output_gate_after_run(
        self,
        context: ToolContext,
        bot: Optional[Bot],
        ev: Optional[Event],
        *,
        return_mode: str,
        ooc_blocked: List[Tuple[str, output_firewall.FirewallHit]],
        ab_abort: bool,
    ) -> bool:
        """尖括号收尾 + OOC 重说。返回是否尖括号熔断静默。"""
        silence_markers = SILENCE_MARKERS
        clean_sent = [
            t
            for t in self._run_sent_texts
            if t and t not in silence_markers and not angle_bracket_guard.has_illegal_angle_tags(t)
        ]
        plan = output_gate.plan_angle_after_run(context.extra, clean_sent=clean_sent)
        angle_fused = ab_abort or plan.fused

        if plan.fused or ab_abort:
            logger.warning(
                i18n_t(
                    "log.ai.output_gate_run_fused",
                    attempts=plan.attempts,
                    session_id=self.session_id,
                )
            )
            self._scrub_gate_history(set(plan.blocked), drop_blocked=True)
            angle_fused = True
        elif plan.replace_map:
            self._edit_history_tail(
                tail_n=len(self.history),
                replace_text_parts=plan.replace_map,
            )
            self._scrub_gate_history(set(), drop_blocked=False)
        elif plan.rewrite_original and bot and return_mode in ["always", "by_bot"]:
            rewritten = await self._angle_bracket_rewrite_loop(
                plan.rewrite_original,
                attempts_already=plan.attempts,
            )
            if rewritten:
                rewritten = self._ooc_safe_outbound(rewritten, ev)
                self._session_logger.log_text_output(rewritten)
                sent_ok = False
                try:
                    await send_chat_result(bot, rewritten, ev=ev, ooc_check=False)
                    self._run_sent_texts.add(rewritten)
                    sent_ok = True
                except Exception as abe:
                    logger.debug(i18n_t("log.ai.output_gate_angle_rewrite_send_fail", e=abe))
                # 仅替换本条 rewrite_original，避免多脏文被同一产物覆盖
                if sent_ok:
                    self._replace_blocked_text_in_history({plan.rewrite_original}, rewritten)
                    self._scrub_gate_history(set(), drop_blocked=False)
            else:
                logger.warning(
                    i18n_t(
                        "log.ai.output_gate_run_fused_post_end",
                        session_id=self.session_id,
                        attempts=plan.attempts,
                    )
                )
                output_gate.set_fused(context.extra, "angle_bracket")
                self._scrub_gate_history(set(plan.blocked), drop_blocked=True)
                angle_fused = True
        elif plan.scrub_nudges:
            self._scrub_gate_history(set(), drop_blocked=False)

        # 尖括号熔断仍恢复独立 OOC 段（与 angle scrub 正交）
        if ooc_blocked and bot and return_mode in ["always", "by_bot"] and not plan.skip_ooc_rewrite:
            await self._ooc_rewrite_and_send(ooc_blocked, bot, ev)
        return angle_fused

    def _scrub_fake_done_history(self, fabricated_texts: set[str]) -> None:
        """假完成收尾：删纠正 nudge 与未发出的编造声明（与闸门 scrub 共用编辑器）。"""
        self._edit_history_tail(
            tail_n=8,
            drop_user_markers=(_FAKE_DONE_NUDGE,),
            drop_text_parts=fabricated_texts,
        )

    async def _execute_run_once(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
        output_type: Optional[type] = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        fake_done_retry: bool = False,
        turn_graph: Optional[Any] = None,
        cheap_gate: Optional[Any] = None,
    ) -> Union[str, Any]:
        """
        实际执行 Agent 运行的内部方法（单次尝试）

        瞬时故障（超时/网络/5xx/529 等）**不在此捕获**，直接向上抛出由
        ``_execute_run`` 统一重试；``UsageLimitExceeded`` 仍在此走专属兜底总结。

        Args:
            output_type: 当指定为某个 Pydantic 模型类时，利用 pydantic_ai 的
                output_type 特性，要求模型必须返回符合该模型结构的 JSON。
                此时返回值为该 Pydantic 模型实例而非字符串。
            budget_gate: 本次 run 是否为预算入口。True 时（巡检 / proactive / 定时等自主
                调用）超额直接早退、绝不花费 Token；交互被动路径已在 handle_ai 提前闸门，
                按默认 False 只记账不二次拦截；在途嵌套子 agent 同样默认 False（只记账）。
            suppress_intermediate_text: True 时抑制工具调用前后的文本片段，只保留最终文本。
            fake_done_retry: 本次是否为假完成闸的纠正重跑（护栏随调用栈传递而非实例状态，
                避免共享 session 并发 run 间互相压制闸门 / 复位遗漏）。
        """
        from gsuid_core.ai_core.statistics import statistics_manager

        # 抑制中间文本的默认值改由 ai_config 决定（网页控制台可改、即时生效，默认 True）；
        # 保留形参供插件显式覆盖：调用方显式传 True 仍强制抑制，故取两者或值。
        _suppress_intermediate_text = suppress_intermediate_text or bool(
            ai_config.get_config("suppress_intermediate_text").data
        )

        # ============ 预算闸门 + scope 解析（统一入口）============
        # 仅 budget_gate=True 的自主入口在此早退；放行/未启用/豁免均零额外开销。
        _budget_scope = self._resolve_budget_scope(ev)
        if budget_gate and _budget_scope is not None:
            try:
                from gsuid_core.ai_core.budget import budget_manager

                _bd = await budget_manager.check_scope(
                    _budget_scope[0], _budget_scope[1], _budget_scope[2], self.session_id
                )
            except SQLAlchemyError as _be:
                logger.warning(i18n_t("log.agent.budget_check_db_fail", _be=_be))
                _bd = None
            except Exception as _be:
                logger.exception(i18n_t("log.agent.budget_check_fail_allowing", _be=_be))
                _bd = None
            if _bd is not None and not _bd.allowed:
                logger.info(
                    i18n_t(
                        "log.agent.budget_exceeded_intercepted_create",
                        p0=self.create_by,
                        p1=_bd.block_scope_label,
                    )
                )
                # 仅交互式（有 bot + ev）时处理用户提示与主人告警；自主后台静默掐断。
                if bot is not None and ev is not None:
                    if _bd.notify and _bd.message:
                        try:
                            await bot.send(_bd.message)
                        except Exception as _se:
                            logger.warning(i18n_t("log.agent.budget_exceeded_notice_se", _se=_se))
                    # 主人告警独立于用户提示：运行层拦截也同步给主人，便于与会话层闸区分开排查
                    await notify_master_of_budget_block(
                        bot=bot,
                        ev=ev,
                        decision=_bd,
                    )
                return None if output_type is not None else ""

        # 提前到 try 前设置归属 scope：使本次 run 期间未显式绑定 scope 的嵌套 LLM 调用（含
        # _prepare_user_message 的图片理解）都按此记账；finally 还原，泄漏至多止于本 task。
        _budget_scope_token = _current_budget_scope.set(_budget_scope) if _budget_scope is not None else None

        _tool_call_list: list[str] = []  # 用于记录本次运行中被调用的工具列表，供后续统计使用
        # 同引用暴露给 _execute_run 的干净重试分支：判断失败前是否已有工具副作用（F14）
        self._last_attempt_tool_calls = _tool_call_list
        _wall_nudged = False  # C-4 墙钟软预算：每 run 至多注入一次收敛提示
        # 出戏防火墙拦下的文本段（§D.4）：iter 结束后走"提醒→重说→放行"闭环
        _ooc_blocked: List[Tuple[str, output_firewall.FirewallHit]] = []
        # 输出闸门：待注入 REWRITE feedback（同 response 可多段合并）；熔断后本轮静默
        _ab_pending_nudges: List[str] = []
        _ab_abort = False
        # 假完成预检暂扣的文本段：声明完成但至今零工具——iter 后按"动作是否真发生"补发或纠正
        _fab_blocked: list[str] = []
        # 本轮是否见过结构化工具返回（用于出图履约闸）
        _saw_structured_return = False
        # 同工具空转计数：连续同名工具调用次数；达阈值后注入 thrash fuse（每 run 一次）
        _same_tool_streak = 0
        _same_tool_name = ""
        _thrash_fused = False
        _thinking_segments: list[str] = []  # 累积本轮模型 thinking 文本，供意图-行为一致性检测
        # A: 被同 Session 更新消息 supersede 时置位，不写 history、不收尾发
        _generation_cancelled = False
        _cancel_ev = self._cancel_generation

        # 使用自定义迭代次数限制（如果有），否则使用配置默认值
        if self.max_iterations is not None:
            limits = UsageLimits(request_limit=self.max_iterations)
        else:
            multi_agent_lenth: int = ai_config.get_config("multi_agent_lenth").data
            limits = UsageLimits(request_limit=multi_agent_lenth)

        # 记录开始时间用于延迟统计
        start_time = time.time()
        # C-4 墙钟时钟：ask_user 等"挂起等人"的时段记进 excluded，判定预算时扣除。
        # token 在 finally 还原，否则嵌套 run（图片理解/subagent）会顶掉本 run 的时钟。
        _wall_clock, _wall_clock_token = wall_clock.install_clock()

        logger.info(i18n_t("log.agent.run_start_started"))
        # turn_id：本轮 run 的唯一标识，写入 ToolContext.extra 供子工具读取（如 scheduler.py 的 add_once_task
        # 单轮节流计数）。回合结束 finally 清理。
        turn_id = uuid.uuid4().hex
        # 交互主人格：专属工具从静态池剥离后，同步写入 blocked，堵住 find_tools 回灌
        _blocked_exclusive: set[str] = (
            _capability_exclusive_tool_names() if self.create_by in _INTERACTIVE_CREATE_BY else set()
        )
        context = ToolContext(
            bot=bot,
            ev=ev,
            # run_sent_texts 同引用透传：send_message_by_ai 等工具内发送路径与主循环
            # 共用同一去重集合，干净历史重试不再重复发送相同文本（评审修复 F14）
            extra={"turn_id": turn_id, "run_sent_texts": self._run_sent_texts},
            parent_session_id=self.session_id,
            blocked_tool_names=_blocked_exclusive,
        )

        # 记录原始用户问题，供后续强制总结使用
        last_user_question: str = ""
        if isinstance(user_message, str):
            last_user_question = user_message.strip()
        elif isinstance(user_message, Sequence):
            # 从 Sequence[UserContent] 中提取纯文本
            last_user_question = "\n".join(item for item in user_message if isinstance(item, str)).strip()

        # 处理用户消息：当传入 Sequence[UserContent] 时，自动处理其中的图片
        if isinstance(user_message, Sequence) and not isinstance(user_message, str):
            final_user_message = await self._prepare_user_message(list(user_message))
        else:
            final_user_message = f"[用户发言]\n{user_message}"

        # history 只存精简 user turn，避免 rag 快照逐轮累积
        _lean_user_message: Union[str, List[UserContent]] = (
            list(final_user_message) if isinstance(final_user_message, list) else final_user_message
        )

        if rag_context:
            final_user_message = _append_user_text(final_user_message, f"\n\n{rag_context}")
            logger.info(i18n_t("log.agent.added_rag_context"))

        # DS 专属角色扮演模式（inner_os）：仅在 Chat 模式首轮 user_message 末尾追加
        if (
            self.create_by == "Chat"
            and not self.history
            and ai_config.get_config("enable_deepseek_rp").data
            and isinstance(final_user_message, str)
        ):
            final_user_message = f"{final_user_message}{INNER_OS_MARKER}"
            logger.info(i18n_t("log.agent.ds_inject"))

        # 连续无工具调用检测：连续两轮只推脱不调工具时注入强制提醒。闲聊类意图豁免（§15）
        # 豁免口径唯一定义在 _PROGRESSIVE_TOOLS_SKIP_INTENTS（评审修复 E12）。
        if (
            self.create_by in ["Chat", "Agent"]
            and self._consecutive_no_tool_rounds >= 2
            and intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS
        ):
            no_tool_reminder = (
                "\n\n【⚠️ 系统检测】你已连续多轮未调用任何工具，"
                "当前用户问题可能尚未得到有效回答。"
                "若你上一轮的思考里明确提到要调用某个工具（如 register_kanban_task、"
                "evaluate_agent_mesh_capability、create_subagent）却没有真正调用——"
                "口头答应 ≠ 执行，请本轮立即调用对应工具。否则请立即检查工具列表，"
                "选择最合适的工具调用，或明确说明为何确实无工具可用——禁止以角色"
                "不懂为由跳过工具。"
            )
            final_user_message = _append_user_text(final_user_message, no_tool_reminder)
            logger.debug(i18n_t("log.agent.forced_nudge_consecutive_turns"))

        # ── 交互脚手架：优先消费入口 TurnGraph；缺省时现场构建 ──
        _addr_gated = False
        _followup_detected = False
        _tg = turn_graph
        _cheap = cheap_gate
        if self.create_by in _INTERACTIVE_CREATE_BY:
            _cur_text = last_user_question
            _probe = ev.raw_text if ev is not None and ev.raw_text else last_user_question
            _is_tome = bool(ev.is_tome) if ev is not None else False
            _recent = interaction_scaffold.recent_history_texts(self.history)
            if _tg is None:
                _spk0 = str(ev.user_id) if ev is not None else ""
                _spk0 = interaction_scaffold.extract_speaker_id(_cur_text) or _spk0
                _ut = "direct"
                if ev is not None:
                    _ut = str(ev.user_type or ("group" if ev.group_id else "direct"))
                _tg = interaction_scaffold.build_turn_graph(
                    _probe or _cur_text,
                    persona_name=self.persona_name or "",
                    is_tome=_is_tome,
                    user_type=_ut,
                    primary_speaker=_spk0,
                    recent=_recent,
                    recent_tool_call=interaction_scaffold.has_recent_tool_call(self.history),
                    followup_max_len=int(ai_config.get_config("scaffold_followup_max_len").data),
                    ambient_max_len=int(ai_config.get_config("scaffold_ambient_max_len").data),
                )
            if _cheap is None:
                _cheap = interaction_scaffold.decide_cheap_gate(
                    _tg, has_active_task=has_active_task, intent=str(intent or "")
                )
            _addr_gated = bool(_tg.address_gated)
            _followup_detected = bool(_tg.needs_task_tools)
            _hints = interaction_scaffold.scaffold_hints_from_graph(_tg, cheap=_cheap)
            # C-2：≥2 且比上轮增加才保留漂移提醒（hints 里可能已有，按计数裁）
            _pushes = _tg.style_push_count
            if interaction_scaffold.DRIFT_REMINDER in _hints:
                if not (_pushes >= 2 and _pushes > self._last_drift_push_count):
                    _hints = [h for h in _hints if h is not interaction_scaffold.DRIFT_REMINDER]
                else:
                    logger.debug(i18n_t("log.agent.scaffold_drift_budget_reminder_inject", _pushes=_pushes))
            self._last_drift_push_count = _pushes
            if _addr_gated:
                logger.info(i18n_t("log.agent.scaffold_addressing_gate_directed_create"))
            elif _tg.ellipsis_followup:
                logger.debug(i18n_t("log.agent.scaffold_ellipsis_style_follow_inject"))
            for _h in _hints:
                final_user_message = _append_user_text(final_user_message, _h)

        # 截断日志输出中的 base64 数据，避免日志过长
        truncated_msg = _truncate_message_for_log(final_user_message)
        logger.trace(i18n_t("log.agent.user_truncated_msg", truncated_msg=truncated_msg))

        # 记录用户输入到 session logger
        self._session_logger.log_run_start()
        self._session_logger.log_user_input(final_user_message)

        if tools is None:
            tools = []

        # 渐进式工具暴露是否在本轮生效（仅自动装配 + 非闲聊轮）。决定是否挂 RetrievableToolset。
        _expose_dynamic = False
        _is_light = _cheap is interaction_scaffold.CheapGate.LIGHT if _cheap is not None else False
        # 媒体句柄（event 字段或正文 img_/图片ID 标注）——通道信号，非话题词
        _probe_for_media = ""
        if isinstance(user_message, str):
            _probe_for_media = user_message
        elif ev is not None and ev.raw_text:
            _probe_for_media = ev.raw_text
        _has_media = interaction_scaffold.message_has_media_handles(
            _probe_for_media,
            image_id_list=getattr(ev, "image_id_list", None) if ev is not None else None,
            image_list=getattr(ev, "image_list", None) if ev is not None else None,
            audio_id=getattr(ev, "audio_id", None) if ev is not None else None,
        )
        # light 与 full 群聊均走瘦保底；light 不再清工具，只是少检索 + 短回 hint
        _group_slim = bool(
            _tg is not None and getattr(_tg, "is_group", False) and self.create_by in _INTERACTIVE_CREATE_BY
        )

        # dynamic 能力族门：显式 True/False 优先；None 沿用旧门（agentic 且未传 tools）。
        if self.dynamic_tools is not None:
            _assemble = self.dynamic_tools
        else:
            _assemble = self.create_by in _AGENTIC_CREATE_BY and not tools

        # persona 会话与其 AgentNode 声明同步：packs 去掉 dynamic 即关闭五层自动装配
        # 改为静态解析 packs + tool_names（与 task-mode 的 runner 同语义）。
        if _assemble and self.dynamic_tools is None and self.persona_name:
            from gsuid_core.ai_core.agent_node import (
                get_node as _get_agent_node,
                has_dynamic_pack,
                resolve_pack_tool_names,
            )

            _pnode = _get_agent_node(self.persona_name)
            if _pnode is not None and not has_dynamic_pack(_pnode.tool_packs):
                _assemble = False
                _static_names = list(dict.fromkeys(resolve_pack_tool_names(_pnode.tool_packs) + _pnode.tool_names))
                _seen_names = {t.name for t in tools}
                for _tn in _static_names:
                    if _tn in _seen_names:
                        continue
                    _tb = find_tool_base(_tn)
                    if _tb is not None:
                        _seen_names.add(_tn)
                        tools.append(_tb.tool)
                logger.debug(
                    i18n_t(
                        "log.agent.persona_declare_dynamic_capability",
                        p0=self.persona_name,
                        p1=len(tools),
                    )
                )

        if _addr_gated:
            # C-3：@别人且未点自己 → 零工具
            tools = []
        elif _assemble or self.create_by in _AGENTIC_CREATE_BY:
            if _assemble:
                qy = ""
                if isinstance(user_message, str):
                    qy = user_message
                elif ev is not None:
                    qy = ev.raw_text

                # 第一层：保底池。群聊（含 light）瘦保底；私聊/能力代理仍全量。
                if _group_slim or _is_light:
                    core_tools = []
                    core_names: set[str] = set()
                    for _tn in interaction_scaffold.SLIM_GROUP_CORE_TOOLS:
                        _tb = find_tool_base(_tn)
                        if _tb is not None and _tn not in core_names:
                            core_names.add(_tn)
                            core_tools.append(_tb.tool)
                else:
                    core_tools = await get_main_agent_tools()
                    core_names = {t.name for t in core_tools}

                # 调用方显式传入的基础工具（dynamic 节点的 packs+白名单）并入保底
                for _bt in tools:
                    if _bt.name not in core_names:
                        core_names.add(_bt.name)
                        core_tools.append(_bt)

                # 节点显式白名单：persona 投影节点在 config.json 声明的 tool_names 并入保底
                if self.persona_name and not _group_slim:
                    from gsuid_core.ai_core.agent_node import get_node as _get_agent_node

                    _node = _get_agent_node(self.persona_name)
                    if _node is not None and _node.tool_names:
                        for _tn in _node.tool_names:
                            if _tn in core_names:
                                continue
                            _tb = find_tool_base(_tn)
                            if _tb is not None:
                                core_names.add(_tn)
                                core_tools.append(_tb.tool)

                # 第 1.5 层：状态驱动工具池（L2）
                try:
                    from gsuid_core.ai_core.tool_state_signals import get_state_driven_family_tools

                    state_tools = await get_state_driven_family_tools(
                        ev, core_names, has_active_task=has_active_task, intent=intent
                    )
                    if state_tools:
                        core_tools = core_tools + state_tools
                        core_names.update(t.name for t in state_tools)
                except Exception as e:
                    logger.debug(i18n_t("log.agent.load_state_driven_pool", e=e))

                # C-1 / TurnGraph：任务管理或省略跟进 → 补调度族
                if _followup_detected:
                    for _dom in ("定时任务", "长期任务编排"):
                        for _tb in get_tools_by_capability_domain(_dom):
                            if _tb.name not in core_names:
                                core_names.add(_tb.name)
                                core_tools.append(_tb.tool)
                    logger.debug(i18n_t("log.agent.scaffold_supplemented_scheduled_task"))

                # 第 1.6 层：会话驻留工具池（L3）
                if self._recent_tool_families:
                    for _dom, _ttl in list(self._recent_tool_families.items()):
                        if _ttl <= 0:
                            continue
                        for _tb in get_tools_by_capability_domain(_dom):
                            if _tb.name not in core_names:
                                core_names.add(_tb.name)
                                core_tools.append(_tb.tool)
                    self._recent_tool_families = {
                        _d: _t - 1 for _d, _t in self._recent_tool_families.items() if _t - 1 > 0
                    }

                # 附加工具池 = 语境工具池 + 查询工具池
                extra_tools: ToolList = []

                # 第二层：语境工具池（群聊瘦模式也保留标签池，上限更紧）
                ctx_tags: list[str] = []
                if ev is not None and ev.group_id:
                    try:
                        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

                        scope_key = make_scope_key(ScopeType.GROUP, str(ev.group_id))
                        ctx_tags = await get_scope_context_tags(scope_key)
                        if ctx_tags:
                            _ctx_max = 4 if _group_slim else 8
                            ctx_tools = get_tools_by_context_tags(ctx_tags, max_count=_ctx_max)
                            if ctx_tools:
                                extra_tools += ctx_tools
                                logger.debug(
                                    i18n_t(
                                        "log.agent.contextual_pool_context_tags",
                                        p0=len(ctx_tools),
                                        ctx_tags=ctx_tags,
                                    )
                                )
                    except Exception as e:
                        logger.debug(i18n_t("log.agent.load_contextual_pool", e=e))

                # 第三层：向量检索。light 或群聊纯闲聊可跳过（保底已含搜/图/渲/调度入口）。
                # soft_continue / ellipsis 与呼叫跟进同权：不得因 intent=闲聊 跳过检索。
                _recall_limit = int(ai_config.get_config("tool_search_recall").data)
                max_extra_tools: int = int(ai_config.get_config("tool_extra_pool_max").data)
                _soft_cont = bool(_tg.soft_continue) if _tg is not None else False
                _ellip = bool(_tg.ellipsis_followup) if _tg is not None else False
                _skip_search = _is_light or (
                    _group_slim
                    and intent == "闲聊"
                    and not _followup_detected
                    and not has_active_task
                    and not _has_media
                    and not _ellip
                    and not _soft_cont
                )
                if (
                    intent == "闲聊"
                    and not _followup_detected
                    and not has_active_task
                    and not self._recent_tool_families
                ):
                    _recall_limit = max(2, _recall_limit // 2)
                    max_extra_tools = max(3, max_extra_tools // 2)
                if _group_slim or _is_light:
                    max_extra_tools = min(max_extra_tools, 6)
                if qy and not _skip_search:
                    search_query = interaction_scaffold.build_tool_search_query(
                        qy,
                        self._recent_user_texts,
                        ctx_tags,
                    )
                    logger.debug(i18n_t("log.agent.attempting_search_tools_query", search_query=search_query))

                    extra_tools += await search_tools_with_entity_routing(
                        query=search_query,
                        route_text=qy,
                        limit=_recall_limit,
                        non_category=["self", "buildin"],
                    )
                    # 补搜索族（瘦保底已含 web_search_tool；再补 fetch/knowledge）
                    if (_group_slim or _is_light) and intent in ("工具", "问答"):
                        for _tn in ("web_fetch_tool", "search_knowledge"):
                            if _tn in core_names:
                                continue
                            _tb = find_tool_base(_tn)
                            if _tb is not None:
                                core_names.add(_tn)
                                core_tools.append(_tb.tool)

                # 附加池：先按能力族整族展开（L4），再去重/限量。 召回族内任一工具即带出整族（剔除与保底重名/族内重复）
                deduped_extra = expand_tools_to_families(
                    extra_tools,
                    exclude_names=core_names,
                    max_tools=max_extra_tools,
                )

                # 召回族也写进 L3 驻留：下一轮并入稳定保底段，工具集随对话收敛，
                # provider 前缀缓存命中↑、跨轮追问免重检索（§cache 54%→更高）。
                for _et in deduped_extra:
                    _etb = find_tool_base(_et.name)
                    _edom = _etb.capability_domain if _etb is not None else None
                    if _edom:
                        self._recent_tool_families[_edom] = _STICKY_FAMILY_TURNS

                # §25(3) 工具序稳定化：两段各自按名排序，
                core_tools.sort(key=lambda _t: _t.name)
                deduped_extra.sort(key=lambda _t: _t.name)
                tools = core_tools + deduped_extra

                # 委派：剥离能力代理专属工具，逼主人格走 create_subagent
                _did_strip_exclusive = False
                if self.create_by in _INTERACTIVE_CREATE_BY:
                    _exclusive = _capability_exclusive_tool_names()
                    if _exclusive:
                        _before = {t.name for t in tools}
                        _stripped = _before & _exclusive
                        if _stripped:
                            tools = [t for t in tools if t.name not in _exclusive]
                            _did_strip_exclusive = True
                            logger.info(
                                i18n_t(
                                    "log.agent.main_persona_stripped_capability",
                                    n=len(_stripped),
                                    names=sorted(_stripped)[:12],
                                )
                            )

                _need_subagent = _did_strip_exclusive
                deleg_pid = ""
                if qy:
                    deleg_pid = _matched_delegation_only_profile(qy)
                    if deleg_pid:
                        _need_subagent = True
                    elif not _need_subagent:
                        _pool_names = {t.name for t in tools}
                        _domain_pid = _pool_overlaps_capability_agent(_pool_names)
                        if _domain_pid:
                            _need_subagent = True
                            deleg_pid = _domain_pid
                if _need_subagent and not any(t.name == "create_subagent" for t in tools):
                    cs = find_tool_base("create_subagent")
                    if cs is not None:
                        tools.append(cs.tool)
                        logger.debug(
                            i18n_t(
                                "log.agent.delegation_safeguard_create_subagent",
                                deleg_pid=deleg_pid or "exclusive_strip",
                            )
                        )

                # 渐进式工具暴露：常挂 find_tools + RetrievableToolset（含误判闲聊轮）。
                if ENABLE_PROGRESSIVE_TOOLS:
                    if any(t.name == "find_tools" for t in tools):
                        _expose_dynamic = True
                    else:
                        ft = find_tool_base("find_tools")
                        if ft is not None:
                            tools.append(ft.tool)
                            _expose_dynamic = True
                    if _expose_dynamic:
                        logger.debug(i18n_t("log.agent.find_tools_progressive_exposure"))

                logger.debug(
                    i18n_t(
                        "log.agent.tool_count_baseline_extra",
                        p0=len(tools),
                        p1=len(core_tools),
                        p2=len(deduped_extra),
                    )
                )

                # L5：记录本轮用户原话，供下一轮上下文增强检索（保留窗口内的"上文"）
                if qy:
                    _text_window: int = ai_config.get_config("tool_context_window").data
                    keep = max(_text_window - 1, 0)
                    self._recent_user_texts.append(qy)
                    self._recent_user_texts = self._recent_user_texts[-keep:] if keep else []
            else:
                logger.debug(i18n_t("log.agent.passed_tools_list_arguments", p0=len(tools)))
        else:
            logger.debug(i18n_t("log.agent.skip_tool_search_searching_tools"))

        logger.debug(i18n_t("log.agent.tool_list", p0=[tool.name for tool in tools]))

        # 最终去重（兼容外部直接传入 tools 的情况）
        tools = list({obj.name: obj for obj in tools}.values())
        tool_names = [t.name for t in tools]

        # 回填本轮装配工具的能力域，供 handle_ai 偏好注入精确过滤（"装配后回传"）： 把工具名映射回 capability_domain
        # handle_ai 据此只注入本轮可用工具相关的软偏好。
        assembled_domains: set[str] = set()
        for _tn in tool_names:
            _tb = find_tool_base(_tn)
            if _tb is not None and _tb.capability_domain:
                assembled_domains.add(_tb.capability_domain)
        self._last_assembled_domains = assembled_domains

        # 能力代理花名册已固化进 session system_prompt（可缓存），不再每轮塞 user 侧。

        # 记录本次传给 AI 的工具列表
        self._session_logger.log_tools_list(tool_names)

        # 当 return_model 指定时，使用 output_type 让 pydantic_ai 强制结构化输出
        # output_type 默认为 str（返回文本），指定 Pydantic 模型时强制返回结构化 JSON
        _toolsets = [skills_toolset] if self.create_by in _SKILLS_CREATE_BY and not _addr_gated else []
        # 启用渐进式暴露时挂 RetrievableToolset：每个 step 读 dynamic_tool_names 即时暴露命中工具。
        # exclude_names：静态池 + 能力代理专属（防 find_tools 把已剥离工具回灌主人格）。
        if _expose_dynamic:
            _dyn_exclude = set(tool_names) | set(context.blocked_tool_names)
            _toolsets = [*_toolsets, RetrievableToolset(exclude_names=_dyn_exclude)]
        # eval_mode 下固定 temperature=0：记忆评测的答案须可复现，
        from gsuid_core.ai_core.memory.config import memory_config

        if self.model:
            _model_settings = self.model.settings
            if memory_config.eval_mode and _model_settings:
                _model_settings["temperature"] = 0.0
        else:
            _model_settings = None

        _agent = Agent(
            model=self.model,
            deps_type=ToolContext,
            system_prompt=self.system_prompt or "你是一个智能助手, 简短的一句话回答问题即可。",
            model_settings=_model_settings,
            tools=tools,
            toolsets=_toolsets,
            retries=3,
            output_type=output_type or str,
        )

        # 截断历史记录，避免无限制增长
        self.extract_history()

        # TTFT/TPS 流式统计：按"每次模型请求"打点，在对应 CallToolsNode 中结算入库。
        # _req_start 在 ModelRequestNode 发起前记录；_first/_last_event_at 由
        _req_start: float = 0.0
        _first_event_at: Optional[float] = None
        _last_event_at: Optional[float] = None
        _model_name: str = self.model.model_name if self.model else "unknown"
        _provider: str = self.model.system if self.model else "unknown"
        # 流式响应下需手动按完整文本重新拆分内嵌 <think> 标签（见 _split_embedded_thinking）。
        # thinking_tags 取自模型 profile，默认 ('<think>','</think>')。
        _thinking_tags: tuple[str, str] = ("think", "think")
        if self.model is not None:
            _profile_obj = self.model.profile
            if isinstance(_profile_obj, dict):
                _profile_thinking_tags = _profile_obj.get("thinking_tags")
                if _profile_thinking_tags is not None:
                    _thinking_tags = _profile_thinking_tags
            else:
                logger.error(
                    i18n_t(
                        "log.agent.abnormal_profile_type_forensics",
                        p0=type(_profile_obj).__name__,
                        p1=_model_name,
                        p2=repr(_profile_obj)[:300],
                    )
                )

        try:
            logger.info(i18n_t("log.agent.iter_start"))
            logger.info(i18n_t("log.agent.current_history", p0=len(self.history)))

            async with _agent.iter(
                final_user_message,
                deps=context,  # type: ignore[arg-type]
                message_history=self.history,
                usage_limits=limits,
            ) as agent_run:
                # 遍历每一步 Node
                async for node in agent_run:
                    # A: 节点间隙检查抢答取消（后到消息已请求 abort）
                    if _cancel_ev.is_set():
                        _generation_cancelled = True
                        logger.info(i18n_t("log.agent.generation_cancelled_supersede"))
                        break
                    # 1. 发起大模型请求前的处理
                    if isinstance(node, ModelRequestNode):
                        logger.debug(i18n_t("log.agent.trigger_node_modelrequestnode"))

                        self._session_logger.log_node_transition("ModelRequestNode")

                        # C-4 墙钟软预算：交互式 run 超时后，请求前注入收敛提示（只注入一次），
                        _wall_budget = (
                            self.wall_clock_budget
                            if self.wall_clock_budget is not None
                            else float(ai_config.get_config("scaffold_wall_clock_budget").data)
                        )
                        _wall_elapsed = time.time() - start_time - wall_clock.excluded_seconds(_wall_clock)
                        if (
                            not _wall_nudged
                            and _wall_budget > 0
                            and self.create_by in _INTERACTIVE_CREATE_BY
                            and _wall_elapsed > _wall_budget
                        ):
                            node.request.parts = [*node.request.parts, UserPromptPart(content=_WALL_CLOCK_NUDGE)]
                            _wall_nudged = True
                            logger.info(
                                i18n_t(
                                    "log.agent.wall_clock_soft_budget",
                                    p0=_wall_elapsed,
                                )
                            )

                        # 输出闸门：上一轮 REWRITE feedback（多段已合并）注入下一轮请求
                        if _ab_pending_nudges:
                            _nudge_body = output_gate.merge_rewrite_feedbacks(_ab_pending_nudges)
                            node.request.parts = [
                                *node.request.parts,
                                UserPromptPart(content=_nudge_body),
                            ]
                            logger.warning(i18n_t("log.ai.output_gate_injected_rewrite_feedback"))
                            _ab_pending_nudges = []
                        # 熔断提示只注入一次（与 thrash fuse 同形）
                        if (output_gate.is_fused(context.extra) or _ab_abort) and not output_gate.fuse_already_injected(
                            context.extra
                        ):
                            _ab_abort = True
                            output_gate.mark_fuse_injected(context.extra)
                            node.request.parts = [
                                *node.request.parts,
                                UserPromptPart(content=angle_bracket_guard.build_fuse_warning()),
                            ]

                        # 同工具空转熔断：连续同名工具 ≥ 阈值后，下一轮模型请求前注入一次收敛提示
                        if (
                            not _thrash_fused
                            and _same_tool_streak >= _THRASH_SAME_TOOL_LIMIT
                            and self.create_by in _INTERACTIVE_CREATE_BY
                        ):
                            node.request.parts = [*node.request.parts, UserPromptPart(content=_THRASH_FUSE_NUDGE)]
                            _thrash_fused = True
                            logger.warning(
                                i18n_t(
                                    "log.agent.tool_thrash_fuse",
                                    tool_name=_same_tool_name,
                                    streak=_same_tool_streak,
                                )
                            )

                        _has_tool_return = False
                        for part in node.request.parts:
                            if isinstance(part, ToolReturnPart):
                                _has_tool_return = True
                                # 如果工具返回b64图片或者bytes内容, 则调用RM实例上传
                                if (
                                    isinstance(part.content, str) and part.content.startswith("base64://")
                                ) or isinstance(part.content, bytes):
                                    resource_id = RM.register(part.content)
                                    logger.info(
                                        i18n_t(
                                            "log.agent.content_registered_resource_id",
                                            p0=part.tool_name,
                                            resource_id=resource_id,
                                        )
                                    )
                                    # v2.0: ToolReturnPart.content 在标注中是 str|Any,
                                    # 仅 ToolReturnPart 分支, 其他 part 类型跳过替换。
                                    if type(part) is ToolReturnPart:
                                        # 工具返回过长时写入短占位，避免污染上下文
                                        part.content = (
                                            f"[工具 {part.tool_name} 已生成内容, 但未发送给用户, 资源ID: {resource_id}]"
                                        )

                                # 交互主人格：技术 dump / 高密度 JSON 当轮折叠，防机器腔 OOC
                                if (
                                    self.create_by in _INTERACTIVE_CREATE_BY
                                    and type(part) is ToolReturnPart
                                    and isinstance(part.content, str)
                                ):
                                    if output_firewall.is_tech_dump(part.content):
                                        part.content = output_firewall.TECH_DUMP_TOOL_SHIELD
                                    else:
                                        from gsuid_core.ai_core.utils import (
                                            _summarize_structured_data,
                                            _looks_like_structured_data,
                                        )

                                        if _looks_like_structured_data(part.content):
                                            _saw_structured_return = True
                                            part.content = (
                                                _summarize_structured_data(part.content)
                                                + "\n（结构数据已折叠。综合分析请 create_subagent；"
                                                "多项数据用 render_html_to_image 出图，勿在台词复述原文。）"
                                            )
                                        else:
                                            # 搜索/拉取类：多行或够长 → 视为可出图材料（形状信号，非话题词）
                                            _tn_l = (part.tool_name or "").lower()
                                            _blob = part.content
                                            if any(h in _tn_l for h in _SEARCHISH_TOOL_HINTS) and (
                                                _blob.count("\n") >= 3 or len(_blob) >= 400
                                            ):
                                                _saw_structured_return = True

                                # 返回的可能是对象也可能是字符串，这里为了打印转成 str
                                tool_result_str = str(part.content)
                                if len(tool_result_str) > 200:
                                    tool_result_str = tool_result_str[:200] + f"...[截断, 共{len(tool_result_str)}字符]"
                                logger.debug(
                                    i18n_t(
                                        "log.agent.tool_execution_compl_name_result_passed_ok",
                                        p0=part.tool_name,
                                        tool_result_str=tool_result_str,
                                    )
                                )
                                self._session_logger.log_tool_return(part.tool_name, part.content, part.tool_call_id)

                        # 事件驱动输出契约：本轮已有工具返回（主人格出图 / 能力代理事实包）
                        if _has_tool_return and self.create_by in _INTERACTIVE_CREATE_BY:
                            _any_fail = False
                            for _p in node.request.parts:
                                if type(_p) is ToolReturnPart and _tool_return_looks_failed(_p):
                                    _any_fail = True
                                    break
                            _ok_c, _fail_c = _post_tool_contracts_for(self.create_by)
                            _contract = _fail_c if _any_fail else _ok_c
                            if not any(
                                isinstance(p, UserPromptPart)
                                and p.content
                                in (
                                    _POST_TOOL_OUTPUT_CONTRACT,
                                    _POST_TOOL_FAIL_CONTRACT,
                                    _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
                                    _POST_TOOL_FAIL_CONTRACT_CAPABILITY,
                                )
                                for p in node.request.parts
                            ):
                                node.request.parts = [
                                    *node.request.parts,
                                    UserPromptPart(content=_contract),
                                ]

                        logger.debug(i18n_t("log.agent.sending_request_waiting_think_send"))
                        # 以流式方式发起本轮模型请求并逐 event 打点： 普通的节点迭代走非流式请求，
                        _req_start = time.perf_counter()
                        _first_event_at = None
                        _last_event_at = None
                        async with node.stream(agent_run.ctx) as request_stream:
                            async for _event in request_stream:
                                _last_event_at = time.perf_counter()
                                if _first_event_at is None:
                                    _first_event_at = _last_event_at

                    # 2. 获取到大模型响应，准备调用工具或者输出文本 这里使用了 isinstance
                    # Pyright 就能明确知道此时 node 是 CallToolsNode 拥有 model_response 属性
                    elif isinstance(node, CallToolsNode):
                        logger.debug(i18n_t("log.agent.trigger_node_calltoolsnode"))

                        self._session_logger.log_node_transition("CallToolsNode")

                        # 流式请求下 pydantic_ai 未必能拆出内嵌 <think> 标签（仅当标签作为 独立 SSE chunk
                        node.model_response.parts = _split_embedded_thinking(node.model_response.parts, _thinking_tags)
                        # 紧接着清除文本里泄漏的工具调用标记残留（弱模型 / 兼容网关常把工具
                        # 调用以文本标签输出而非结构化 function calling），整体替换保持三处一致。
                        node.model_response.parts = _sanitize_tool_call_artifacts_in_parts(node.model_response.parts)
                        # 规范化工具参数（去重复键）：防退化参数串回放时被网关 400（§12.22 事故 #2）
                        node.model_response.parts = _canonicalize_tool_call_args_in_parts(node.model_response.parts)

                        # 熔断：单次响应工具调用数上限，防弱模型批量幻觉
                        _MAX_TOOL_CALLS_PER_RESPONSE = 30
                        _tc_count = sum(1 for p in node.model_response.parts if isinstance(p, ToolCallPart))
                        if _tc_count > _MAX_TOOL_CALLS_PER_RESPONSE:
                            logger.warning(
                                i18n_t(
                                    "log.agent.tool_calls_per_response_truncate",
                                    count=_tc_count,
                                    limit=_MAX_TOOL_CALLS_PER_RESPONSE,
                                )
                            )
                            _kept: list = []
                            _tc_kept = 0
                            for _p in node.model_response.parts:
                                if isinstance(_p, ToolCallPart):
                                    _tc_kept += 1
                                    if _tc_kept > _MAX_TOOL_CALLS_PER_RESPONSE:
                                        continue
                                _kept.append(_p)
                            node.model_response.parts = _kept

                        # thrash fuse 后：若仍连打同一工具，直接从本响应剥掉，逼模型换路或收束
                        if _thrash_fused and _same_tool_name and self.create_by in _INTERACTIVE_CREATE_BY:
                            _stripped = [
                                _p
                                for _p in node.model_response.parts
                                if not (isinstance(_p, ToolCallPart) and _p.tool_name == _same_tool_name)
                            ]
                            if len(_stripped) < len(node.model_response.parts):
                                logger.warning(
                                    i18n_t(
                                        "log.agent.tool_thrash_strip_duplicate",
                                        tool_name=_same_tool_name,
                                    )
                                )
                                node.model_response.parts = _stripped

                        # 遍历大模型返回的具体片段 (Parts)
                        # 本轮是否已出现工具调用：用于 suppress_intermediate_text 时判断
                        _saw_tool_call_this_turn = False
                        # 同 ModelResponse 多 TextPart：尖括号 attempt 只计 1 次
                        output_gate.begin_response_batch(context.extra)
                        _ab_attempt_counted_this_response = False
                        # thrash：同响应内工具名列表，结束本响应后一次性按「轮」更新 streak
                        _resp_tool_names: list[str] = []
                        for part in node.model_response.parts:
                            # 拦截到模型即将调用工具
                            if isinstance(part, ToolCallPart):
                                _saw_tool_call_this_turn = True
                                logger.debug(
                                    i18n_t(
                                        "log.agent.llm_requests_tool_name_args",
                                        p0=part.tool_name,
                                        p1=part.args,
                                    )
                                )
                                _tool_call_list.append(part.tool_name)
                                _resp_tool_names.append(part.tool_name)
                                self._session_logger.log_tool_call(part.tool_name, part.args, part.tool_call_id)
                                self._emit_trace("tool", f"{part.tool_name}|{part.args_as_json_str()}")

                                # 程序性记忆（默认开；关闭时零影响）：记一笔工具调用轨迹，
                                try:
                                    from gsuid_core.ai_core.memory.config import memory_config as _mem_cfg

                                    if _mem_cfg.enable_preference_memory and ev is not None:
                                        from gsuid_core.ai_core.memory.ingestion.tool_trace import record_tool_call

                                        record_tool_call(str(ev.user_id), part.tool_name, part.args)
                                except Exception:
                                    pass

                            # 大模型直接输出文本
                            elif isinstance(part, TextPart):
                                _text = part.content.strip()
                                # 拆出 <think> 后只剩空白的文本片段（如纯思考+工具调用轮）， 既无需打印也无需下发，
                                if not _text:
                                    continue
                                logger.debug(i18n_t("log.agent.llm_text", _text=_text))
                                self._session_logger.log_text_output(_text)
                                if _text in SILENCE_MARKERS:
                                    logger.info(i18n_t("log.agent.silent_skipping_text", _text=_text))
                                elif _text in self._run_sent_texts:
                                    # 本轮已发过完全相同的段：模型跨轮重复最终答复 / 重试重发，
                                    logger.debug(i18n_t("log.agent.skipping_duplicate", p0=repr(_text[:40])))
                                elif _suppress_intermediate_text and _saw_tool_call_this_turn:
                                    # 工具调用前后伴随的文本属于中间步骤碎碎念，不发送给用户，
                                    logger.debug(i18n_t("log.agent.suppressing_intermediate_text", p0=repr(_text[:40])))
                                elif bot and _text and return_mode in ["always", "by_bot"]:
                                    # 统一输出闸门；同 response 尖括号只计一次 attempt
                                    _user_raw = ev.raw_text if ev is not None and ev.raw_text else ""
                                    _count_ab = not _ab_attempt_counted_this_response
                                    _gr = output_gate.pre_send_gate(
                                        _text,
                                        context.extra,
                                        user_text=_user_raw,
                                        channel="main",
                                        count_attempt=_count_ab,
                                    )
                                    if _gr.decision is output_gate.GateDecision.FUSE:
                                        _ab_abort = True
                                        _ab_attempt_counted_this_response = True
                                        logger.warning(
                                            i18n_t(
                                                "log.ai.output_gate_drop_text_after_fuse",
                                                policy=_gr.policy,
                                                preview=repr(_text[:80]),
                                            )
                                        )
                                        continue
                                    if _gr.decision is output_gate.GateDecision.REWRITE:
                                        if _gr.defer_ooc and _gr.ooc_hit is not None:
                                            logger.warning(
                                                i18n_t(
                                                    "log.agent.firewall_main_output_hit_ooc",
                                                    p0=_gr.ooc_hit.category,
                                                    p1=_gr.ooc_hit.matched,
                                                )
                                            )
                                            _ooc_blocked.append((_text, _gr.ooc_hit))
                                        else:
                                            if _gr.policy == "angle_bracket":
                                                _ab_attempt_counted_this_response = True
                                            if _gr.feedback:
                                                _ab_pending_nudges.append(_gr.feedback)
                                            if _gr.fused:
                                                _ab_abort = True
                                        continue
                                    if _gr.decision is output_gate.GateDecision.FALLBACK:
                                        _fb = _gr.send_text or output_firewall.MACHINE_FALLBACK_TEXT
                                        try:
                                            await send_chat_result(bot, _fb, ev=ev, ooc_check=False)
                                            self._run_sent_texts.add(_fb)
                                        except Exception as _me:
                                            logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=_me))
                                        continue
                                    # 假完成预检（结构判据：完成声明 + 本轮至今零工具调用）：
                                    _fab_gate_on = not fake_done_retry and not _tool_call_list and bool(tool_names)
                                    if _fab_gate_on and _claims_fake_done(_text):
                                        logger.warning(
                                            i18n_t("log.agent.fakedone_zero_claim_pending_ok", p0=repr(_text[:40]))
                                        )
                                        _fab_blocked.append(_text)
                                        continue
                                    # Why: send_chat_result 抛异常会穿透 _agent.iter() 的 async context 触发
                                    # athrow/cancel scope
                                    try:
                                        await send_chat_result(bot, _text, ev=ev)
                                        # 发送成功才登记去重：发送失败的段允许后续相同输出补发。
                                        self._run_sent_texts.add(_text)
                                    except Exception as _e:
                                        logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=_e))

                            elif isinstance(part, ThinkingPart):
                                _thinking = part.content.strip()
                                logger.debug(i18n_t("log.agent.llm_thinking", _thinking=_thinking))
                                if _thinking:
                                    _thinking_segments.append(_thinking)
                                self._session_logger.log_thinking(_thinking)
                                self._emit_trace("thinking", _thinking)

                        # thrash：本响应只按「轮」计 1 次（并行多 query 不累加）
                        _same_tool_name, _same_tool_streak = _update_thrash_streak_for_response(
                            _resp_tool_names,
                            prev_name=_same_tool_name,
                            prev_streak=_same_tool_streak,
                        )

                        # 结算本轮模型请求的性能统计： TTFT = 请求发起 → 首个流式 event；
                        _ttft_ms: float = 0.0
                        _tps: float = 0.0
                        _req_usage = node.model_response.usage
                        if _first_event_at is not None and _last_event_at is not None:
                            _ttft_ms = round((_first_event_at - _req_start) * 1000, 2)
                            _generation_time = _last_event_at - _first_event_at
                            if _req_usage.output_tokens > 0 and _generation_time > 0:
                                _tps = round(_req_usage.output_tokens / _generation_time, 2)
                            logger.debug(i18n_t("log.ai_agent.ttft_ms_tps_tokens_ok", ttft_ms=_ttft_ms, tps=_tps))
                        statistics_manager.record_hourly_performance(
                            provider=_provider,
                            model_name=_model_name,
                            ttft_ms=_ttft_ms,
                            tps=_tps,
                            input_tokens=_req_usage.input_tokens,
                            output_tokens=_req_usage.output_tokens,
                            cache_read_tokens=_req_usage.cache_read_tokens,
                            cache_write_tokens=_req_usage.cache_write_tokens,
                            tool_call_count=sum(1 for p in node.model_response.parts if isinstance(p, ToolCallPart)),
                        )
                        # 复位打点，避免异常路径下两轮请求的数据串台
                        _first_event_at = None
                        _last_event_at = None

                    # 3. 运行结束节点
                    elif isinstance(node, End):
                        logger.debug(i18n_t("log.agent.node_trigger_end"))
                        logger.debug(i18n_t("log.agent.run_ended_final_result_generated"))
                        self._session_logger.log_node_transition("End")

            # A: 被 supersede 打断 → 不写 history、不 OOC 重说，让后到 run 用完整上下文重生成
            if _generation_cancelled:
                logger.info(i18n_t("log.agent.generation_aborted_no_history"))
                return "" if output_type is None else None

            # 遍历完成后，直接从 agent_run 中获取最终结果
            result = agent_run.result
            if result:
                logger.info(i18n_t("log.agent.iter_ok"))

                # 存 history 前把本轮 user turn 的 content 换成精简版（剥离 rag_context）
                # 防止 [历史对话]/记忆/群语境快照逐轮累积膨胀 input 并冲淡缓存（§优化 O-1）。
                _new_msgs = result.new_messages()
                _relean_user_turn(
                    _new_msgs,
                    _lean_user_message,
                    strip_hint_texts=(_WALL_CLOCK_NUDGE, *output_gate.GATE_NUDGE_MARKERS),
                )
                # 超长工具返回截断为头+尾摘要（§25(5)）：本轮已消费完整返回，历史无需原文
                _truncate_tool_returns_in_history(_new_msgs)
                self.history.extend(_new_msgs)

                # 输出闸门收尾：尖括号熔断/补写/scrub；熔断后仍做独立 OOC 重说
                _ab_abort = await self._resolve_output_gate_after_run(
                    context,
                    bot,
                    ev,
                    return_mode=return_mode,
                    ooc_blocked=_ooc_blocked,
                    ab_abort=_ab_abort or output_gate.is_fused(context.extra),
                )

                # L3：记录本轮实际调用过的工具所属能力族，使其在随后数轮继续常驻，
                if _tool_call_list:
                    for _tname in set(_tool_call_list):
                        _tb = find_tool_base(_tname)
                        _dom = _tb.capability_domain if _tb else None
                        if _dom:
                            self._recent_tool_families[_dom] = _STICKY_FAMILY_TURNS

                # 更新连续无工具调用计数（仅对交互式主 Agent 生效）。闲聊类意图不计数（§15）
                # 豁免口径与注入门同源：_PROGRESSIVE_TOOLS_SKIP_INTENTS（评审修复 E12）。
                if self.create_by in ["Chat", "Agent"] and intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS:
                    if _tool_call_list:
                        self._consecutive_no_tool_rounds = 0
                    else:
                        self._consecutive_no_tool_rounds += 1
                        # 意图-行为不一致检测（结构化）：thinking 里提到了本轮
                        # 已装配的工具名却没真正调用——顶到阈值，下轮强制提醒。
                        thinking_blob = "\n".join(_thinking_segments)
                        if thinking_blob and tool_names and any(tn in thinking_blob for tn in tool_names):
                            self._consecutive_no_tool_rounds = max(self._consecutive_no_tool_rounds, 2)
                            logger.debug(i18n_t("log.agent.intent_action_mismatch_force"))

                # 记录 Token 使用量和延迟统计
                # 记录响应延迟
                latency = time.time() - start_time
                statistics_manager.record_latency(latency=latency)

                try:
                    # v2: result.usage / result.timestamp 由方法改为属性
                    usage_obj: RunUsage = result.usage
                    input_tokens: int = usage_obj.input_tokens
                    output_tokens: int = usage_obj.output_tokens
                    cache_read_tokens: int = usage_obj.cache_read_tokens
                    cache_write_tokens: int = usage_obj.cache_write_tokens

                    logger.info(
                        i18n_t(
                            "log.agent.token_usage_input_tokens",
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            cache_write_tokens=cache_write_tokens,
                        )
                    )

                    # 小时级性能统计（TTFT/TPS）已在每轮 CallToolsNode 中按请求结算,
                    # 此处只记录 run 级的 Token 汇总
                    if input_tokens > 0 or output_tokens > 0:
                        statistics_manager.record_token_usage(
                            model_name=_model_name,
                            chat_type=self.create_by,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            cache_write_tokens=cache_write_tokens,
                        )
                        # 预算记账：可归属 scope 的 run 计入对应 Session 额度，无 scope 只进全局
                        # 统计。独立 try 且先于 session 日志，避免日志抛错把整笔记账一起跳过。
                        if _budget_scope is not None:
                            try:
                                from gsuid_core.ai_core.budget import budget_manager

                                await budget_manager.record_usage_scope(
                                    _budget_scope[0],
                                    _budget_scope[1],
                                    _budget_scope[2],
                                    self.session_id,
                                    input_tokens,
                                    output_tokens,
                                    cache_read_tokens,
                                    cache_write_tokens,
                                )
                            except Exception as _be:
                                logger.warning(i18n_t("log.agent.budget_fail", _be=_be))
                        try:
                            self._session_logger.log_token_usage(
                                input_tokens,
                                output_tokens,
                                _model_name,
                                cache_read_tokens,
                                cache_write_tokens,
                            )
                        except Exception as _le:
                            logger.debug(i18n_t("log.agent.write_token_usage_log", _le=_le))
                except AttributeError as e:
                    # result 没有 usage 属性（如 pydantic_graph End 节点返回的结果）
                    logger.info(i18n_t("log.agent.access_result_usage", e=e))
                    pass
                except TypeError as e:
                    # v1 旧写法 result.usage() 在 v2 抛 'RunUsage' is not callable
                    logger.info(i18n_t("log.agent.result_usage_call_style", e=e))
                    pass
                except Exception as e:
                    logger.warning(i18n_t("log.agent.record_statistics", e=e))

                # 当 return_model 指定时，直接返回 Pydantic 模型实例
                if output_type is not None:
                    self._session_logger.log_run_end()
                    self._session_logger.log_result(result.output, _tool_call_list)
                    return result.output

                # 始终返回字符串类型
                result_msg = str(result.output).strip()
                # 工具调用列表只进调试日志，不追加到用户可见消息
                if _tool_call_list:
                    logger.debug(i18n_t("log.agent.current_tool_call_event", p0=", ".join(_tool_call_list)))

                self._session_logger.log_run_end()
                self._session_logger.log_result(result_msg, _tool_call_list)

                # 假完成结算（结构判据收口）。
                async def _resend_fab_blocked() -> None:
                    for _bt in _fab_blocked:
                        if _bt in self._run_sent_texts:
                            continue
                        try:
                            if bot is None:
                                logger.warning(i18n_t("log.agent.fakedone_bot_object_unavailable"))
                                continue
                            await send_chat_result(bot, _bt, ev=ev)
                            self._run_sent_texts.add(_bt)
                        except Exception as _se:
                            logger.debug(i18n_t("log.agent.fakedone_se", _se=_se))

                if _fab_blocked and _tool_call_list and bot and return_mode in ["always", "by_bot"]:
                    logger.info(i18n_t("log.agent.fakedone_claim"))
                    await _resend_fab_blocked()
                elif (
                    result_msg
                    and not _tool_call_list
                    and tool_names
                    and not fake_done_retry
                    # 结构证据：预检暂扣 or 文本宣称完成；不靠 intent 标签（误标会误伤闲聊）
                    and (_fab_blocked or _claims_fake_done(result_msg))
                ):
                    logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                    try:
                        corrected = await self._execute_run_once(
                            user_message=_FAKE_DONE_NUDGE,
                            bot=bot,
                            ev=ev,
                            tools=tools,
                            return_mode=return_mode,
                            intent=intent,
                            has_active_task=has_active_task,
                            suppress_intermediate_text=suppress_intermediate_text,
                            fake_done_retry=True,
                        )
                    except Exception as _fe:
                        # 纠正 pass 是增强路径，失败不影响原结果返回；暂扣文本补发防"整轮沉默"
                        logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_fe))
                        corrected = None
                        if _fab_blocked and bot and return_mode in ["always", "by_bot"]:
                            await _resend_fab_blocked()
                    if isinstance(corrected, str) and corrected.strip():
                        # 纠正成功：从持久历史剥掉 nudge user turn 与暂扣未发的编造声明 （用户从没见过它们，
                        _fabricated = {t.strip() for t in _fab_blocked}
                        if _claims_fake_done(result_msg):
                            _fabricated.add(result_msg.strip())
                        result_msg = corrected.strip()
                        self._scrub_fake_done_history(_fabricated)

                # 结构假完成：被呼叫/省略续聊 + 池非空 + 零调用 + 非沉默 + 非极短寒暄（无用户话题词）
                elif (
                    result_msg
                    and not _tool_call_list
                    and tool_names
                    and not fake_done_retry
                    and self.create_by in _INTERACTIVE_CREATE_BY
                    and ev is not None
                    and (
                        bool(getattr(ev, "is_tome", False))
                        or bool(_tg is not None and (_tg.call_to_self or _tg.soft_continue or _tg.ellipsis_followup))
                    )
                    and result_msg.strip() not in SILENCE_MARKERS
                    and len(result_msg.strip()) > 12
                ):
                    logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                    try:
                        corrected = await self._execute_run_once(
                            user_message=_STRUCTURAL_ZERO_TOOL_NUDGE,
                            bot=bot,
                            ev=ev,
                            tools=tools,
                            return_mode=return_mode,
                            intent=intent,
                            has_active_task=has_active_task,
                            suppress_intermediate_text=suppress_intermediate_text,
                            fake_done_retry=True,
                        )
                    except Exception as _fe:
                        logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_fe))
                        corrected = None
                    if isinstance(corrected, str) and corrected.strip():
                        _prior = result_msg.strip()
                        result_msg = corrected.strip()
                        if _prior:
                            self._scrub_fake_done_history({_prior})

                # 结构数据已返回却未出图：仅当池内有渲染工具且台词已偏长（疑似念表）
                elif (
                    _saw_structured_return
                    and _tool_call_list
                    and not (_RENDER_TOOL_NAMES & set(_tool_call_list))
                    and bool(_RENDER_TOOL_NAMES & set(tool_names))
                    and result_msg
                    and len(result_msg.strip()) > 80
                    and not fake_done_retry
                    and self.create_by in _INTERACTIVE_CREATE_BY
                ):
                    logger.warning(i18n_t("log.agent.render_data_nudge_once"))
                    try:
                        _rc = await self._execute_run_once(
                            user_message=_RENDER_DATA_NUDGE,
                            bot=bot,
                            ev=ev,
                            tools=tools,
                            return_mode=return_mode,
                            intent=intent,
                            has_active_task=has_active_task,
                            suppress_intermediate_text=suppress_intermediate_text,
                            fake_done_retry=True,
                        )
                    except Exception as _re:
                        logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_re))
                        _rc = None
                    if isinstance(_rc, str) and _rc.strip():
                        result_msg = _rc.strip()

                # <report> 制品正文换占位符（§1 漂移固化）。
                _compact_report_blocks_in_history(_new_msgs, sent_texts=self._run_sent_texts)

                if return_mode in ["by_bot"] and bot and ev:
                    return ""
                # 出戏兜底（§D.4）：run() 的返回值供**无 bot 发送通道**的消费方使用
                # （chat_with_history API、任何直接读取 output 的调用方）——send 路径的出戏
                if result_msg and output_firewall.is_enabled():
                    result_msg, _ooc_scrubbed = output_firewall.scrub_or_fallback(
                        result_msg, user_text=ev.raw_text if ev is not None and ev.raw_text else ""
                    )
                    if _ooc_scrubbed:
                        logger.warning(i18n_t("log.agent.firewall_run_return_value_hit"))
                return result_msg

            # result 为空时的默认返回值（常量：handle_ai 好感度门等消费端按它识别准失败轮）
            return NO_RESULT_TEXT

        except UsageLimitExceeded:
            # 达到限制后的处理逻辑
            logger.warning(i18n_t("log.agent.pydanticai_reached_maximum_thinking", p0=limits.request_limit))
            statistics_manager.record_error(error_type="usage_limit")
            self._session_logger.log_error("usage_limit", f"达到最高思考轮数限制 {limits.request_limit}")

            # 子代理（return 模式，如 Kanban 能力代理 / plugin_developer_agent）： **绝不**直接对用户的 bot 说话
            # 也**绝不**把超轮数的中间产物强制总结后回灌
            if return_mode == "return":
                return (
                    "⚠️ 已达最大思考轮数，未能在限定步数内完成本任务。"
                    "中间产物（如已写入的文件 / artifact）已留在工作区，未回传以避免刷屏。"
                )

            # 安抚用户
            if bot:
                await bot.send(await bot.t("log.ai_agent.chain_too_long_summary"))

            # ✨ 【关键点2】发起"强制总结"请求
            try:
                user_question = last_user_question or "用户之前提出的问题"

                # 从历史中提取已获取的事实和模型推理片段
                run_context = _extract_run_context(self.history)

                if run_context:
                    final_message = (
                        f"【用户的问题】\n{user_question}\n\n"
                        f"【已获取的信息和推理过程】\n{run_context}\n\n"
                        "请根据以上已知信息，根据人设风格直接回答用户的问题。"
                        "禁止调用任何工具，只输出自然语言文本。"
                    )
                else:
                    final_message = (
                        f"【用户的问题】\n{user_question}\n\n"
                        "请直接回答这个问题（根据你的已有知识和角色性格），不要调用任何工具。"
                    )

                # 创建无工具精简 Agent（tools=[] = 内部无 schema，从根源消除工具调用）
                _fallback_agent = Agent(
                    model=self.model,
                    system_prompt=self.system_prompt or "你是一个智能助手。",
                    model_settings={"max_tokens": self.max_tokens},
                    tools=[],
                    toolsets=[],
                    retries=0,
                    output_type=str,
                )

                # message_history 为空：所有上下文已聚焦到 final_message 中
                fallback_result = await _fallback_agent.run(
                    final_message,
                    message_history=[],
                    usage_limits=UsageLimits(request_limit=1),
                )

                # 强制总结同样是一次真实 LLM 往返，把它的最终产出记进当前 session
                # logger（与本 run 同一文件）——否则"超轮数兜底"答复在日志里不可见。
                fallback_text = str(fallback_result.output)
                self._session_logger.log_text_output(fallback_text)
                self._session_logger.log_result(fallback_text, _tool_call_list)

                if bot:
                    await send_chat_result(bot, fallback_result.output, ev=ev)
                return ""

            except Exception as e:
                logger.error(i18n_t("log.agent.pydanticai_forced_summary", e=e))
                self._session_logger.log_error("fallback_failed", str(e))
                fallback_error = (
                    "⚠️ 问题较复杂，现有信息不足以给出准确答案。可以尝试提高思维链长度，或换个方式描述问题。"
                )
                if bot:
                    await bot.send(fallback_error)
                    return ""
                return fallback_error

        # 瞬时故障（超时/网络/5xx/529 等）一律不在此捕获，向上抛给 _execute_run
        # 统一重试；download image 自愈与错误文案/统计也收敛到 _execute_run。
        finally:
            # 还原预算 scope contextvar，避免本次绑定泄漏到上层调用栈。
            if _budget_scope_token is not None:
                _current_budget_scope.reset(_budget_scope_token)
            # 同理还原墙钟时钟：嵌套 run 结束后父 run 必须拿回自己的累加器。
            wall_clock.uninstall_clock(_wall_clock_token)
            # 清理本轮的单轮节流计数（scheduler.py add_once_task 等共享）， 防止内存中 key 无限累积。
            try:
                from gsuid_core.ai_core.buildin_tools.scheduler import (
                    clear_turn_throttle,
                )
                from gsuid_core.ai_core.buildin_tools.message_sender import (
                    clear_turn_send_throttle,
                )

                sess = ev.session_id if ev is not None else None
                if sess:
                    clear_turn_throttle(str(sess), turn_id)
                    clear_turn_send_throttle(str(sess), turn_id)
            except Exception as _e:
                logger.debug(i18n_t("log.agent.clear_counter", _e=_e))

    @overload
    async def run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
        output_type: None = None,
        enqueue_ts: Optional[float] = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[Any] = None,
        cheap_gate: Optional[Any] = None,
    ) -> str: ...

    @overload
    async def run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
        output_type: type[_T] = ...,
        enqueue_ts: Optional[float] = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[Any] = None,
        cheap_gate: Optional[Any] = None,
    ) -> _T: ...

    async def run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        return_mode: Literal["always", "return", "by_bot"] = "by_bot",
        output_type: Optional[type] = None,
        enqueue_ts: Optional[float] = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[Any] = None,
        cheap_gate: Optional[Any] = None,
    ) -> Union[str, Any]:
        """
        运行 Agent 并返回结果

        此方法使用锁机制确保同一时间只有一个请求在执行，
        其他请求会挂起等待，执行时自动继承历史记录

        Args:
            output_type: 当指定为某个 Pydantic 模型类时，利用 pydantic_ai 的
                output_type 特性，要求模型必须返回符合该模型结构的 JSON。
                此时返回值为该 Pydantic 模型实例而非字符串。
            enqueue_ts: 本次请求入队时间戳（O-A）。交互式主对话在 _run_lock 上排队过久
                （> STALE_CHAT_REQUEST_TTL）则视为"过期请求"丢弃，避免对早已结束的话题
                突兀回复。仅对 create_by=="Chat" 生效。
            has_active_task: 是否存在需即时介入的 Kanban 任务，透传给状态驱动工具池（L2），
                决定是否把"长期任务编排 + 产物"能力族补进工具列表。
            intent: 本轮意图标签（闲聊/工具/问答）。仅影响「连续无工具强制提醒」豁免与
                输出风格；**不**再作为向量预装/状态驱动工具池的硬门（分类器会误判闲聊）。
            budget_gate: 本次 run 是否为预算入口。True（巡检 / proactive / 定时等自主调用）
                时超额直接早退、绝不花费 Token；交互被动路径已在 handle_ai 提前闸门，按默认
                False 只记账不二次拦截。无论是否拦截，可归属 scope 的 Token 都会记账。
            suppress_intermediate_text: True 时，本轮中**只要出现过 ToolCallPart**，其前后伴随的
                文本片段都不会发送给用户，仅保留没有任何工具调用的最终文本回复。
                用于画布 Agent 等多工具编排场景，避免中间步骤的碎碎念刷屏。
            turn_graph: 入口构建的 TurnGraph（可选）；缺省时在装配层现场构建。
            cheap_gate: CheapGate 成本档（可选）；驱动 light 零工具 / 群聊瘦保底。

        Returns:
            Agent 执行结果。默认返回 str，当 output_type 指定时返回对应模型实例
        """
        # A: 同 Session 新消息入队时，打断正在跑的 generation（合并语义交给后到者：
        # HistoryManager 已含 A/B 用户句，后到 run 装配完整上下文）。
        if self.create_by in _INTERACTIVE_CREATE_BY and self._run_lock.locked():
            self._cancel_generation.set()
            logger.info(i18n_t("log.agent.supersede_cancel_current"))

        async with self._run_lock:
            logger.info(i18n_t("log.agent.acquired_lock"))
            # 本 generation 独立 cancel 事件；上轮 set 过的不得污染本轮
            self._cancel_generation = asyncio.Event()
            # O-A 群聊队头阻塞防护：拿到锁时若已排队过久（话题大概率翻篇），丢弃过期回复。
            if (
                enqueue_ts is not None
                and self.create_by == "Chat"
                and (time.time() - enqueue_ts) > STALE_CHAT_REQUEST_TTL
            ):
                waited = time.time() - enqueue_ts
                logger.info(i18n_t("log.agent.queue_wait_waited_exceeded", waited=waited))
                return "" if output_type is None else None
            # 模型热切换：网页控制台切换高/低级任务模型后，存活会话在此即时热替换到新模型， 无需 coreclear 重置会话。
            await self.refresh_model_if_changed()

            async def _do_run():
                return await self._execute_run(
                    user_message=user_message,
                    bot=bot,
                    ev=ev,
                    rag_context=rag_context,
                    tools=tools,
                    return_mode=return_mode,
                    output_type=output_type,
                    intent=intent,
                    has_active_task=has_active_task,
                    budget_gate=budget_gate,
                    suppress_intermediate_text=suppress_intermediate_text,
                    turn_graph=turn_graph,
                    cheap_gate=cheap_gate,
                )

            # 显式绑定固定模型的会话（model_config_name 为 None）不参与 provider 路由
            if self.model_config_name is None:
                result = await _do_run()
                logger.info(i18n_t("log.agent.lock_ok"))
                return result

            # provider 路由：主配置并发满/冷却时切到备用(2nd)配置；请求命中
            # provider 级故障（限流/连接）时给该配置冷却期并换路重试一次。
            _primary_cfg = get_config_name_for_task(self.task_level)
            _secondary_cfg = get_2nd_config_name_for_task(self.task_level)
            logger.debug(
                i18n_t(
                    "log.agent.provider_routing_task_level",
                    task_level=self.task_level,
                    primary=_primary_cfg,
                    secondary=_secondary_cfg or "(未配置)",
                )
            )
            for _attempt in range(2):
                async with provider_router.slot(self.task_level) as routed_name:
                    logger.debug(
                        i18n_t(
                            "log.agent.attempt_routed_config_name",
                            attempt=_attempt + 1,
                            routed_name=routed_name,
                        )
                    )
                    temp_model = None
                    orig_model = self.model
                    if routed_name and routed_name != self.model_config_name:
                        try:
                            temp_model = get_model_by_full_name(routed_name)
                            self.model = temp_model
                        except Exception as e:
                            logger.warning(
                                i18n_t(
                                    "log.agent.backup_config_routed_name",
                                    routed_name=routed_name,
                                    e=e,
                                )
                            )
                            routed_name = self.model_config_name
                    try:
                        result = await _do_run()
                        # 内层重试耗尽后返回错误字符串（非异常）：若为 provider 级故障
                        # （限流/连接/5xx），标记冷却并换路重试，而非直接返回错误给用户。
                        _is_error_str = isinstance(result, str) and result.startswith(ERROR_RESULT_PREFIX)
                        _is_provider_failure = _is_error_str and looks_like_provider_failure(result)
                        logger.debug(
                            i18n_t(
                                "log.agent.run_str_fail_provider",
                                is_str=isinstance(result, str),
                                is_error=_is_error_str,
                                is_failure=_is_provider_failure,
                                attempt=_attempt,
                            )
                        )
                        if _attempt == 0 and _is_provider_failure:
                            provider_router.mark_failure(routed_name or self.model_config_name)
                            logger.warning(
                                i18n_t(
                                    "log.agent.provider_level_inner_retries",
                                    r=result[:200],
                                )
                            )
                            continue
                        provider_router.mark_success(routed_name or self.model_config_name)
                        logger.info(i18n_t("log.agent.lock_ok"))
                        return result
                    except Exception as e:
                        if _attempt == 0 and looks_like_provider_failure(str(e)):
                            provider_router.mark_failure(routed_name or self.model_config_name)
                            logger.warning(i18n_t("log.agent.provider_level_switching_route", e=e))
                            continue
                        raise
                    finally:
                        if temp_model is not None:
                            # 备用模型不关底层 client（共享缓存客户端，close 会拖垮全进程会话）
                            self.model = orig_model


# 工厂函数
def create_agent(
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
    max_iterations: Optional[int] = None,
    persona_name: Optional[str] = None,
    create_by: str = "LLM",
    max_history: Optional[int] = None,
    task_level: Literal["high", "low"] = "high",
    session_id: Optional[str] = None,
    is_subagent: bool = False,
    dynamic_tools: Optional[bool] = None,
    scope_key: Optional[str] = None,
    wall_clock_budget: Optional[float] = None,
    on_trace: Optional[Callable[[str, str], None]] = None,
) -> GsCoreAIAgent:
    """
    创建 PydanticAI Agent 实例

    Args:
        model_name: 模型名称
        system_prompt: 系统提示词
        max_tokens: 最大输出 token 数，None 时使用全局配置默认值
        max_iterations: 最大迭代次数限制，None 时使用配置默认值
        persona_name: Persona 名称（用于热重载检测）
        task_level: 任务级别，"high"表示高级任务，"low"表示低级任务
        session_id: 会话 ID，用于关联 session 日志
        is_subagent: 是否为 SubAgent，为 True 时日志存放于独立子目录
        dynamic_tools: dynamic 能力族开关；None 沿用旧门（agentic 且未传 tools 才装配）
        scope_key: 记忆 scope（group:xxx / user_global:xxx 等）。仅在未显式给 session_id 的
            后台调用时生效——把"针对哪个群/用户"编进自动派生的 auto_ session_id，供 webconsole 展示指向
        wall_clock_budget: C-4 墙钟软预算(秒)覆写。None=沿用全局 scaffold_wall_clock_budget(默认 45s，
            按聊天回复标定)；<=0=关闭软预算。长流程编排入口（一轮几十次工具调用、还要等人确认）
            必须显式放宽，否则会在半途被"停止新工具轮"提示逼停
        on_trace: 轨迹观察者 `on_trace(kind, text)`，kind ∈ {"thinking","tool"}（tool 的 text 为
            `"<工具名>|<参数JSON>"`）。宿主用它把模型推理与工具调用实时呈现给用户
            （如画布前端的「思考过程」折叠块）。旁路钩子，异常会被吞掉，不影响 run

    Returns:
        PydanticAIAgent 实例

    Example:
        agent = create_agent(
            system_prompt='你是一个智能助手。',
        )
    """
    return GsCoreAIAgent(
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        max_iterations=max_iterations,
        persona_name=persona_name,
        create_by=create_by,
        max_history=max_history,
        task_level=task_level,
        session_id=session_id,
        is_subagent=is_subagent,
        dynamic_tools=dynamic_tools,
        scope_key=scope_key,
        wall_clock_budget=wall_clock_budget,
        on_trace=on_trace,
    )


async def build_new_persona(query: str) -> str:
    """
    构建新的角色提示词

    使用角色构建模板和用户查询，生成新的角色提示词。

    Args:
        query: 用户查询，描述新角色的特征和能力

    Returns:
        新角色的提示词字符串
    """
    # 不再传固定的 "build_persona" session_id：让 __init__ 自动派生 auto_BuildPersona_* 的一次性 subagent
    # 日志（落 subagents/ 子目录
    agent = create_agent(
        system_prompt=CHARACTER_BUILDING_TEMPLATE,
        create_by="BuildPersona",
        task_level="high",
    )
    response = await agent.run(query)
    return response.strip()
