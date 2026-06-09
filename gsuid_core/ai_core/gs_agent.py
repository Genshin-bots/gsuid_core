"""
PydanticAI Agent 核心模块
基于 pydantic_ai 实现的轻量级 Agent
"""

import time
import uuid
import asyncio
from typing import Any, Set, List, Union, Literal, TypeVar, Optional, Sequence, overload

import httpx
from pydantic_ai import Agent
from pydantic_graph import End
from pydantic_ai.agent import CallToolsNode, ModelRequestNode
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    UserContent,
    ModelMessage,
    ModelRequest,
    ThinkingPart,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
    RetryPromptPart,
    ModelResponsePart,
)
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import SILENCE_MARKERS, send_chat_result, materialize_image_url
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.skills import skills_toolset
from gsuid_core.ai_core.register import find_tool_base, get_tools_by_capability_domain
from gsuid_core.ai_core.rag.tools import (
    NON_SEARCHABLE_TOOL_CATEGORIES,
    ToolList,
    search_tools,
    get_main_agent_tools,
    get_scope_context_tags,
    expand_tools_to_families,
    get_tools_by_context_tags,
)
from gsuid_core.ai_core.configs.models import get_model_for_task
from gsuid_core.ai_core.session_logger import AISessionLogger, ProactiveSource
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.persona.prompts import INNER_OS_MARKER, CHARACTER_BUILDING_TEMPLATE
from gsuid_core.ai_core.configs.ai_config import ai_config

_T = TypeVar("_T")

# L3 会话驻留：一个能力族被使用后，继续在随后多少轮里保持常驻（兜底紧邻的追问）。
_STICKY_FAMILY_TURNS = 3
# L5 上下文增强检索：把最近多少轮用户原话拼进工具向量检索 query（含本轮）。
_RECENT_TEXT_WINDOW = 3

# 框架默认的工具前摇台词（仅针对耗时较长、用户需要被告知"正在做事"的工具）。
# 这是框架级默认值，必须保持「人格中性」——不带任何特定 Persona 的口吻或语气，
# 任何 Persona 都应能直接套用而不出戏。带角色个性的台词应由各 Persona 在
# config.json 的 "pre_tool_expressions" 字段中覆盖（值为空字符串表示该工具
# 无需前摇）。早柚等具体人格的专属台词请写在其 Persona 配置内，切勿写在此处。
_FRAMEWORK_PRE_TOOL_EXPRESSIONS: dict[str, str] = {
    "web_search_tool": "稍等，我查一下相关信息…",
    "search_knowledge": "让我先查一下资料…",
    "web_fetch_tool": "我打开这个链接看看…",
    "create_subagent": "这个任务我来安排处理…",
    "render_html_to_image": "稍等，正在生成图片…",
    "render_markdown_to_image": "稍等，正在生成图片…",
    "generate_image": "稍等，正在生成图片，可能需要一点时间…",
    "generate_video": "稍等，正在生成视频，这个会比较久，请耐心等待…",
    "edit_image": "稍等，正在处理图片…",
    "generate_music": "稍等，正在生成音乐…",
}

# 每次运行最多发送的前摇数量，避免刷屏
_MAX_PRE_TOOL_EXPRESSIONS_PER_RUN = 2


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
    from gsuid_core.ai_core.capability_agents.registry import list_profiles

    registered = get_registered_tools()
    hidden_names: set[str] = set()
    for cat in NON_SEARCHABLE_TOOL_CATEGORIES:
        if cat in registered:
            hidden_names.update(registered[cat].keys())
    if not hidden_names:
        return ""

    for profile in list_profiles():
        matched = profile.profile_id.lower() in h or any(kw.lower() in h for kw in profile.match_keywords)
        if matched and any(tn in hidden_names for tn in profile.tool_names):
            return profile.profile_id
    return ""


# Persona 前摇配置缓存 {persona_name: dict}
_persona_pre_tool_cache: dict[str, dict] = {}


def invalidate_persona_pre_tool_cache(persona_name: Optional[str] = None) -> None:
    """清理 persona 前摇台词缓存：``persona_name`` 为 None 清全部，否则只清一项。

    A-6 修复：``_persona_pre_tool_cache`` 是模块级缓存、首次读取后不再回盘。persona
    热重载（``ai_router`` 检测到目录 mtime 变化重建 session）时若不清缓存，改了
    ``config.json`` 的 ``pre_tool_expressions`` 必须重启进程才生效。供 ``ai_router``
    在热重载分支调用，与 ``invalidate_voice_anchor_cache`` 配套。
    """
    if persona_name is None:
        _persona_pre_tool_cache.clear()
        return
    _persona_pre_tool_cache.pop(persona_name, None)


