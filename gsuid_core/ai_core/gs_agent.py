"""
PydanticAI Agent 核心模块
基于 pydantic_ai 实现的轻量级 Agent
"""

import re
import time
import uuid
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
    UserContent,
    ModelMessage,
    ModelRequest,
    ThinkingPart,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.anthropic import AnthropicModel

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core import wall_clock, output_firewall, interaction_scaffold
from gsuid_core.ai_core.const import (
    _SKILLS_CREATE_BY,
    _AGENTIC_CREATE_BY,
    _STICKY_FAMILY_TURNS,
    STALE_CHAT_REQUEST_TTL,
    _INTENT_TRIGGER_KEYWORDS,
    ENABLE_PROGRESSIVE_TOOLS,
    _PROGRESSIVE_TOOLS_SKIP_INTENTS,
)
from gsuid_core.ai_core.utils import (
    SILENCE_MARKERS,
    send_chat_result,
    _relean_user_turn,
    _extract_run_context,
    _is_content_rejected,
    materialize_image_url,
    _split_embedded_thinking,
    _drop_orphan_tool_results,
    _truncate_message_for_log,
    _is_non_retryable_model_error,
    _strip_remote_images_from_history,
    _truncate_history_with_tool_safety,
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
    get_model_for_task,
    get_model_by_full_name,
    get_config_name_for_task,
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

# 父 run 把本次归属 scope 写入此 contextvar，途中 spawn 的嵌套子 agent 自动继承记账：
# await 的子协程共享 Context、create_task 复制创建时 Context，两条 spawn 路径都覆盖。
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
# 闭类完成动词 + 第一人称施动锚点（语言学范畴，非某个评测域的词表）；不做天气/股价等
# **数据域**词表——数据编造的防线在 prompt 合规层，域词表是对测试集的过拟合（已移除）。
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
_INTERACTIVE_CREATE_BY = ("Chat", "Agent", "TEST")

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


# scope_key（记忆 scope，见 memory/scope.py）→ 可嵌进 session_id 的一段指向标识：
# group:789012 → group-789012 / user_global:12345 → uglobal-12345 /
# user_in_group:u@g → uingroup-u@g / self:x → self-x。用短码而非原 scope_type，避免
# user_global/user_in_group 自带的下划线破坏 session_id 的 "_" 分词。前端据此显示"针对哪个群/用户"。
_SCOPE_SEG_CODE: dict = {
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
        openai_chat_model: Optional[Union[OpenAIChatModel, OpenAIResponsesModel, AnthropicModel]] = None,
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
        self.max_tokens = _max_tokens
        self.max_iterations = max_iterations  # 自定义迭代次数限制，None时使用配置默认值
        # C-4 墙钟软预算(秒)覆写：None=沿用全局 scaffold_wall_clock_budget；<=0=本 Agent 关闭软预算。
        # 长流程入口（画布编排等，一轮几十次工具调用 + 等人确认）必须放宽，否则永远跑不到终态。
        self.wall_clock_budget = wall_clock_budget
        # 轨迹观察者：让宿主（画布前端的"思考过程"折叠块等）看见模型推理与工具调用，
        # 不必去翻 session log。None = 不观察（零开销）；契约见 _emit_trace。
        self.on_trace = on_trace
        self.task_level: Literal["high", "low"] = task_level  # 任务级别，用于选择对应的模型配置

        self.create_by = create_by
        # 未显式给 session_id 的来源（能力评估 / meme 打标 / 记忆摄入·检索等后台 LLM
        # 调用）自动派生一个一次性 subagent id——这样"所有调用来源都写 session log"
        # 在结构上得到保证，无法被某个来源遗漏。详见 docs/AI_SESSION_LOGGING.md。
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

        # 连续无工具调用计数：连续多轮只输出文本、不调用任何工具时，
        # 下一轮注入强制提醒，防止 Agent 以角色无知为由持续推脱
        self._consecutive_no_tool_rounds: int = 0

        # L3 会话驻留：最近使用过的能力族 → 剩余可常驻轮数（每轮递减）。
        # 兜底"刚用过某能力、紧接着的追问语义却召不回该工具"的场景。
        self._recent_tool_families: dict[str, int] = {}
        # 本轮实际装配（保底 + 附加）工具的能力域集合，run() 装配后回填。供 handle_ai
        # 偏好注入做"精确能力域过滤"（只注入本轮可用工具相关的软偏好），见
        # get_assembled_capability_domains()。
        self._last_assembled_domains: set[str] = set()
        # L5 上下文增强检索：最近几轮用户原话，拼进工具向量检索 query，
        # 让"改成后天吧"这类无独立语义的追问也能借上文召回到正确工具族。
        self._recent_user_texts: List[str] = []
        # by_bot 单轮已发送文本去重集合：弱模型常跨轮重复同一段最终答复，叠加瞬时
        # 故障重试重发，会让 C 端收到两段相同的话。每个用户轮次在 _execute_run 重置。
        self._run_sent_texts: set[str] = set()
        # C-2 漂移预算的上轮计数：只在计数**增加**时注入提醒，防一次 push 滞留
        # recent 窗口导致后续每轮重复唠叨（会话级状态，正是"预算"的容器）。
        self._last_drift_push_count: int = 0

        self.model: Optional[Union[OpenAIChatModel, OpenAIResponsesModel, AnthropicModel]] = openai_chat_model
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
        # system_prompt 由 AISessionLogger 内部记一条 system_prompt entry，
        # 这里不再重复调用 log_system_prompt（避免与旧逻辑重复落两遍）。
        self._session_logger: AISessionLogger = AISessionLogger(
            session_id=session_id,
            system_prompt=system_prompt,
            persona_name=persona_name,
            create_by=create_by,
            is_subagent=is_subagent,
        )

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
            logger.debug(f"🧠 [GsCoreAIAgent] on_trace 观察者异常（已忽略）: {e}")

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
            self.history = _truncate_history_with_tool_safety(
                self.history,
                self.max_history,
            )
        # 兜底：无论是否截断，都做一次孤儿工具结果清理，确保历史对 API 自洽
        self.history = _drop_orphan_tool_results(self.history)
        after: int = len(self.history)
        # 仅「因超长主动裁剪且确有条目被丢弃」才打 auto_compact（供 webconsole 画独立色块）；
        # 纯孤儿清理属结构性整理、stateless 模式每轮清空，均不打标以免噪声。
        if truncated and after < before:
            self._session_logger.log_history_reset("auto_compact", {"before": before, "after": after})
        logger.debug(i18n_t("🧠 [GsCoreAIAgent] 历史记录已处理至 {p0} 条", p0=len(self.history)))

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
                    "🧠 [GsCoreAIAgent] 检测到{p0}级模型配置变更为 {current}，但加载失败，沿用原模型: {e}",
                    p0=self.task_level,
                    current=current,
                    e=e,
                )
            )
            return False

        old = self.model_config_name
        # 旧模型不关底层 client：本项目所有模型共享 pydantic-ai 进程级缓存 httpx 客户端，
        # close 会拖垮全进程会话（曾致所有请求报 client has been closed），交给 GC 即可。
        self.model = new_model
        self.model_config_name = current
        self.model_config_fingerprint = current_fp
        # 全名变=换配置文件；全名同指纹变=原地改了当前配置文件字段。
        change_desc = f"{old} → {current}" if old != current else f"{current}（配置内容已更新）"
        logger.info(
            i18n_t(
                "🧠 [GsCoreAIAgent] 检测到{p0}级模型配置变更 {change_desc}，"
                "已为 Session {p1} 热替换模型（保留对话历史，无需 coreclear）",
                p0=self.task_level,
                change_desc=change_desc,
                p1=self.session_id,
            )
        )
        return True

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
                        logger.warning(
                            i18n_t("🖼️ [GsCoreAIAgent] 图片入历史前物化失败，跳过该图片: {p0}", p0=item.url[:120])
                        )
                        continue
                    result.append(ImageUrl(url=url))
                else:
                    result.append(item)
            return result

        # 模型不支持图片，调用图片理解模块转述
        if image_urls:
            logger.info(
                i18n_t("🖼️ [ImageUnderstand] 当前模型不支持图片，开始图片理解转述，共 {p0} 张图片", p0=len(image_urls))
            )
            # 用户问题：用于把冗长的图片描述按需精简到与问题相关的部分
            user_question = "\n".join(text_parts).strip()
            descriptions: list[str] = []
            for idx, url in enumerate(image_urls):
                try:
                    description = await understand_image(image_url=url, parent_session_id=self.session_id)
                    description = await self._summarize_image_description(description, user_question)
                    descriptions.append(f"图片{idx + 1}: {description}")
                except Exception as e:
                    logger.error(i18n_t("🖼️ [ImageUnderstand] 图片 {p0} 理解失败: {e}", p0=idx + 1, e=e))
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
                logger.debug(
                    i18n_t(
                        "🖼️ [ImageUnderstand] 图片描述二次摘要: {p0} -> {p1} 字符", p0=len(description), p1=len(summary)
                    )
                )
                return summary
        except Exception as e:
            logger.debug(i18n_t("🖼️ [ImageUnderstand] 图片描述二次摘要失败，使用原始描述: {e}", e=e))
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

        for attempt in range(1, max_attempts + 1):
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
                )
            except Exception as e:
                err_str = str(e)
                # 自愈：过期远程图片导致的下载失败会让后续每轮都 500，先剥离历史里的
                # 过期远程图片，让本次重试（及下一轮）用干净历史恢复。
                if "download image" in err_str.lower():
                    stripped = _strip_remote_images_from_history(self.history)
                    if stripped:
                        logger.warning(
                            i18n_t(
                                "🧠 [GsCoreAIAgent] 图片下载失败，已从历史剥离 {stripped} 处过期远程图片",
                                stripped=stripped,
                            )
                        )

                # 永久性 4xx（内容审核拦截 / 请求非法等）：重试必复现，直接 fail-fast，
                # 不再消耗剩余重试次数。
                non_retryable = _is_non_retryable_model_error(e)

                if attempt < max_attempts and not non_retryable:
                    logger.warning(
                        i18n_t(
                            "🧠 [PydanticAI] 核心请求第 {attempt}/{max_attempts} 次失败，{retry_delay}s 后重试: {e}",
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
                                "🧠 [PydanticAI] 模型拒绝处理本次输入（内容审核 {p0}）: {err_str}",
                                p0=e.status_code,
                                err_str=err_str,
                            )
                        )
                        statistics_manager.record_error(error_type="content_rejected")
                        self._session_logger.log_error("content_rejected", err_str)
                        return "执行出错: 内容被模型安全策略拒绝"
                    logger.warning(
                        i18n_t(
                            "🧠 [PydanticAI] 模型返回客户端错误（{p0}，不重试）: {err_str}",
                            p0=e.status_code,
                            err_str=err_str,
                        )
                    )
                    statistics_manager.record_error(error_type="client_error")
                    self._session_logger.log_error("client_error", err_str)
                    return f"执行出错: {err_str}"

                # 已达最大尝试次数：按异常类型记录统计 + 写 session 日志并返回错误文案
                if isinstance(e, httpx.TimeoutException):
                    logger.warning(i18n_t("🧠 [PydanticAI] Agent 运行异常: 请求超时 {e}", e=e))
                    statistics_manager.record_error(error_type="timeout")
                    self._session_logger.log_error("timeout", err_str)
                    return "执行出错: 请求超时"
                if isinstance(e, httpx.HTTPError):
                    low = err_str.lower()
                    if "rate" in low or "429" in low or "limit" in low:
                        logger.warning(i18n_t("🧠 [PydanticAI] Agent 运行异常: Rate Limit {e}", e=e))
                        statistics_manager.record_error(error_type="rate_limit")
                        self._session_logger.log_error("rate_limit", err_str)
                    else:
                        logger.warning(i18n_t("🧠 [PydanticAI] Agent 运行异常: 网络错误 {e}", e=e))
                        statistics_manager.record_error(error_type="network_error")
                        self._session_logger.log_error("network_error", err_str)
                    return f"执行出错: {err_str}"

                logger.error(i18n_t("🧠 [PydanticAI] Agent 运行异常: {e}", e=e))
                logger.exception(i18n_t("🧠 [PydanticAI] 异常详情:"))
                if "529" in err_str:
                    statistics_manager.record_error(error_type="api_529_error")
                else:
                    statistics_manager.record_error(error_type="agent_error")
                self._session_logger.log_error("agent_error", err_str)
                return f"执行出错: {err_str}"

        # range(1, max_attempts + 1) 至少一次循环，正常不可达
        return "执行出错: 未知错误"

    async def _ooc_rewrite_and_send(
        self,
        blocked: List[Tuple[str, output_firewall.FirewallHit]],
        bot: Bot,
        ev: Optional[Event],
    ) -> None:
        """出戏命中后的重说闭环（§D.4）：无工具轻量 Agent 带警告重写一次，产物直接放行。

        误杀的代价只是多一次生成；重写本身失败才退到 ``PERSONA_FALLBACK_TEXT``。
        重写后把 history 里被拦的原文换成重写版，防出戏原文被后续轮模仿。
        """
        original = "\n\n".join(text for text, _ in blocked)
        first_hit = blocked[0][1]
        rewrite_message = (
            f"{output_firewall.build_rewrite_warning(first_hit)}\n\n"
            f"【被拦下的原文】\n{original}\n\n"
            "请保持原意、用你的角色口吻重写这段话，直接输出重写后的内容，不要解释。"
        )
        rewritten = ""
        try:
            _rewrite_agent = Agent(
                model=self.model,
                system_prompt=self.system_prompt or "你是一个智能助手。",
                model_settings={"max_tokens": self.max_tokens},
                tools=[],
                toolsets=[],
                retries=0,
                output_type=str,
            )
            rewrite_result = await _rewrite_agent.run(
                rewrite_message,
                message_history=[],
                usage_limits=UsageLimits(request_limit=1),
            )
            rewritten = str(rewrite_result.output).strip()
        except Exception as e:
            logger.warning(i18n_t("[OutputFirewall] 重说生成失败，使用角色化兜底: {e}", e=e))
        if not rewritten or rewritten in SILENCE_MARKERS:
            rewritten = output_firewall.PERSONA_FALLBACK_TEXT
        self._session_logger.log_text_output(rewritten)
        try:
            await send_chat_result(bot, rewritten, ev=ev, ooc_check=False)
            self._run_sent_texts.add(rewritten)
        except Exception as e:
            logger.debug(i18n_t("🧠 [GsCoreAIAgent] 重说发送失败: {e}", e=e))
        blocked_texts = {text for text, _ in blocked}
        for msg in reversed(self.history):
            if not isinstance(msg, ModelResponse):
                continue
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content.strip() in blocked_texts:
                    part.content = rewritten

    def _scrub_fake_done_history(self, fabricated_texts: set[str]) -> None:
        """纠正重跑成功后的历史外科（假完成闸收尾）：删掉纠正 nudge 的 user turn 与
        被暂扣未发出的编造声明，让持久历史 = 原始用户消息 + 纠正后的真实回复——与
        用户实际所见一致，也防编造话术 /「（系统校验…」句式被后续轮模仿。
        只扫尾部本轮产物；编造声明按 stripped 文本精确匹配，零误删。
        """
        tail = self.history[-8:]
        kept: List[ModelMessage] = []
        for msg in tail:
            if isinstance(msg, ModelRequest) and any(
                isinstance(p, UserPromptPart) and isinstance(p.content, str) and _FAKE_DONE_NUDGE in p.content
                for p in msg.parts
            ):
                continue
            if isinstance(msg, ModelResponse):
                parts = [
                    p for p in msg.parts if not (isinstance(p, TextPart) and p.content.strip() in fabricated_texts)
                ]
                if not parts:
                    continue
                if len(parts) != len(msg.parts):
                    msg.parts = parts
            kept.append(msg)
        self.history[-8:] = kept

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
        # scope 用于记账与闸门：显式 ev > 实例绑定 > contextvar（父 run 透传）。
        # 仅 budget_gate=True 的自主入口在此早退；放行/未启用/豁免均零额外开销。
        _budget_scope = self._resolve_budget_scope(ev)
        if budget_gate and _budget_scope is not None:
            try:
                from gsuid_core.ai_core.budget import budget_manager

                _bd = await budget_manager.check_scope(
                    _budget_scope[0], _budget_scope[1], _budget_scope[2], self.session_id
                )
            except SQLAlchemyError as _be:
                logger.warning(i18n_t("💰 [GsCoreAIAgent] 预算校验 DB 异常，放行本次 run: {_be}", _be=_be))
                _bd = None
            except Exception as _be:
                logger.exception(i18n_t("💰 [GsCoreAIAgent] 预算校验未知异常，放行本次 run: {_be}", _be=_be))
                _bd = None
            if _bd is not None and not _bd.allowed:
                logger.info(
                    i18n_t(
                        "💰 [GsCoreAIAgent] 预算超额拦截 create_by={p0} ({p1})",
                        p0=self.create_by,
                        p1=_bd.block_scope_label,
                    )
                )
                # 仅交互式（有 bot）且本次应提示时向用户发一句；自主后台（无 bot）静默掐断。
                if bot is not None and _bd.notify and _bd.message:
                    try:
                        await bot.send(_bd.message)
                    except Exception as _se:
                        logger.warning(i18n_t("💰 [GsCoreAIAgent] 预算超额提示发送失败: {_se}", _se=_se))
                return None if output_type is not None else ""

        # 提前到 try 前设置归属 scope：使本次 run 期间未显式绑定 scope 的嵌套 LLM 调用（含
        # _prepare_user_message 的图片理解）都按此记账；finally 还原，泄漏至多止于本 task。
        _budget_scope_token = _current_budget_scope.set(_budget_scope) if _budget_scope is not None else None

        _tool_call_list: list[str] = []  # 用于记录本次运行中被调用的工具列表，供后续统计使用
        _wall_nudged = False  # C-4 墙钟软预算：每 run 至多注入一次收敛提示
        # 出戏防火墙拦下的文本段（§D.4）：iter 结束后走"提醒→重说→放行"闭环
        _ooc_blocked: List[Tuple[str, output_firewall.FirewallHit]] = []
        # 假完成预检暂扣的文本段：声明完成但至今零工具——iter 后按"动作是否真发生"补发或纠正
        _fab_blocked: list[str] = []
        _thinking_segments: list[str] = []  # 累积本轮模型 thinking 文本，供意图-行为一致性检测

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

        logger.info(i18n_t("🧠 [GsCoreAIAgent] ====== Agent 运行开始 ======"))
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

        # 只含用户真实发言+图片的精简版：run 后用它替换写入 history 的 user turn，
        # 避免 rag_context 快照逐轮累积进持久历史（§优化 O-1）。
        _lean_user_message: Union[str, List[UserContent]] = (
            list(final_user_message) if isinstance(final_user_message, list) else final_user_message
        )

        if rag_context:
            final_user_message = _append_user_text(final_user_message, f"\n\n{rag_context}")
            logger.info(i18n_t("🧠[GsCoreAIAgent] 已添加 RAG 上下文"))

        # DS 专属角色扮演模式（inner_os）：仅在 Chat 模式首轮 user_message 末尾追加
        if (
            self.create_by == "Chat"
            and not self.history
            and ai_config.get_config("enable_deepseek_rp").data
            and isinstance(final_user_message, str)
        ):
            final_user_message = f"{final_user_message}{INNER_OS_MARKER}"
            logger.info(i18n_t("🧠[GsCoreAIAgent] 已注入 DS 角色扮演 Marker（首轮 Chat）"))

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
            final_user_message = _append_user_text(final_user_message, no_tool_reminder)
            logger.debug(i18n_t("🧠 [GsCoreAIAgent] 已注入连续无工具调用强制提醒"))

        # ── 交互脚手架（C-1/C-2/C-3，见 interaction_scaffold）：仅交互式主 Agent 生效 ──
        _addr_gated = False
        _followup_detected = False
        if self.create_by in _INTERACTIVE_CREATE_BY:
            # 只看**当前消息**（含本轮 @ 标注），绝不用 final_user_message——后者已拼进
            # rag_context（历史+记忆），历史里的 @别人标注与助手自称会污染寻址/自称判定。
            # last_user_question 就是 user_message 的纯文本拼接（上方已算），别重复 join。
            _cur_text = last_user_question
            _probe = ev.raw_text if ev is not None and ev.raw_text else last_user_question
            _is_tome = bool(ev.is_tome) if ev is not None else False
            _recent = interaction_scaffold.recent_history_texts(self.history)
            # 触发阈值可配置（ai_config），默认值按评测分布标定，上线后按生产日志重标
            _followup_maxlen = int(ai_config.get_config("scaffold_followup_max_len").data)
            _ambient_maxlen = int(ai_config.get_config("scaffold_ambient_max_len").data)
            _hints: list[str] = []
            _addr_gated = interaction_scaffold.addressed_to_someone_else(
                _cur_text, self.persona_name or "", _is_tome
            ) or interaction_scaffold.ambient_followup_to_other(
                _cur_text, _recent, self.persona_name or "", _is_tome, max_len=_ambient_maxlen
            )
            if _addr_gated:
                _hints.append(interaction_scaffold.ADDRESS_GATE_HINT)
                logger.info(i18n_t("🧭 [Scaffold] C-3 寻址门：这条不是冲你来的（@别人/催被@者），本轮砍掉工具集"))
            elif _probe:
                _ellipsis = interaction_scaffold.detect_ellipsis_followup(
                    _probe,
                    _recent,
                    recent_tool_call=interaction_scaffold.has_recent_tool_call(self.history),
                    max_len=_followup_maxlen,
                )
                if _ellipsis or interaction_scaffold.references_task_management(_cur_text):
                    _followup_detected = True  # 用于下方补调度族工具
                    if _ellipsis:
                        _hints.append(interaction_scaffold.FOLLOWUP_HINT)
                        logger.debug(i18n_t("🧭 [Scaffold] C-1 省略式跟进提示已注入"))
                # C-2 漂移预算：累积 ≥2 且比上轮**增加**才注入——单次 push 交 prompt 层
                # 既有条款（模型单轮守得住），提醒只针对连续软磨；不增加不重复唠叨。
                # speaker_id 让计数只累计同一说话人（群里两人各提一次意见≠一人连续软磨）。
                _pushes = interaction_scaffold.count_style_pushes(
                    _probe, _recent, speaker_id=str(ev.user_id) if ev is not None else ""
                )
                if _pushes >= 2 and _pushes > self._last_drift_push_count:
                    _hints.append(interaction_scaffold.DRIFT_REMINDER)
                    logger.debug(i18n_t("🧭 [Scaffold] C-2 漂移预算提醒已注入（累积 {_pushes} 次）", _pushes=_pushes))
                self._last_drift_push_count = _pushes
            for _h in _hints:
                final_user_message = _append_user_text(final_user_message, _h)

        # 截断日志输出中的 base64 数据，避免日志过长
        truncated_msg = _truncate_message_for_log(final_user_message)
        logger.trace(i18n_t("🧠[GsCoreAIAgent] 用户消息: {truncated_msg}", truncated_msg=truncated_msg))

        # 记录用户输入到 session logger
        self._session_logger.log_run_start()
        self._session_logger.log_user_input(final_user_message)

        if tools is None:
            tools = []

        # 渐进式工具暴露是否在本轮生效（仅自动装配 + 非闲聊轮）。决定是否挂 RetrievableToolset。
        _expose_dynamic = False

        # dynamic 能力族门：显式 True/False 优先；None 沿用旧门（agentic 且未传 tools）。
        if self.dynamic_tools is not None:
            _assemble = self.dynamic_tools
        else:
            _assemble = self.create_by in _AGENTIC_CREATE_BY and not tools

        # persona 会话与其 AgentNode 声明同步：packs 去掉 dynamic 即关闭五层自动装配，
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
                        "🧠 [GsCoreAIAgent] persona「{p0}」未声明 dynamic 能力族，按静态 packs+白名单装配 {p1} 个工具",
                        p0=self.persona_name,
                        p1=len(tools),
                    )
                )

        if _addr_gated:
            # C-3 装配层硬约束：@的是别人 → 本轮零工具（含 send_message_by_ai / find_tools）
            tools = []
        elif _assemble or self.create_by in _AGENTIC_CREATE_BY:
            if _assemble:
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

                # 调用方显式传入的基础工具（dynamic 节点的 packs+白名单）并入保底
                for _bt in tools:
                    if _bt.name not in core_names:
                        core_names.add(_bt.name)
                        core_tools.append(_bt)

                # 节点显式白名单：persona 投影节点在 config.json 声明的 tool_names 并入保底
                if self.persona_name:
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
                    logger.debug(i18n_t("🧠 [GsCoreAIAgent] 状态驱动工具池加载失败: {e}", e=e))

                # C-1 跟进保障：检测到"改成/取消那个/再查"类省略跟进时，把「定时任务」族
                # （list/modify/cancel/pause…）强制补进池——上一轮的动作目标可能建在别的
                # session、或本 agent 召不回，followup 文本本身又无调度语义（向量检索抓不到），
                # 没有这些工具模型只能凭空"已改/已取消"。与 tool_state_signals 状态池互补。
                if _followup_detected:
                    for _dom in ("定时任务", "长期任务编排"):
                        for _tb in get_tools_by_capability_domain(_dom):
                            if _tb.name not in core_names:
                                core_names.add(_tb.name)
                                core_tools.append(_tb.tool)
                    logger.debug(i18n_t("🧭 [Scaffold] C-1 已补充定时任务/编排族工具供省略跟进定位"))

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
                                    i18n_t(
                                        "🧠 [GsCoreAIAgent] 语境工具池加载 {p0} 个工具 (语境标签: {ctx_tags})",
                                        p0=len(ctx_tools),
                                        ctx_tags=ctx_tags,
                                    )
                                )
                    except Exception as e:
                        logger.debug(i18n_t("🧠 [GsCoreAIAgent] 语境工具池加载失败: {e}", e=e))

                # 第三层：查询工具池——基于 query 的向量搜索。只排除已在保底池的
                # self / buildin 分类；planning 工具不再保底，必须保留在向量检索里按需
                # 召回（"闲聊里临时要记账/建任务/查产物"靠这一层 + L4 族展开拿到）。
                if qy:
                    # L5 上下文增强检索：把最近几轮用户原话拼进检索 query，
                    # 让"改成后天吧"这类无独立语义的追问也能借上文召回到正确工具族。
                    search_query = "\n".join([*self._recent_user_texts, qy]) if self._recent_user_texts else qy
                    logger.debug(i18n_t("🧠 [GsCoreAIAgent] 尝试搜索工具: {search_query}", search_query=search_query))

                    # 只排除已在保底池的 self/buildin；plugin_dev 等"委派专用"分类由
                    # search_tools 在检索层统一拦截（NON_SEARCHABLE_TOOL_CATEGORIES），
                    # 不必也不应在这里重复声明。
                    # 召回种子数下沉为可配置（tool_search_recall，默认 4）：Reranker 精排后
                    # 召回质量更高，少而准的种子再经 L4 能力族整族展开即可覆盖需求。
                    # L0 实体路由的 route_text 只传当前消息 qy，不传 L5 拼过的 search_query——
                    # 否则"上轮问长离、这轮设提醒"会被上轮实体劫持（跨轮延续归 L3 驻留管）
                    extra_tools += await search_tools_with_entity_routing(
                        query=search_query,
                        route_text=qy,
                        limit=ai_config.get_config("tool_search_recall").data,
                        non_category=["self", "buildin"],
                    )

                # 附加池：先按能力族整族展开（L4），再去重/限量。
                # 召回族内任一工具即带出整族（剔除与保底重名/族内重复），
                # 保证"能创建就能改/删"——如召回 add_once_task 即带出
                # modify/cancel_scheduled_task，避免后续追问"改成后天"时无工具可调。
                max_extra_tools: int = ai_config.get_config("tool_extra_pool_max").data
                deduped_extra = expand_tools_to_families(
                    extra_tools,
                    exclude_names=core_names,
                    max_tools=max_extra_tools,
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
                                i18n_t(
                                    "🧠 [GsCoreAIAgent] 意图命中委派型画像 {deleg_pid}，"
                                    "注入 create_subagent 保障委派路径",
                                    deleg_pid=deleg_pid,
                                )
                            )

                # 渐进式工具暴露：非闲聊轮注入 find_tools 并标记本轮挂 RetrievableToolset，
                # 模型中途发现缺工具即可调 find_tools 现拉，下一步即可用。闲聊轮跳过。
                if ENABLE_PROGRESSIVE_TOOLS and intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS:
                    if any(t.name == "find_tools" for t in tools):
                        # find_tools 已被上游带入（显式传参 / 分类误配等）也必须挂动态
                        # toolset——否则它加载的工具无人暴露，模型调了也白调（实测踩坑）。
                        _expose_dynamic = True
                    else:
                        ft = find_tool_base("find_tools")
                        if ft is not None:
                            tools.append(ft.tool)
                            _expose_dynamic = True
                    if _expose_dynamic:
                        logger.debug(i18n_t("🧠 [GsCoreAIAgent] 已注入 find_tools，本轮启用渐进式工具暴露"))

                logger.debug(
                    i18n_t(
                        "🧠 [GsCoreAIAgent] 工具数量: {p0} (保底 {p1} + 附加 {p2})",
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
                logger.debug(i18n_t("🧠 [GsCoreAIAgent] 传入Tools列表: {p0}，已传入参数", p0=len(tools)))
        else:
            logger.debug(i18n_t("🧠 [GsCoreAIAgent] 不搜索工具"))

        logger.debug(i18n_t("🧠 [GsCoreAIAgent] 工具列表: {p0}", p0=[tool.name for tool in tools]))

        # 最终去重（兼容外部直接传入 tools 的情况）
        tools = list({obj.name: obj for obj in tools}.values())
        tool_names = [t.name for t in tools]

        # 回填本轮装配工具的能力域，供 handle_ai 偏好注入精确过滤（"装配后回传"）：
        # 把工具名映射回 capability_domain，handle_ai 据此只注入本轮可用工具相关的软偏好。
        assembled_domains: set[str] = set()
        for _tn in tool_names:
            _tb = find_tool_base(_tn)
            if _tb is not None and _tb.capability_domain:
                assembled_domains.add(_tb.capability_domain)
        self._last_assembled_domains = assembled_domains

        # 记录本次传给 AI 的工具列表
        self._session_logger.log_tools_list(tool_names)

        # 当 return_model 指定时，使用 output_type 让 pydantic_ai 强制结构化输出
        # output_type 默认为 str（返回文本），指定 Pydantic 模型时强制返回结构化 JSON
        # skills_toolset 仅挂载于 agentic + CapabilityAgent
        # （详见 _SKILLS_CREATE_BY）；后台调用不挂，避免白送 token 破坏缓存。
        _toolsets = [skills_toolset] if self.create_by in _SKILLS_CREATE_BY and not _addr_gated else []
        # 启用渐进式暴露时挂 RetrievableToolset：每个 step 读 dynamic_tool_names 即时暴露命中工具。
        # exclude_names 传静态已装配工具名，避免与 Agent(tools=...) 隐式 toolset 重名冲突。
        if _expose_dynamic:
            _toolsets = [*_toolsets, RetrievableToolset(exclude_names=set(tool_names))]
        # eval_mode 下固定 temperature=0：记忆评测的答案须可复现，采样噪声会让
        # 同一检索结果的得分跑次间 ±2-4 题波动，无法区分"改动有效"与"随机翻转"。
        from gsuid_core.ai_core.memory.config import memory_config

        if self.model:
            _model_settings = self.model.settings
            if memory_config.eval_mode and _model_settings:
                _model_settings["temperature"] = 0.0

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
            logger.info(i18n_t("🧠 [GsCoreAIAgent] 开始执行 _agent.iter()..."))
            logger.info(i18n_t("🧠 [GsCoreAIAgent] 当前 history: {p0}", p0=len(self.history)))

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
                        logger.debug(i18n_t("🧠 [GsCoreAIAgent] ⚡ 触发节点: ModelRequestNode"))

                        self._session_logger.log_node_transition("ModelRequestNode")

                        # C-4 墙钟软预算：交互式 run 超时后，请求前注入收敛提示（只注入一次），
                        # 让模型停止发起新工具轮、用已有信息作答——治多步任务的延迟长尾。
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
                                    "⏱️ [GsCoreAIAgent] 墙钟软预算已超（{p0:.0f}s），注入收敛提示",
                                    p0=_wall_elapsed,
                                )
                            )

                        for part in node.request.parts:
                            if isinstance(part, ToolReturnPart):
                                # 如果工具返回b64图片或者bytes内容, 则调用RM实例上传
                                if (
                                    isinstance(part.content, str) and part.content.startswith("base64://")
                                ) or isinstance(part.content, bytes):
                                    resource_id = RM.register(part.content)
                                    logger.info(
                                        i18n_t(
                                            "🧠 [GsCoreAIAgent] 工具 [{p0}] 返回内容，已注册资源ID [{resource_id}]",
                                            p0=part.tool_name,
                                            resource_id=resource_id,
                                        )
                                    )
                                    part.content = (
                                        f"[工具 {part.tool_name} 已生成内容, 但没有发送给用户，资源ID: {resource_id}]"
                                    )

                                # 返回的可能是对象也可能是字符串，这里为了打印转成 str
                                tool_result_str = str(part.content)
                                if len(tool_result_str) > 200:
                                    tool_result_str = tool_result_str[:200] + f"...[截断, 共{len(tool_result_str)}字符]"
                                logger.debug(
                                    i18n_t(
                                        "[✅ 工具执行完毕]: 工具名称='{p0}', 结果给到Agent={tool_result_str}",
                                        p0=part.tool_name,
                                        tool_result_str=tool_result_str,
                                    )
                                )
                                self._session_logger.log_tool_return(part.tool_name, part.content, part.tool_call_id)

                        logger.debug(i18n_t("🧠  ▶ [发起请求]: 正在等待大模型思考..."))
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
                        logger.debug(i18n_t("🧠 [GsCoreAIAgent] ⚡ 触发节点: CallToolsNode"))

                        self._session_logger.log_node_transition("CallToolsNode")

                        # 流式请求下 pydantic_ai 未必能拆出内嵌 <think> 标签（仅当标签作为
                        # 独立 SSE chunk 到达时才拆），MiniMax 等网关不保证这点，导致
                        # <think>...</think> 残留在 TextPart 里。这里原地按完整文本重新拆分，
                        # 与非流式路径对齐：既避免思考内容经显示循环 / result.output 泄漏到 C 端，
                        # 也补回意图-行为检测所需的 ThinkingPart。原地改写同一 model_response 对象，
                        # 故 history 与 result.output 一并保持干净；ToolCallPart 原样保留，工具执行不受影响。
                        node.model_response.parts = _split_embedded_thinking(node.model_response.parts, _thinking_tags)
                        # 紧接着清除文本里泄漏的工具调用标记残留（弱模型 / 兼容网关常把工具
                        # 调用以文本标签输出而非结构化 function calling），整体替换保持三处一致。
                        node.model_response.parts = _sanitize_tool_call_artifacts_in_parts(node.model_response.parts)

                        # 遍历大模型返回的具体片段 (Parts)
                        # 本轮是否已出现工具调用：用于 suppress_intermediate_text 时判断
                        # 当前响应中的文本是"中间碎碎念"还是"最终回复"。
                        _saw_tool_call_this_turn = False
                        for part in node.model_response.parts:
                            # 拦截到模型即将调用工具
                            if isinstance(part, ToolCallPart):
                                _saw_tool_call_this_turn = True
                                logger.debug(
                                    i18n_t(
                                        "[🔧 大模型请求调用工具]: 工具名称='{p0}', 参数={p1}",
                                        p0=part.tool_name,
                                        p1=part.args,
                                    )
                                )
                                _tool_call_list.append(part.tool_name)
                                self._session_logger.log_tool_call(part.tool_name, part.args, part.tool_call_id)
                                self._emit_trace("tool", f"{part.tool_name}|{part.args_as_json_str()}")

                                # 程序性记忆（默认开；关闭时零影响）：记一笔工具调用轨迹，供偏好蒸馏把
                                # 用户的"参数传错了"蒸成带具体参数的规则（设计 §4.2）。仅在
                                # enable_preference_memory 开启时写入有界 ring buffer。
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
                                # 拆出 <think> 后只剩空白的文本片段（如纯思考+工具调用轮），
                                # 既无需打印也无需下发，直接跳过，避免空的「大模型文本」噪声日志。
                                if not _text:
                                    continue
                                logger.debug(i18n_t("🧠 [大模型文本]: {_text}", _text=_text))
                                self._session_logger.log_text_output(_text)
                                if _text in SILENCE_MARKERS:
                                    logger.info(
                                        i18n_t("🧠 [GsCoreAIAgent] 检测到沉默标记 '{_text}'，跳过发送", _text=_text)
                                    )
                                elif _text in self._run_sent_texts:
                                    # 本轮已发过完全相同的段：模型跨轮重复最终答复 / 重试重发，
                                    # 跳过避免 C 端收到两段相同的话。
                                    logger.debug(
                                        i18n_t("🧠 [GsCoreAIAgent] 跳过重复文本(本轮已发): {p0}", p0=repr(_text[:40]))
                                    )
                                elif _suppress_intermediate_text and _saw_tool_call_this_turn:
                                    # 工具调用前后伴随的文本属于中间步骤碎碎念，不发送给用户，
                                    # 但仍记入 session log 供调试。
                                    logger.debug(i18n_t("🧠 [GsCoreAIAgent] 抑制中间文本: {p0}", p0=repr(_text[:40])))
                                elif bot and _text and return_mode in ["always", "by_bot"]:
                                    # 出戏预检（§D.4）：命中不发送、记入 _ooc_blocked，
                                    # iter 结束后走"提醒→重说→放行"闭环
                                    _ooc_hit = (
                                        output_firewall.check_ooc(
                                            _text,
                                            user_text=ev.raw_text if ev is not None and ev.raw_text else "",
                                        )
                                        if output_firewall.is_enabled()
                                        else None
                                    )
                                    if _ooc_hit is not None:
                                        logger.warning(
                                            i18n_t(
                                                "[OutputFirewall] 主输出命中出戏红线 {p0}: {p1}，转重说",
                                                p0=_ooc_hit.category,
                                                p1=_ooc_hit.matched,
                                            )
                                        )
                                        _ooc_blocked.append((_text, _ooc_hit))
                                        continue
                                    # 假完成预检（结构判据：完成声明 + 本轮至今零工具调用）：
                                    # 暂扣不发——后续真调了工具则属真话补发，零工具则纠正重跑
                                    _fab_gate_on = not fake_done_retry and not _tool_call_list and bool(tool_names)
                                    if _fab_gate_on and _claims_fake_done(_text):
                                        logger.warning(
                                            i18n_t(
                                                "🧠 [FakeDoneGate] 零工具完成声明，暂扣待核: {p0}", p0=repr(_text[:40])
                                            )
                                        )
                                        _fab_blocked.append(_text)
                                        continue
                                    # Why: send_chat_result 抛异常会穿透 _agent.iter() 的
                                    # async context，触发 pydantic_graph 的 athrow/cancel scope
                                    # 错误。必须在循环体内吞掉发送侧的故障。
                                    try:
                                        await send_chat_result(bot, _text, ev=ev)
                                        # 发送成功才登记去重：发送失败的段允许后续相同输出补发。
                                        self._run_sent_texts.add(_text)
                                    except Exception as _e:
                                        logger.debug(i18n_t("🧠 [GsCoreAIAgent] 文本发送失败: {_e}", _e=_e))

                            elif isinstance(part, ThinkingPart):
                                _thinking = part.content.strip()
                                logger.debug(i18n_t("🧠 [大模型思考]: {_thinking}", _thinking=_thinking))
                                if _thinking:
                                    _thinking_segments.append(_thinking)
                                self._session_logger.log_thinking(_thinking)
                                self._emit_trace("thinking", _thinking)

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
                        logger.debug(i18n_t("🧠 [GsCoreAIAgent] ⚡ 触发节点: End"))
                        logger.debug(i18n_t("  ✅ [运行结束]: 最终结果生成完毕"))
                        self._session_logger.log_node_transition("End")

            # 遍历完成后，直接从 agent_run 中获取最终结果
            result = agent_run.result
            if result:
                logger.info(i18n_t("🧠 [GsCoreAIAgent] _agent.iter() 执行成功!"))

                # 存 history 前把本轮 user turn 的 content 换成精简版（剥离 rag_context），
                # 防止【历史对话】/记忆/群语境快照逐轮累积膨胀 input 并冲淡缓存（§优化 O-1）。
                # C-4 墙钟 nudge 挂在 run 中途的后续请求上，一并从持久历史剥离。
                _new_msgs = result.new_messages()
                _relean_user_turn(_new_msgs, _lean_user_message, strip_hint_texts=(_WALL_CLOCK_NUDGE,))
                self.history.extend(_new_msgs)

                # 出戏重说闭环（§D.4）：被拦文本用警告提示重写一次，产物直接放行发送
                if _ooc_blocked and bot and return_mode in ["always", "by_bot"]:
                    await self._ooc_rewrite_and_send(_ooc_blocked, bot, ev)

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
                            logger.debug(i18n_t("🧠 [GsCoreAIAgent] 检测到意图-行为不一致，下一轮将强制提醒"))

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
                        i18n_t(
                            "📊 [GsCoreAIAgent] Token消耗: input={input_tokens},"
                            " output={output_tokens}, cache_read={cache_read_tokens},"
                            " cache_write={cache_write_tokens}",
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
                                logger.warning(i18n_t("💰 [GsCoreAIAgent] 预算记账失败: {_be}", _be=_be))
                        try:
                            self._session_logger.log_token_usage(
                                input_tokens,
                                output_tokens,
                                _model_name,
                                cache_read_tokens,
                                cache_write_tokens,
                            )
                        except Exception as _le:
                            logger.debug(i18n_t("📊 [GsCoreAIAgent] 写入 token 用量日志失败: {_le}", _le=_le))
                except AttributeError as e:
                    # result 没有 usage 属性（如 pydantic_graph End 节点返回的结果）
                    logger.info(i18n_t("📊 [GsCoreAIAgent] result.usage 访问失败: {e}", e=e))
                    pass
                except Exception as e:
                    logger.warning(i18n_t("📊 [GsCoreAIAgent] 记录统计失败: {e}", e=e))

                # 当 return_model 指定时，直接返回 Pydantic 模型实例
                if output_type is not None:
                    self._session_logger.log_run_end()
                    self._session_logger.log_result(result.output, _tool_call_list)
                    return result.output

                # 始终返回字符串类型
                result_msg = str(result.output).strip()
                # 工具调用列表只进调试日志，不追加到用户可见消息
                if _tool_call_list:
                    logger.debug(i18n_t("🔧 [本次工具调用] {p0}", p0=", ".join(_tool_call_list)))

                self._session_logger.log_run_end()
                self._session_logger.log_result(result_msg, _tool_call_list)

                # 假完成结算（结构判据收口）。两种情形：
                # ① 声明先行、动作后至（本轮终有工具调用）：暂扣文本属真话，补发；
                # ② 本轮零工具：以内部纠正消息重跑一次逼真执行/如实改口；重跑失败则补发原文兜底
                #   （不比旧行为差——旧行为就是把该声明直接发出去）。
                async def _resend_fab_blocked() -> None:
                    for _bt in _fab_blocked:
                        if _bt in self._run_sent_texts:
                            continue
                        try:
                            if bot is None:
                                logger.warning(i18n_t("🧠 [FakeDoneGate] 暂扣文本补发失败：Bot对象不可用"))
                                continue
                            await send_chat_result(bot, _bt, ev=ev)
                            self._run_sent_texts.add(_bt)
                        except Exception as _se:
                            logger.debug(i18n_t("🧠 [FakeDoneGate] 暂扣文本补发失败: {_se}", _se=_se))

                if _fab_blocked and _tool_call_list and bot and return_mode in ["always", "by_bot"]:
                    logger.info(i18n_t("🧠 [FakeDoneGate] 完成声明后续有工具调用支撑，补发暂扣文本"))
                    await _resend_fab_blocked()
                elif (
                    result_msg
                    and not _tool_call_list
                    and tool_names
                    and not fake_done_retry
                    and (_fab_blocked or _claims_fake_done(result_msg))
                ):
                    logger.warning(i18n_t("🧠 [FakeDoneGate] 零工具调用却声称已完成动作，追加纠正重跑"))
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
                        logger.warning(i18n_t("🧠 [FakeDoneGate] 纠正重跑失败，沿用原结果: {_fe}", _fe=_fe))
                        corrected = None
                        if _fab_blocked and bot and return_mode in ["always", "by_bot"]:
                            await _resend_fab_blocked()
                    if isinstance(corrected, str) and corrected.strip():
                        # 纠正成功：从持久历史剥掉 nudge user turn 与暂扣未发的编造声明
                        # （用户从没见过它们，留着只会被后续轮模仿）。重跑失败走上面的
                        # 补发兜底时不清理——那时原文真的发出去了，历史须与所见一致。
                        _fabricated = {t.strip() for t in _fab_blocked}
                        if _claims_fake_done(result_msg):
                            _fabricated.add(result_msg.strip())
                        result_msg = corrected.strip()
                        self._scrub_fake_done_history(_fabricated)

                if return_mode in ["by_bot"] and bot and ev:
                    return ""
                # 出戏兜底（§D.4）：run() 的返回值供**无 bot 发送通道**的消费方使用
                # （chat_with_history API、任何直接读取 output 的调用方）——send 路径的出戏
                # 重说闭环（_ooc_rewrite_and_send）只作用于 bot 发送，不覆盖返回值。这里对
                # 返回值做末端兜底 scrub：命中模型名/AI身份/系统术语即整体替换为角色化兜底，
                # 保证任何消费方拿到的 output 都不泄露出戏内容。roleplay tier；plain 节点自动放行。
                if result_msg and output_firewall.is_enabled():
                    result_msg, _ooc_scrubbed = output_firewall.scrub_or_fallback(
                        result_msg, user_text=ev.raw_text if ev is not None and ev.raw_text else ""
                    )
                    if _ooc_scrubbed:
                        logger.warning(i18n_t("[OutputFirewall] run() 返回值命中出戏红线，已兜底替换为角色化文本"))
                return result_msg

            # result 为空时的默认返回值
            return "Agent 执行完成，但未返回有效结果"

        except UsageLimitExceeded:
            # 达到限制后的处理逻辑
            logger.warning(i18n_t("🧠 [PydanticAI] Agent 达到最高思考轮数限制 {p0}", p0=limits.request_limit))
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
                await bot.send(await bot.t("⏳ 思考链过长，正在根据已有线索为你整理最终结论..."))

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
                logger.error(i18n_t("🧠 [PydanticAI] 强制总结失败: {e}", e=e))
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
            # 清理本轮的单轮节流计数（scheduler.py add_once_task 等共享），
            # 防止内存中 key 无限累积。session_id 缺失时跳过——本轮也没机会
            # 写入计数。
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
                logger.debug(i18n_t("🧠 [GsCoreAIAgent] 清理单轮节流计数失败: {_e}", _e=_e))

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
            budget_gate: 本次 run 是否为预算入口。True（巡检 / proactive / 定时等自主调用）
                时超额直接早退、绝不花费 Token；交互被动路径已在 handle_ai 提前闸门，按默认
                False 只记账不二次拦截。无论是否拦截，可归属 scope 的 Token 都会记账。
            suppress_intermediate_text: True 时，本轮中**只要出现过 ToolCallPart**，其前后伴随的
                文本片段都不会发送给用户，仅保留没有任何工具调用的最终文本回复。
                用于画布 Agent 等多工具编排场景，避免中间步骤的碎碎念刷屏。

        Returns:
            Agent 执行结果。默认返回 str，当 output_type 指定时返回对应模型实例
        """
        async with self._run_lock:
            logger.info(i18n_t("🧠 [GsCoreAIAgent] 获取到执行锁，开始执行..."))
            # O-A 群聊队头阻塞防护：拿到锁时若已排队过久（话题大概率翻篇），丢弃过期回复。
            if (
                enqueue_ts is not None
                and self.create_by == "Chat"
                and (time.time() - enqueue_ts) > STALE_CHAT_REQUEST_TTL
            ):
                waited = time.time() - enqueue_ts
                logger.info(
                    i18n_t("🧠 [GsCoreAIAgent] 队列等待 {waited:.1f}s 超 TTL，丢弃过期请求，释放锁", waited=waited)
                )
                return "" if output_type is None else None
            # 模型热切换：网页控制台切换高/低级任务模型后，存活会话在此即时热替换到新模型，
            # 无需 coreclear 重置会话。覆盖所有 run 入口（交互/巡检/定时/主动发言）。
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
                )

            # 显式绑定固定模型的会话（model_config_name 为 None）不参与 provider 路由
            if self.model_config_name is None:
                result = await _do_run()
                logger.info(i18n_t("🧠 [GsCoreAIAgent] 执行完成，释放锁"))
                return result

            # provider 路由：主配置并发满/冷却时切到备用(2nd)配置；请求命中
            # provider 级故障（限流/连接）时给该配置冷却期并换路重试一次。
            for _attempt in range(2):
                async with provider_router.slot(self.task_level) as routed_name:
                    temp_model = None
                    orig_model = self.model
                    if routed_name and routed_name != self.model_config_name:
                        try:
                            temp_model = get_model_by_full_name(routed_name)
                            self.model = temp_model
                        except Exception as e:
                            logger.warning(
                                i18n_t(
                                    "🧠 [GsCoreAIAgent] 备用配置 {routed_name} 加载失败，沿用主配置: {e}",
                                    routed_name=routed_name,
                                    e=e,
                                )
                            )
                            routed_name = self.model_config_name
                    try:
                        result = await _do_run()
                        provider_router.mark_success(routed_name or self.model_config_name)
                        logger.info(i18n_t("🧠 [GsCoreAIAgent] 执行完成，释放锁"))
                        return result
                    except Exception as e:
                        if _attempt == 0 and looks_like_provider_failure(str(e)):
                            provider_router.mark_failure(routed_name or self.model_config_name)
                            logger.warning(i18n_t("🧠 [GsCoreAIAgent] provider 级故障，换路重试: {e}", e=e))
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