def _get_pre_tool_expression(persona_name: Optional[str], tool_name: str) -> Optional[str]:
    """获取某工具的前摇台词。

    优先使用 Persona config.json 中的 "pre_tool_expressions" 配置，
    否则回退到框架默认台词。返回 None 表示该工具不需要前摇。
    """
    persona_table: dict = {}
    if persona_name:
        if persona_name not in _persona_pre_tool_cache:
            table: dict = {}
            try:
                import json

                from gsuid_core.ai_core.resource import PERSONA_PATH

                config_path = PERSONA_PATH / persona_name / "config.json"
                if config_path.exists():
                    with open(config_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    if isinstance(cfg, dict) and "pre_tool_expressions" in cfg:
                        raw = cfg["pre_tool_expressions"]
                        if isinstance(raw, dict):
                            table = raw
            except Exception:
                table = {}
            _persona_pre_tool_cache[persona_name] = table
        persona_table = _persona_pre_tool_cache[persona_name]

    import random

    for source in (persona_table, _FRAMEWORK_PRE_TOOL_EXPRESSIONS):
        if tool_name in source:
            value = source[tool_name]
            if isinstance(value, list):
                value = random.choice(value) if value else ""
            value = str(value).strip()
            return value or None
    return None


def _extract_run_context(history: List[ModelMessage], max_fact_len: int = 2000) -> str:
    """从历史消息中提取"已知事实"和"模型推理片段"，按轮次组织。

    相比只提取 ToolReturnPart，还保留 TextPart（LLM 中间推理），
    因为这些推理有时本身就是有价值的结论。
    """
    sections: list[str] = []
    round_num = 0

    for msg in history:
        if isinstance(msg, ModelResponse):
            round_num += 1
            texts: list[str] = []
            calls: list[str] = []
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content.strip():
                    t = part.content.strip()
                    if len(t) > 500:
                        t = t[:500] + "...[截断]"
                    texts.append(t)
                elif isinstance(part, ToolCallPart):
                    calls.append(part.tool_name)

            if texts or calls:
                header = f"【第{round_num}轮】"
                if calls:
                    header += f" 调用工具: {', '.join(calls)}"
                if texts:
                    header += "\n" + "\n".join(texts)
                sections.append(header)

        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    content = str(part.content).strip()
                    if len(content) > max_fact_len:
                        content = content[:max_fact_len] + f"\n...[截断, 共{len(content)}字符]"
                    sections.append(f"  → [{part.tool_name}] 返回: {content}")

    return "\n".join(sections) if sections else ""


def _truncate_message_for_log(msg: Any, max_base64_len: int = 100) -> Any:
    """
    截断消息中的长 base64 数据，用于日志输出。

    Args:
        msg: 消息内容，可能是 str、ImageUrl 或其列表
        max_base64_len: base64 数据最大显示长度

    Returns:
        截断后的消息副本
    """
    from pydantic_ai.messages import ImageUrl

    if isinstance(msg, str):
        # 检查是否是 base64 DataURI
        if ";base64," in msg and len(msg) > max_base64_len:
            return f"{msg[:max_base64_len]}...[base64截断, 总长={len(msg)}]"
        return msg
    elif isinstance(msg, ImageUrl):
        url = msg.url
        if ";base64," in url and len(url) > max_base64_len:
            return ImageUrl(url=f"{url[:max_base64_len]}...[base64截断, 总长={len(url)}]")
        return msg
    elif isinstance(msg, list):
        return [_truncate_message_for_log(item, max_base64_len) for item in msg]
    return msg


def _truncate_history_with_tool_safety(
    history: List[ModelMessage],
    max_history: int,
) -> List[ModelMessage]:
    """
    安全截断 history，确保保留的消息中 ToolCallPart 和 ToolReturnPart 完全配对。

    问题：如果简单地从末尾截断 history，可能导致 ToolReturnPart 被保留
    但其对应的 ToolCallPart 被丢弃（在被截断的前半部分），从而在下一轮请求时出现
    "tool result's tool id not found" 错误。

    解决策略：
    1. 先做一次试探性截断：保留最后 max_history 条消息
    2. 扫描截断结果，收集所有保留的 ToolReturnPart 的 tool_call_id
    3. 扫描截断结果，收集所有保留的 ToolCallPart 的 tool_call_id
    4. 如果有 return 找不到对应的 call，说明截断点切到了 tool call/return 对的中间
    5. 向前移动截断点，直到所有保留的 return 都有对应的 call

    Args:
        history: 原始消息历史
        max_history: 最大保留消息数

    Returns:
        截断后的安全消息历史
    """
    if len(history) <= max_history:
        return history

    # 从 max_history 开始，逐步扩大保留范围，直到 tool call/return 完全配对
    truncate_index = len(history) - max_history

    while truncate_index > 0:
        truncated = history[truncate_index:]

        # 收集截断结果中所有 ToolCallPart 的 tool_call_id
        retained_call_ids: Set[str] = set()
        # 收集截断结果中所有 ToolReturnPart 的 tool_call_id
        retained_return_ids: Set[str] = set()

        for msg in truncated:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        retained_call_ids.add(part.tool_call_id)
            elif isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        retained_return_ids.add(part.tool_call_id)
                    # RetryPromptPart 也是"工具结果型"消息：工具参数校验失败时
                    # 由 PydanticAI 生成，同样带 tool_call_id、必须有配对的
                    # ToolCallPart。tool_name 为 None 时是输出校验重试，不绑定
                    # 具体工具调用，不计入。
                    elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                        retained_return_ids.add(part.tool_call_id)

        # 找出截断结果中的孤立 return（有 return 但没有对应的 call）
        orphaned = retained_return_ids - retained_call_ids

        if not orphaned:
            # 所有保留的 return 都有对应的 call，截断安全
            logger.debug(
                f"🧠 [GsCoreAIAgent] 安全截断 history: {len(history)} -> {len(truncated)} (截断点: {truncate_index})"
            )
            return truncated

        # 有孤立 return，需要向前移动截断点
        # 找到所有孤立 return 所在的消息索引（相对于原始 history）
        min_orphaned_idx = len(history)  # 初始化为最大值
        for idx, msg in enumerate(history):
            if idx < truncate_index:
                continue  # 只看截断范围内的
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    tcid: Optional[str] = None
                    if isinstance(part, ToolReturnPart):
                        tcid = part.tool_call_id
                    elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                        tcid = part.tool_call_id
                    if tcid is not None and tcid in orphaned:
                        min_orphaned_idx = min(min_orphaned_idx, idx)

        # 向前移动截断点到孤立 return 之前，再留 2 条消息的缓冲
        new_truncate_index = max(0, min_orphaned_idx - 2)
        if new_truncate_index >= truncate_index:
            # 安全阀：如果无法继续前移，直接保留全部历史
            logger.warning(f"🧠 [GsCoreAIAgent] 无法安全截断 history，保留全部 {len(history)} 条")
            return history

        truncate_index = new_truncate_index

    # truncate_index == 0，保留全部历史
    logger.debug(f"🧠 [GsCoreAIAgent] 安全截断 history: {len(history)} -> {len(history)} (保留全部)")
    return history


def _drop_orphan_tool_results(history: List[ModelMessage]) -> List[ModelMessage]:
    """丢弃所有找不到配对 ToolCallPart 的孤儿工具结果消息。

    最终一致性兜底：即便 ``_truncate_history_with_tool_safety`` 逻辑正确，
    历史里仍可能因并发 / 异常中断残留坏配对（孤儿 ToolReturnPart 或带
    tool_name 的 RetryPromptPart）。本函数在 ``extract_history()`` 末尾被
    无条件调用，保证送进 API 的 message_history 永远自洽——一次坏截断不会
    让 session 永久不可用（"tool result's tool id not found" 400）。
    """
    call_ids: Set[str] = set()
    for msg in history:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    call_ids.add(part.tool_call_id)

    cleaned: List[ModelMessage] = []
    for msg in history:
        if isinstance(msg, ModelRequest):
            kept_parts = []
            for part in msg.parts:
                # 复用同一个 isinstance 守卫：进入分支时 part 类型已被 mypy/Pyright
                # 收窄为 ToolReturnPart / RetryPromptPart，两者都有 tool_call_id，
                # 不需要 getattr 兜底（LLM.md §1.4）。
                if isinstance(part, ToolReturnPart) and part.tool_call_id not in call_ids:
                    logger.warning(f"🧠 [GsCoreAIAgent] 丢弃孤儿 ToolReturnPart: tool_call_id={part.tool_call_id}")
                    continue
                if (
                    isinstance(part, RetryPromptPart)
                    and part.tool_name is not None
                    and part.tool_call_id not in call_ids
                ):
                    logger.warning(f"🧠 [GsCoreAIAgent] 丢弃孤儿 RetryPromptPart: tool_call_id={part.tool_call_id}")
                    continue
                kept_parts.append(part)
            if kept_parts:
                msg.parts = kept_parts
                cleaned.append(msg)
            # parts 全被丢弃的空 ModelRequest 整条剔除
        else:
            cleaned.append(msg)
    return cleaned


def _strip_remote_images_from_history(history: List[ModelMessage]) -> int:
    """把历史里残留的「远程图片 URL」剥离成文字占位，返回剥离处数量。

    推理端报「Failed to download image」基本都是早先入历史的远程图片 URL
    （如 QQ 带 rkey 的临时链接）已过期。不清掉的话，后续每一轮把它重发给
    推理端都会 500，整个会话被永久卡死。这里把过期的远程 ``ImageUrl`` 替换为
    文字占位，让下一轮自动恢复。base64 DataURI 不会过期，保留不动。
    """
    removed = 0
    for msg in history:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content
            if isinstance(content, str):
                continue
            new_content: List[UserContent] = []
            changed = False
            for item in content:
                if isinstance(item, ImageUrl) and item.url.startswith(("http://", "https://")):
                    new_content.append("[图片已过期，无法再显示]")
                    changed = True
                    removed += 1
                else:
                    new_content.append(item)
            if changed:
                part.content = new_content
    return removed


def _split_embedded_thinking(
    parts: Sequence[ModelResponsePart],
    thinking_tags: tuple[str, str],
) -> List[ModelResponsePart]:
    """把 TextPart 里以 thinking_tags 包裹的内嵌思考重新拆成独立的 ThinkingPart。

    流式请求下 pydantic_ai 只有在 ``<think>`` 作为独立 SSE chunk 到达时才会拆分思考
    标签（见 _parts_manager.handle_text_delta）；MiniMax 等兼容网关不保证这一点，
    导致 ``<think>...</think>`` 残留在 TextPart 里被原样发往 C 端，且思考内容拿不到
    意图-行为一致性检测。这里按完整文本重新拆分，对齐非流式路径
    （openai 模型的 split_content_into_text_and_thinking）的行为。非 TextPart 与不含
    起始标签的 TextPart 原样透传。
    """
    start_tag, end_tag = thinking_tags
    result: List[ModelResponsePart] = []
    for part in parts:
        if not isinstance(part, TextPart) or start_tag not in part.content:
            result.append(part)
            continue
        content = part.content
        start_index = content.find(start_tag)
        while start_index >= 0:
            before, content = content[:start_index], content[start_index + len(start_tag) :]
            if before:
                result.append(TextPart(content=before))
            end_index = content.find(end_tag)
            if end_index >= 0:
                think, content = content[:end_index], content[end_index + len(end_tag) :]
                result.append(ThinkingPart(content=think))
            else:
                # 缺少闭合标签：丢弃 <think> 起始标签，剩余内容按文本处理
                result.append(TextPart(content=content))
                content = ""
            start_index = content.find(start_tag)
        if content:
            result.append(TextPart(content=content))
    return result


# 单轮意图-行为不一致检测关键词：thinking 里点名了某工具 / 任务编排意图
# 却没真正调用——直接顶到阈值，下一轮立刻强制提醒。提到模块级避免每轮重建。
_INTENT_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "register_kanban_task",
    "evaluate_agent_mesh_capability",
    "create_subagent",
    "复合多代理任务",
    "任务树",
    "创建任务树",
    "托管",
    "委派",
    # 「枚举时间点」思维信号——主人格想用 add_once_task 逐个时间点注册时，
    # 本轮即便确实调用了 add_once_task，下一轮也强提醒走 register_kanban_task
    # 的 recurring_trigger 路径。
    "逐个时间点",
    "逐一设置",
    "每个时间点单独",
    "为每个时间点",
    "5个时间点",
    "10个时间点",
    "cron 的话需要写多个",
    "需要写多个触发器",
)

# O-A 群聊队头阻塞防护：交互式回复在 _run_lock 上排队超过此秒数（话题大概率已翻篇）
# 则丢弃本次回复，避免对早已结束的话题"过期答复"。仅作用于 create_by=="Chat" 的主对话。
STALE_CHAT_REQUEST_TTL = 8.0


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
        openai_chat_model: Optional[OpenAIChatModel] = None,
        system_prompt: Optional[str] = None,
        max_tokens: int = 30000,
        max_iterations: Optional[int] = None,
        persona_name: Optional[str] = None,
        max_history: int = 20,
        create_by: str = "LLM",
        task_level: Literal["high", "low"] = "high",
        session_id: Optional[str] = None,
        is_subagent: bool = False,
    ):
        self.history: List[ModelMessage] = []
        self.max_history = max_history
        self.system_prompt = system_prompt
        self.persona_name = persona_name  # 用于热重载检查
        # 用于串行执行 run 方法的锁
        self._run_lock = asyncio.Lock()
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations  # 自定义迭代次数限制，None时使用配置默认值
        self.task_level: Literal["high", "low"] = task_level  # 任务级别，用于选择对应的模型配置

        self.create_by = create_by
        # 未显式给 session_id 的来源（能力评估 / meme 打标 / 记忆摄入·检索等后台 LLM
        # 调用）自动派生一个一次性 subagent id——这样"所有调用来源都写 session log"
        # 在结构上得到保证，无法被某个来源遗漏。详见 docs/AI_SESSION_LOGGING.md。
        if session_id is None:
            session_id = f"auto_{create_by}_{uuid.uuid4().hex[:8]}"
            is_subagent = True
        self.session_id: str = session_id
        self.is_subagent: bool = is_subagent

        # 连续无工具调用计数：连续多轮只输出文本、不调用任何工具时，
        # 下一轮注入强制提醒，防止 Agent 以角色无知为由持续推脱
        self._consecutive_no_tool_rounds: int = 0

        # L3 会话驻留：最近使用过的能力族 → 剩余可常驻轮数（每轮递减）。
        # 兜底"刚用过某能力、紧接着的追问语义却召不回该工具"的场景。
        self._recent_tool_families: dict[str, int] = {}
        # L5 上下文增强检索：最近几轮用户原话，拼进工具向量检索 query，
        # 让"改成后天吧"这类无独立语义的追问也能借上文召回到正确工具族。
        self._recent_user_texts: List[str] = []

        self.model = openai_chat_model
        if self.model is None:
            self.model = get_model_for_task(task_level)

        # 初始化会话日志记录器：所有 Agent 恒有 logger（session_id 已在上方自动派生
        # 兜底），因此 _session_logger 非 Optional，run() 中不再需要 None 守卫。
        # system_prompt 由 AISessionLogger 内部记一条 system_prompt entry，
        # 这里不再重复调用 log_system_prompt（避免与旧逻辑重复落两遍）。
        self._session_logger: AISessionLogger = AISessionLogger(
            session_id=session_id,
            system_prompt=system_prompt,
            persona_name=persona_name,
            create_by=create_by,
            is_subagent=is_subagent,
        )

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

        if len(self.history) > self.max_history:
            self.history = _truncate_history_with_tool_safety(
                self.history,
                self.max_history,
            )
        # 兜底：无论是否截断，都做一次孤儿工具结果清理，确保历史对 API 自洽
        self.history = _drop_orphan_tool_results(self.history)
        logger.debug(f"🧠 [GsCoreAIAgent] 历史记录已处理至 {len(self.history)} 条")

    async def _prepare_user_message(
        self,
        content_list: list[UserContent],
    ) -> Union[str, list[UserContent]]:
        """处理用户消息中的图片内容

        当 user_message 为 Sequence[UserContent] 时，检查其中是否包含 ImageUrl。
        如果包含，根据当前模型的 model_support 配置决定：
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

        # 分离文本和图片
        text_parts: list[str] = []
        image_urls: list[str] = []
        for item in content_list:
            if isinstance(item, ImageUrl):
                image_urls.append(item.url)
            elif isinstance(item, str):
                text_parts.append(item)

        if "image" in model_support:
            # 模型支持图片，保留原始内容；但远程图片 URL（如 QQ 带 rkey 的临时
            # 链接）会过期，一旦写进 message_history，之后每轮重发都会让推理端
            # 反复下载并 500「Failed to download image」、整个会话被永久卡死。
            # 故在「入历史前」就把远程 URL 物化为 base64 DataURI（永不过期）；
            # 已是 DataURI 的输入会被 materialize_image_url 原样跳过。
            result: list[UserContent] = []
            for item in content_list:
                if isinstance(item, str):
                    result.append(f"【用户发言】\n{item}")
                elif isinstance(item, ImageUrl):
                    # Fix-07 兜底：入历史前再次确认远程 URL 已物化为 base64；
                    # 若物化失败（仍为 http(s) URL），跳过该图片，避免把过期
                    # 链接写入 message_history 导致后续轮次 400/500。
                    url = await materialize_image_url(item.url)
                    if url.startswith(("http://", "https://")):
                        logger.warning(f"🖼️ [GsCoreAIAgent] 图片入历史前物化失败，跳过该图片: {item.url[:120]}")
                        continue
                    result.append(ImageUrl(url=url))
                else:
                    result.append(item)
            return result

        # 模型不支持图片，调用图片理解模块转述
        if image_urls:
            logger.info(f"🖼️ [ImageUnderstand] 当前模型不支持图片，开始图片理解转述，共 {len(image_urls)} 张图片")
            # 用户问题：用于把冗长的图片描述按需精简到与问题相关的部分
            user_question = "\n".join(text_parts).strip()
            descriptions: list[str] = []
            for idx, url in enumerate(image_urls):
                try:
                    description = await understand_image(image_url=url, parent_session_id=self.session_id)
                    description = await self._summarize_image_description(description, user_question)
                    descriptions.append(f"图片{idx + 1}: {description}")
                except Exception as e:
                    logger.error(f"🖼️ [ImageUnderstand] 图片 {idx + 1} 理解失败: {e}")
                    descriptions.append(f"图片{idx + 1}: [图片理解失败]")

            if descriptions:
                image_text = "--- 图片内容描述 ---\n" + "\n".join(descriptions)
                text_parts.append(image_text)

        combined = "\n".join(text_parts) if text_parts else ""
        return f"【用户发言】\n{combined}"

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
            # session，保证"任何 AI 调用都有日志"——不再裸用 pydantic_ai Agent()。
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
                logger.debug(f"🖼️ [ImageUnderstand] 图片描述二次摘要: {len(description)} -> {len(summary)} 字符")
                return summary
        except Exception as e:
            logger.debug(f"🖼️ [ImageUnderstand] 图片描述二次摘要失败，使用原始描述: {e}")
        return description

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
    ) -> Union[str, Any]:
        """
        实际执行 Agent 运行的内部方法

        Args:
            output_type: 当指定为某个 Pydantic 模型类时，利用 pydantic_ai 的
                output_type 特性，要求模型必须返回符合该模型结构的 JSON。
                此时返回值为该 Pydantic 模型实例而非字符串。
        """
        from gsuid_core.ai_core.statistics import statistics_manager

        _tool_call_list: list[str] = []  # 用于记录本次运行中被调用的工具列表，供后续统计使用
        _pre_tool_sent: int = 0  # 本次运行已发送的前摇数量
        _thinking_segments: list[str] = []  # 累积本轮模型 thinking 文本，供意图-行为一致性检测

        # 使用自定义迭代次数限制（如果有），否则使用配置默认值
        if self.max_iterations is not None:
            limits = UsageLimits(request_limit=self.max_iterations)
        else:
            multi_agent_lenth: int = ai_config.get_config("multi_agent_lenth").data
            limits = UsageLimits(request_limit=multi_agent_lenth)

        # 记录开始时间用于延迟统计
        start_time = time.time()

        logger.info("🧠 [GsCoreAIAgent] ====== Agent 运行开始 ======")
        # turn_id：本轮 run 的唯一标识，写入 ToolContext.extra 供子工具读取（如
        # scheduler.py 的 add_once_task 单轮节流计数）。回合结束 finally 清理。
        # parent_session_id：透传给工具，让 send_message_by_ai 等"工具内主动发"
        # 路径能找到调用自己的主 session，把发出去的话同步进 pydantic_ai 历史 +
        # session_logger（见 §8.1）。
        turn_id = uuid.uuid4().hex
        context = ToolContext(
            bot=bot,
            ev=ev,
            extra={"turn_id": turn_id},
            parent_session_id=self.session_id,
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
            final_user_message = f"【用户发言】\n{user_message}"

        if rag_context:
            if isinstance(final_user_message, str):
                final_user_message = f"{final_user_message}\n\n{rag_context}"
            elif isinstance(final_user_message, list):
                final_user_message = list(final_user_message)
                final_user_message.append(f"\n\n{rag_context}")
            logger.info("🧠[GsCoreAIAgent] 已添加 RAG 上下文")

        # DS 专属角色扮演模式（inner_os）：仅在 Chat 模式首轮 user_message 末尾追加
        if (
            self.create_by == "Chat"
            and not self.history
            and ai_config.get_config("enable_deepseek_rp").data
            and isinstance(final_user_message, str)
        ):
            final_user_message = f"{final_user_message}{INNER_OS_MARKER}"
            logger.info("🧠[GsCoreAIAgent] 已注入 DS 角色扮演 Marker（首轮 Chat）")

        # 连续无工具调用检测：连续两轮以上只推脱不调工具时，注入强制提醒
        if self.create_by in ["Chat", "Agent"] and self._consecutive_no_tool_rounds >= 2:
            no_tool_reminder = (
                "\n\n【⚠️ 系统检测】你已连续多轮未调用任何工具，"
                "当前用户问题可能尚未得到有效回答。"
                "若你上一轮的思考里明确提到要调用某个工具（如 register_kanban_task、"
                "evaluate_agent_mesh_capability、create_subagent）却没有真正调用——"
                "口头答应 ≠ 执行，请本轮立即调用对应工具。否则请立即检查工具列表，"
                "选择最合适的工具调用，或明确说明为何确实无工具可用——禁止以角色"
                "不懂为由跳过工具。"
            )
            if isinstance(final_user_message, str):
                final_user_message += no_tool_reminder
            elif isinstance(final_user_message, list):
                final_user_message = list(final_user_message)
                final_user_message.append(no_tool_reminder)
            logger.debug("🧠 [GsCoreAIAgent] 已注入连续无工具调用强制提醒")

        # 截断日志输出中的 base64 数据，避免日志过长
        truncated_msg = _truncate_message_for_log(final_user_message)
        logger.trace(f"🧠[GsCoreAIAgent] 用户消息: {truncated_msg}")

        # 记录用户输入到 session logger
        self._session_logger.log_run_start()
        self._session_logger.log_user_input(final_user_message)

        if tools is None:
            tools = []

        if self.create_by in ["SubAgent", "Chat", "Agent", "AutoPlanner"]:
            if not tools:
                qy = ""
                if isinstance(user_message, str):
                    qy = user_message
                elif ev is not None:
                    qy = ev.raw_text

                # 第一层：框架保底工具池（仅 self + buildin 分类，由 category 决定，无条件加载）。
                # planning 工具（kanban/artifact/record）不再保底——它们靠下方"状态驱动工具池
                # （L2）"按持久实体精确召回 + 向量检索（L4/L5）按需加载，避免每轮闲聊都常驻
                # 15 个规划工具 schema 抬高 Token 并稀释工具选择精度。
                core_tools = await get_main_agent_tools()
                core_names = {t.name for t in core_tools}

                # 第 1.5 层：状态驱动工具池（L2）——用户已有持久实体时把对应能力族补进保底：
                # 活跃 Kanban 任务→长期任务编排+产物族；未完成定时任务→定时任务族；
                # 名下有 record:* 集合→结构化记录族。解决"一小时后追问'改成后天'""追问任务
                # 产物原文"等无法靠单条语义召回的场景——无论本轮意图如何都生效。
                try:
                    from gsuid_core.ai_core.tool_state_signals import get_state_driven_family_tools

                    state_tools = await get_state_driven_family_tools(
                        ev, core_names, has_active_task=has_active_task, intent=intent
                    )
                    if state_tools:
                        core_tools = core_tools + state_tools
                        core_names.update(t.name for t in state_tools)
                except Exception as e:
                    logger.debug(f"🧠 [GsCoreAIAgent] 状态驱动工具池加载失败: {e}")

                # 第 1.6 层：会话驻留工具池（L3）——最近几轮用过的能力族继续常驻数轮，
                # 兜底"刚用过某能力、紧接着的追问语义却召不回该工具"（如改完别名又口头追加）。
                # 加载后递减 TTL 并清理到期项。
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

                # 第二层：语境工具池——根据群组画像标签自动加载相关工具集
                # （如原神群自动加载所有声明了 context_tags=["原神"] 的工具）
                if ev is not None and ev.group_id:
                    try:
                        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

                        scope_key = make_scope_key(ScopeType.GROUP, str(ev.group_id))
                        ctx_tags = await get_scope_context_tags(scope_key)
                        if ctx_tags:
                            ctx_tools = get_tools_by_context_tags(ctx_tags, max_count=8)
                            if ctx_tools:
                                extra_tools += ctx_tools
                                logger.debug(
                                    f"🧠 [GsCoreAIAgent] 语境工具池加载 {len(ctx_tools)} 个工具 (语境标签: {ctx_tags})"
                                )
                    except Exception as e:
                        logger.debug(f"🧠 [GsCoreAIAgent] 语境工具池加载失败: {e}")

                # 第三层：查询工具池——基于 query 的向量搜索。只排除已在保底池的
                # self / buildin 分类；planning 工具不再保底，必须保留在向量检索里按需
                # 召回（"闲聊里临时要记账/建任务/查产物"靠这一层 + L4 族展开拿到）。
                if qy:
                    # L5 上下文增强检索：把最近几轮用户原话拼进检索 query，
                    # 让"改成后天吧"这类无独立语义的追问也能借上文召回到正确工具族。
                    search_query = "\n".join([*self._recent_user_texts, qy]) if self._recent_user_texts else qy
                    logger.debug(f"🧠 [GsCoreAIAgent] 尝试搜索工具: {search_query}")
                    # 只排除已在保底池的 self/buildin；plugin_dev 等"委派专用"分类由
                    # search_tools 在检索层统一拦截（NON_SEARCHABLE_TOOL_CATEGORIES），
                    # 不必也不应在这里重复声明。
                    extra_tools += await search_tools(
                        query=search_query,
                        limit=8,
                        non_category=["self", "buildin"],
                    )

                # 附加池：先按能力族整族展开（L4），再去重/限量。
                # 召回族内任一工具即带出整族（剔除与保底重名/族内重复），
                # 保证"能创建就能改/删"——如召回 add_once_task 即带出
                # modify/cancel_scheduled_task，避免后续追问"改成后天"时无工具可调。
                MAX_EXTRA_TOOLS = 16
                deduped_extra = expand_tools_to_families(
                    extra_tools,
                    exclude_names=core_names,
                    max_tools=MAX_EXTRA_TOOLS,
                )

                # 保底工具全部保留；附加工具池已在族展开时限量
                tools = core_tools + deduped_extra

                # 委派保障：意图命中"工具对主人格隐藏、只能委派"的能力代理（如
                # plugin_developer_agent）时，确保 create_subagent 在池里。否则主人格
                # 既够不到那些工具、又没有委派入口，只能放弃或拿碎片工具硬拼。只补
                # create_subagent 本身（不做能力族展开），代价仅 +1 个工具 schema。
                if qy:
                    deleg_pid = _matched_delegation_only_profile(qy)
                    if deleg_pid and not any(t.name == "create_subagent" for t in tools):
                        cs = find_tool_base("create_subagent")
                        if cs is not None:
                            tools.append(cs.tool)
                            logger.debug(
                                f"🧠 [GsCoreAIAgent] 意图命中委派型画像 {deleg_pid}，注入 create_subagent 保障委派路径"
                            )

                logger.debug(
                    f"🧠 [GsCoreAIAgent] 工具数量: {len(tools)} (保底 {len(core_tools)} + 附加 {len(deduped_extra)})"
                )

                # L5：记录本轮用户原话，供下一轮上下文增强检索（保留窗口内的"上文"）
                if qy:
                    keep = max(_RECENT_TEXT_WINDOW - 1, 0)
                    self._recent_user_texts.append(qy)
                    self._recent_user_texts = self._recent_user_texts[-keep:] if keep else []
            else:
                logger.debug(f"🧠 [GsCoreAIAgent] 传入Tools列表: {len(tools)}，已传入参数")
        else:
            logger.debug("🧠 [GsCoreAIAgent] 不搜索工具")

        logger.debug(f"🧠 [GsCoreAIAgent] 工具列表: {[tool.name for tool in tools]}")

        # 最终去重（兼容外部直接传入 tools 的情况）
        tools = list({obj.name: obj for obj in tools}.values())
        tool_names = [t.name for t in tools]

        # 记录本次传给 AI 的工具列表
        self._session_logger.log_tools_list(tool_names)

        # 当 return_model 指定时，使用 output_type 让 pydantic_ai 强制结构化输出
        # output_type 默认为 str（返回文本），指定 Pydantic 模型时强制返回结构化 JSON
        _agent = Agent(
            model=self.model,
            deps_type=ToolContext,
            system_prompt=self.system_prompt or "你是一个智能助手, 简短的一句话回答问题即可。",
            model_settings={"max_tokens": self.max_tokens},
            tools=tools,
            toolsets=[skills_toolset],
            retries=3,
            output_type=output_type or str,
        )

        # 截断历史记录，避免无限制增长
        self.extract_history()

        # TTFT/TPS 流式统计：按"每次模型请求"打点，在对应 CallToolsNode 中结算入库。
        # _req_start 在 ModelRequestNode 发起前记录；_first/_last_event_at 由
        # node.stream() 的事件流逐 event 刷新。
        _req_start: float = 0.0
        _first_event_at: Optional[float] = None
        _last_event_at: Optional[float] = None
        _model_name: str = self.model.model_name if self.model else "unknown"
        _provider: str = self.model.system if self.model else "unknown"
        # 流式响应下需手动按完整文本重新拆分内嵌 <think> 标签（见
        # _split_embedded_thinking）。thinking_tags 取自模型 profile，默认 ('<think>','</think>')。
        _thinking_tags: tuple[str, str] = self.model.profile.thinking_tags if self.model else ("<think>", "</think>")

        try:
            logger.info("🧠 [GsCoreAIAgent] 开始执行 _agent.iter()...")
            logger.info(f"🧠 [GsCoreAIAgent] 当前 history: {len(self.history)}")

            async with _agent.iter(
                final_user_message,
                deps=context,  # type: ignore[arg-type]
                message_history=self.history,
                usage_limits=limits,
            ) as agent_run:
                # 遍历每一步 Node
                async for node in agent_run:
                    # 1. 发起大模型请求前的处理
                    if isinstance(node, ModelRequestNode):
                        logger.debug("🧠 [GsCoreAIAgent] ⚡ 触发节点: ModelRequestNode")

                        self._session_logger.log_node_transition("ModelRequestNode")

                        for part in node.request.parts:
                            if isinstance(part, ToolReturnPart):
                                # 如果工具返回b64图片或者bytes内容, 则调用RM实例上传
                                if (
                                    isinstance(part.content, str) and part.content.startswith("base64://")
                                ) or isinstance(part.content, bytes):
                                    resource_id = RM.register(part.content)
                                    logger.info(
                                        f"🧠 [GsCoreAIAgent] 工具 [{part.tool_name}] 返回内容，"
                                        f"已注册资源ID [{resource_id}]"
                                    )
                                    part.content = (
                                        f"[工具 {part.tool_name} 已生成内容, 但没有发送给用户，资源ID: {resource_id}]"
                                    )

                                # 返回的可能是对象也可能是字符串，这里为了打印转成 str
                                tool_result_str = str(part.content)
                                if len(tool_result_str) > 200:
                                    tool_result_str = tool_result_str[:200] + f"...[截断, 共{len(tool_result_str)}字符]"
                                logger.debug(
                                    f"[✅ 工具执行完毕]: 工具名称='{part.tool_name}', 结果给到Agent={tool_result_str}"
                                )
                                self._session_logger.log_tool_return(part.tool_name, part.content, part.tool_call_id)

                        logger.debug("🧠  ▶ [发起请求]: 正在等待大模型思考...")
                        # 以流式方式发起本轮模型请求并逐 event 打点：
                        # 普通的节点迭代走非流式请求，CallToolsNode 要等完整响应返回
                        # 后才产出，无法区分"首 token 延迟"与"生成耗时"。这里主动
                        # 消费 node.stream()，请求即转为流式；流结束后完整响应仍会
                        # 照常进入 CallToolsNode，后续工具调用/文本处理逻辑不受影响。
                        _req_start = time.perf_counter()
                        _first_event_at = None
                        _last_event_at = None
                        async with node.stream(agent_run.ctx) as request_stream:
                            async for _event in request_stream:
                                _last_event_at = time.perf_counter()
                                if _first_event_at is None:
                                    _first_event_at = _last_event_at

                    # 2. 获取到大模型响应，准备调用工具或者输出文本
                    # 这里使用了 isinstance，Pyright 就能明确知道此时 node 是 CallToolsNode，拥有 model_response 属性
                    elif isinstance(node, CallToolsNode):
                        logger.debug("🧠 [GsCoreAIAgent] ⚡ 触发节点: CallToolsNode")

                        self._session_logger.log_node_transition("CallToolsNode")

                        # 流式请求下 pydantic_ai 未必能拆出内嵌 <think> 标签（仅当标签作为
                        # 独立 SSE chunk 到达时才拆），MiniMax 等网关不保证这点，导致
                        # <think>...</think> 残留在 TextPart 里。这里原地按完整文本重新拆分，
                        # 与非流式路径对齐：既避免思考内容经显示循环 / result.output 泄漏到 C 端，
                        # 也补回意图-行为检测所需的 ThinkingPart。原地改写同一 model_response 对象，
                        # 故 history 与 result.output 一并保持干净；ToolCallPart 原样保留，工具执行不受影响。
                        node.model_response.parts = _split_embedded_thinking(node.model_response.parts, _thinking_tags)

                        # 遍历大模型返回的具体片段 (Parts)
                        for part in node.model_response.parts:
                            # 拦截到模型即将调用工具
                            if isinstance(part, ToolCallPart):
                                logger.debug(f"[🔧 大模型请求调用工具]: 工具名称='{part.tool_name}', 参数={part.args}")
                                _tool_call_list.append(part.tool_name)
                                self._session_logger.log_tool_call(part.tool_name, part.args, part.tool_call_id)

                                # 代码层前摇触发：耗时工具调用前，主动发送一句角色化台词，
                                # 避免用户面对沉默等待。每次运行最多发送 N 句，防止刷屏。
                                if (
                                    bot
                                    and return_mode in ["always", "by_bot"]
                                    and _pre_tool_sent < _MAX_PRE_TOOL_EXPRESSIONS_PER_RUN
                                ):
                                    pre_expr = _get_pre_tool_expression(self.persona_name, part.tool_name)
                                    if pre_expr:
                                        _pre_tool_sent += 1
                                        try:
                                            await send_chat_result(bot, pre_expr, ev=ev)
                                        except Exception as _e:
                                            logger.debug(f"🧠 [GsCoreAIAgent] 前摇发送失败: {_e}")

                            # 大模型直接输出文本
                            elif isinstance(part, TextPart):
                                _text = part.content.strip()
                                # 拆出 <think> 后只剩空白的文本片段（如纯思考+工具调用轮），
                                # 既无需打印也无需下发，直接跳过，避免空的「大模型文本」噪声日志。
                                if not _text:
                                    continue
                                logger.debug(f"🧠 [大模型文本]: {_text}")
                                self._session_logger.log_text_output(_text)
                                if _text in SILENCE_MARKERS:
                                    logger.info(f"🧠 [GsCoreAIAgent] 检测到沉默标记 '{_text}'，跳过发送")
                                elif bot and _text and return_mode in ["always", "by_bot"]:
                                    # Why: send_chat_result 抛异常会穿透 _agent.iter() 的
                                    # async context，触发 pydantic_graph 的 athrow/cancel scope
                                    # 错误。必须在循环体内吞掉发送侧的故障。
                                    try:
                                        await send_chat_result(bot, _text, ev=ev)
                                    except Exception as _e:
                                        logger.debug(f"🧠 [GsCoreAIAgent] 文本发送失败: {_e}")

                            elif isinstance(part, ThinkingPart):
                                _thinking = part.content.strip()
                                logger.debug(f"🧠 [大模型思考]: {_thinking}")
                                if _thinking:
                                    _thinking_segments.append(_thinking)
                                self._session_logger.log_thinking(_thinking)
                                if bot and _thinking:
                                    pass

                        # 结算本轮模型请求的性能统计：
                        # TTFT = 请求发起 → 首个流式 event；生成耗时 = 首个 → 最后一个 event；
                        # TPS 用本轮响应自身的 usage（而非整个 run 的累计值）计算
                        _ttft_ms: float = 0.0
                        _tps: float = 0.0
                        _req_usage = node.model_response.usage
                        if _first_event_at is not None and _last_event_at is not None:
                            _ttft_ms = round((_first_event_at - _req_start) * 1000, 2)
                            _generation_time = _last_event_at - _first_event_at
                            if _req_usage.output_tokens > 0 and _generation_time > 0:
                                _tps = round(_req_usage.output_tokens / _generation_time, 2)
                            logger.debug(f"⏱️ [GsCoreAIAgent] TTFT: {_ttft_ms:.2f} ms, TPS: {_tps:.2f} tokens/s")
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
                        logger.debug("🧠 [GsCoreAIAgent] ⚡ 触发节点: End")
                        logger.debug("  ✅ [运行结束]: 最终结果生成完毕")
                        self._session_logger.log_node_transition("End")

            # 遍历完成后，直接从 agent_run 中获取最终结果
            result = agent_run.result
            if result:
                logger.info("🧠 [GsCoreAIAgent] _agent.iter() 执行成功!")

                self.history.extend(result.new_messages())

                # L3：记录本轮实际调用过的工具所属能力族，使其在随后数轮继续常驻，
                # 兜底紧邻的同主题追问（语义本身可能召不回该工具）。
                if _tool_call_list:
                    for _tname in set(_tool_call_list):
                        _tb = find_tool_base(_tname)
                        _dom = _tb.capability_domain if _tb else None
                        if _dom:
                            self._recent_tool_families[_dom] = _STICKY_FAMILY_TURNS

                # 更新连续无工具调用计数（仅对交互式主 Agent 生效）
                if self.create_by in ["Chat", "Agent"]:
                    if _tool_call_list:
                        self._consecutive_no_tool_rounds = 0
                    else:
                        self._consecutive_no_tool_rounds += 1
                        # 单轮意图-行为不一致检测：thinking 里点名了某工具 / 长任务
                        # 编排意图却没真正调用——直接顶到阈值，下一轮立刻强制提醒。
                        # 纯规则字符串匹配，零额外 LLM 成本。
                        thinking_blob = "\n".join(_thinking_segments)
                        if thinking_blob and any(kw in thinking_blob for kw in _INTENT_TRIGGER_KEYWORDS):
                            self._consecutive_no_tool_rounds = max(self._consecutive_no_tool_rounds, 2)
                            logger.debug("🧠 [GsCoreAIAgent] 检测到意图-行为不一致，下一轮将强制提醒")

                # 记录 Token 使用量和延迟统计
                # 记录响应延迟
                latency = time.time() - start_time
                statistics_manager.record_latency(latency=latency)

                try:
                    usage_obj: RunUsage = result.usage()
                    input_tokens: int = usage_obj.input_tokens
                    output_tokens: int = usage_obj.output_tokens
                    cache_read_tokens: int = usage_obj.cache_read_tokens
                    cache_write_tokens: int = usage_obj.cache_write_tokens

                    logger.info(
                        f"📊 [GsCoreAIAgent] Token消耗: input={input_tokens}, output={output_tokens}, "
                        f"cache_read={cache_read_tokens}, cache_write={cache_write_tokens}"
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
                        self._session_logger.log_token_usage(
                            input_tokens,
                            output_tokens,
                            _model_name,
                            cache_read_tokens,
                            cache_write_tokens,
                        )
                except AttributeError as e:
                    # result 没有 usage 属性（如 pydantic_graph End 节点返回的结果）
                    logger.info(f"📊 [GsCoreAIAgent] result.usage 访问失败: {e}")
                    pass
                except Exception as e:
                    logger.warning(f"📊 [GsCoreAIAgent] 记录统计失败: {e}")

                # 当 return_model 指定时，直接返回 Pydantic 模型实例
                if output_type is not None:
                    self._session_logger.log_run_end()
                    self._session_logger.log_result(result.output, _tool_call_list)
                    return result.output

                # 始终返回字符串类型
                result_msg = str(result.output).strip()
                # 工具调用列表只进调试日志，不追加到用户可见消息
                if _tool_call_list:
                    logger.debug(f"🔧 [本次工具调用] {', '.join(_tool_call_list)}")

                self._session_logger.log_run_end()
                self._session_logger.log_result(result_msg, _tool_call_list)

                if return_mode in ["by_bot"] and bot and ev:
                    return ""
                return result_msg

            # result 为空时的默认返回值
            return "Agent 执行完成，但未返回有效结果"

        except UsageLimitExceeded:
            # 达到限制后的处理逻辑
            logger.warning(f"🧠 [PydanticAI] Agent 达到最高思考轮数限制 {limits.request_limit}")
            statistics_manager.record_error(error_type="usage_limit")
            self._session_logger.log_error("usage_limit", f"达到最高思考轮数限制 {limits.request_limit}")

            # 子代理（return 模式，如 Kanban 能力代理 / plugin_developer_agent）：
            # **绝不**直接对用户的 bot 说话，也**绝不**把超轮数的中间产物强制总结后回灌
            # 给用户——那些中间文本往往是大段代码 / 原始数据，直接下发会造成群聊刷屏与
            # 污染。只返回一句简短状态，由 Kanban 转译层（_persona_relay）决定要不要、用
            # 什么口吻告诉用户。原"安抚 + 强制总结 + send_chat_result"逻辑仅服务于面向
            # 用户的主人格（by_bot / always 模式）。
            if return_mode == "return":
                return (
                    "⚠️ 已达最大思考轮数，未能在限定步数内完成本任务。"
                    "中间产物（如已写入的文件 / artifact）已留在工作区，未回传以避免刷屏。"
                )

            # 安抚用户
            if bot:
                await bot.send("⏳ 思考链过长，正在根据已有线索为你整理最终结论...")

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
                logger.error(f"🧠 [PydanticAI] 强制总结失败: {e}")
                self._session_logger.log_error("fallback_failed", str(e))
                fallback_error = (
                    "⚠️ 问题较复杂，现有信息不足以给出准确答案。可以尝试提高思维链长度，或换个方式描述问题。"
                )
                if bot:
                    await bot.send(fallback_error)
                    return ""
                return fallback_error

        except httpx.TimeoutException as e:
            # HTTP 请求超时
            logger.warning(f"🧠 [PydanticAI] Agent 运行异常: 请求超时 {e}")
            statistics_manager.record_error(error_type="timeout")
            self._session_logger.log_error("timeout", str(e))
            return "执行出错: 请求超时"

        except httpx.HTTPError as e:
            # 其他 HTTP 错误（网络相关）
            error_str = str(e).lower()
            if "rate" in error_str or "429" in error_str or "limit" in error_str:
                logger.warning(f"🧠 [PydanticAI] Agent 运行异常: Rate Limit {e}")
                statistics_manager.record_error(error_type="rate_limit")
                self._session_logger.log_error("rate_limit", str(e))
            else:
                logger.warning(f"🧠 [PydanticAI] Agent 运行异常: 网络错误 {e}")
                statistics_manager.record_error(error_type="network_error")
                self._session_logger.log_error("network_error", str(e))
            return f"执行出错: {str(e)}"

        except Exception as e:
            logger.error(f"🧠 [PydanticAI] Agent 运行异常: {e}")
            logger.exception("🧠 [PydanticAI] 异常详情:")
            err_str = str(e)
            if "529" in err_str:
                statistics_manager.record_error(error_type="api_529_error")
            else:
                statistics_manager.record_error(error_type="agent_error")
            self._session_logger.log_error("agent_error", err_str)
            # 自愈：推理端「Failed to download image」基本都是历史里残留了过期的
            # 远程图片 URL，不清掉会导致之后每一轮重发都 500、整个会话卡死。这里把
            # 历史中的过期远程图片剥离成文字占位，使下一轮能自动恢复。
            if "download image" in err_str.lower():
                stripped = _strip_remote_images_from_history(self.history)
                if stripped:
                    logger.warning(
                        f"🧠 [GsCoreAIAgent] 图片下载失败，已从历史剥离 {stripped} 处过期远程图片，下一轮自动恢复"
                    )
            return f"执行出错: {str(e)}"
        finally:
            # 清理本轮的单轮节流计数（scheduler.py add_once_task 等共享），
            # 防止内存中 key 无限累积。session_id 缺失时跳过——本轮也没机会
            # 写入计数。
            try:
                from gsuid_core.ai_core.buildin_tools.scheduler import (
                    clear_turn_throttle,
                )

                sess = ev.session_id if ev is not None else None
                if sess:
                    clear_turn_throttle(str(sess), turn_id)
            except Exception as _e:
                logger.debug(f"🧠 [GsCoreAIAgent] 清理单轮节流计数失败: {_e}")

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
            intent: 本轮意图标签（闲聊/工具/问答）。当前工具装配不再据此精简（planning
                已退出保底池、改由状态驱动 + 向量检索按需召回），保留参数仅作调用方兼容。

        Returns:
            Agent 执行结果。默认返回 str，当 output_type 指定时返回对应模型实例
        """
        async with self._run_lock:
            logger.info("🧠 [GsCoreAIAgent] 获取到执行锁，开始执行...")
            # O-A 群聊队头阻塞防护：拿到锁时若已排队过久（话题大概率翻篇），丢弃过期回复。
            if (
                enqueue_ts is not None
                and self.create_by == "Chat"
                and (time.time() - enqueue_ts) > STALE_CHAT_REQUEST_TTL
            ):
                waited = time.time() - enqueue_ts
                logger.info(f"🧠 [GsCoreAIAgent] 队列等待 {waited:.1f}s 超 TTL，丢弃过期请求，释放锁")
                return "" if output_type is None else None
            result = await self._execute_run(
                user_message=user_message,
                bot=bot,
                ev=ev,
                rag_context=rag_context,
                tools=tools,
                return_mode=return_mode,
                output_type=output_type,
                intent=intent,
                has_active_task=has_active_task,
            )
            logger.info("🧠 [GsCoreAIAgent] 执行完成，释放锁")
            return result


# 工厂函数
def create_agent(
    system_prompt: Optional[str] = None,
    max_tokens: int = 30000,
    max_iterations: Optional[int] = None,
    persona_name: Optional[str] = None,
    create_by: str = "LLM",
    max_history: int = 20,
    task_level: Literal["high", "low"] = "high",
    session_id: Optional[str] = None,
    is_subagent: bool = False,
) -> GsCoreAIAgent:
    """
    创建 PydanticAI Agent 实例

    Args:
        model_name: 模型名称
        system_prompt: 系统提示词
        max_tokens: 最大输出 token 数
        max_iterations: 最大迭代次数限制，None 时使用配置默认值
        persona_name: Persona 名称（用于热重载检测）
        task_level: 任务级别，"high"表示高级任务，"low"表示低级任务
        session_id: 会话 ID，用于关联 session 日志
        is_subagent: 是否为 SubAgent，为 True 时日志存放于独立子目录

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
    # 不再传固定的 "build_persona" session_id：让 __init__ 自动派生
    # auto_BuildPersona_* 的一次性 subagent 日志（落 subagents/ 子目录，
    # 不污染主 session 列表）。详见 docs/AI_SESSION_LOGGING.md。
    agent = create_agent(
        system_prompt=CHARACTER_BUILDING_TEMPLATE,
        create_by="BuildPersona",
        task_level="high",
    )
    response = await agent.run(query)
    return response.strip()
