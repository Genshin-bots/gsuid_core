"""
PydanticAI Agent 核心模块
基于 pydantic_ai 实现的轻量级 Agent
"""

import re
import time
import uuid
import base64
import asyncio
from typing import Any, List, Tuple, Union, Literal, TypeVar, Callable, Optional, Sequence, overload

import httpx
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits
from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    VideoUrl,
    UserContent,
    ModelMessage,
    ModelRequest,
    UploadedFile,
    BinaryContent,
    ModelResponse,
    UserPromptPart,
)
from pydantic_ai.exceptions import ModelHTTPError

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core import output_gate, output_firewall, angle_bracket_guard
from gsuid_core.ai_core.const import (
    STALE_CHAT_REQUEST_TTL,
)
from gsuid_core.ai_core.utils import (
    ERROR_TIMEOUT_TEXT,
    ERROR_RESULT_PREFIX,
    ERROR_CONTENT_REJECTED,
    send_chat_result,
    fetch_video_bytes,
    is_silence_marker,
    _is_content_rejected,
    materialize_image_url,
    compact_session_history,
    _is_retryable_client_error,
    _is_non_retryable_model_error,
    _strip_remote_images_from_history,
)
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.rag.tools import (
    ToolList,
)
from gsuid_core.ai_core.prefix_probe import PrefixSnapshot
from gsuid_core.ai_core.configs.models import (
    AnyModel,
    get_model_for_task,
    get_model_by_full_name,
    get_config_name_for_task,
    get_2nd_config_name_for_task,
    get_model_fingerprint_for_task,
)
from gsuid_core.ai_core.session_logger import AISessionLogger, ProactiveSource
from gsuid_core.ai_core.persona.prompts import CHARACTER_BUILDING_TEMPLATE
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.interaction_scaffold import CheapGate, TurnGraph
from gsuid_core.ai_core.configs.provider_router import (
    provider_router,
    looks_like_provider_failure,
)
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    POST_TOOL_FAIL_CONTRACT as _POST_TOOL_FAIL_CONTRACT,
    POST_TOOL_OUTPUT_CONTRACT as _POST_TOOL_OUTPUT_CONTRACT,
    POST_TOOL_FAIL_CONTRACT_RENDER as _POST_TOOL_FAIL_CONTRACT_RENDER,
    POST_TOOL_OUTPUT_CONTRACT_RENDER as _POST_TOOL_OUTPUT_CONTRACT_RENDER,
    POST_TOOL_FAIL_CONTRACT_CAPABILITY as _POST_TOOL_FAIL_CONTRACT_CAPABILITY,
    POST_TOOL_OUTPUT_CONTRACT_CAPABILITY as _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
    post_tool_contracts_for as _post_tool_contracts_for,
)

# 兼容 re-export：单测 / 外部仍从 gs_agent 取 POST_TOOL 契约常量
_ = (
    _POST_TOOL_FAIL_CONTRACT,
    _POST_TOOL_FAIL_CONTRACT_CAPABILITY,
    _POST_TOOL_FAIL_CONTRACT_RENDER,
    _POST_TOOL_OUTPUT_CONTRACT,
    _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
    _POST_TOOL_OUTPUT_CONTRACT_RENDER,
    _post_tool_contracts_for,
)

_T = TypeVar("_T")

# 超过 max_history 时裁到 max_history * 该比例；越低则单次腾出越多、compact 越稀。
# compact 走 keep_prefix（保头裁中段），绝不砍头部字节——前缀缓存的前提。
_HISTORY_TRIM_RATIO = 0.6

# 预算 scope + run-once 共享符号（实现在 agent_run；此处 re-export 保持 import 稳定）
from gsuid_core.ai_core.agent_run.support import (  # noqa: E402
    _FAKE_DONE_RE,
    _FAKE_DONE_NUDGE,
    _WALL_CLOCK_NUDGE,
    _RENDER_TOOL_NAMES,
    _THRASH_FUSE_NUDGE,
    _FAKE_DONE_CASUAL_RE,
    _SEARCHISH_TOOL_HINTS,
    _FAKE_DONE_QUESTION_RE,
    _INTERACTIVE_CREATE_BY,
    _RENDER_DELEGATE_NUDGE,
    _FAKE_DONE_TASK_NOUN_RE,
    _MAIN_PERSONA_CREATE_BY,
    _THRASH_SAME_TOOL_LIMIT,
    _FAKE_DONE_THIRD_SUBJ_RE,
    _FIND_TOOLS_THRASH_LIMIT,
    _STRUCTURAL_ZERO_TOOL_NUDGE,
    TraceKind,
    _append_user_text,
    _claims_fake_done,
    _correction_nudge_markers,
    _format_capability_roster,
    _tool_return_looks_failed,
    usage_limit_return_payload,
    _tool_return_is_async_pending,
    _pool_overlaps_capability_agent,
    _tool_call_targets_render_agent,
    _tool_return_is_effectual_write,
    _capability_exclusive_tool_names,
    _matched_delegation_only_profile,
    _update_thrash_streak_for_response,
)
from gsuid_core.ai_core.agent_run.budget_ctx import (  # noqa: E402
    _current_budget_scope,
    budget_scope_from_event as _budget_scope_from_event,
    set_budget_scope_context,
    reset_budget_scope_context,
)

# 阻止 ruff F401 删掉仅被测试 import 的 re-export
_ = (
    TraceKind,
    _FAKE_DONE_CASUAL_RE,
    _FAKE_DONE_NUDGE,
    _FAKE_DONE_QUESTION_RE,
    _FAKE_DONE_RE,
    _FAKE_DONE_TASK_NOUN_RE,
    _FAKE_DONE_THIRD_SUBJ_RE,
    _FIND_TOOLS_THRASH_LIMIT,
    _INTERACTIVE_CREATE_BY,
    _MAIN_PERSONA_CREATE_BY,
    _RENDER_DELEGATE_NUDGE,
    _RENDER_TOOL_NAMES,
    _SEARCHISH_TOOL_HINTS,
    _STRUCTURAL_ZERO_TOOL_NUDGE,
    _THRASH_FUSE_NUDGE,
    _THRASH_SAME_TOOL_LIMIT,
    _WALL_CLOCK_NUDGE,
    _append_user_text,
    _capability_exclusive_tool_names,
    _claims_fake_done,
    _correction_nudge_markers,
    _format_capability_roster,
    _matched_delegation_only_profile,
    _pool_overlaps_capability_agent,
    _tool_call_targets_render_agent,
    _tool_return_is_async_pending,
    _tool_return_is_effectual_write,
    usage_limit_return_payload,
    _tool_return_looks_failed,
    _update_thrash_streak_for_response,
    reset_budget_scope_context,
    set_budget_scope_context,
    _current_budget_scope,
    _budget_scope_from_event,
)

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


from gsuid_core.ai_core.agent_run import RunOnceMixin  # noqa: E402


class GsCoreAIAgent(RunOnceMixin):
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
        capability_node_id: Optional[str] = None,
    ):
        # max_tokens / max_history 未显式传入时落到全局配置（主对话等走默认的路径据此可调）
        _max_history: int = max_history if max_history is not None else ai_config.get_config("agent_max_history").data
        _max_tokens: int = max_tokens if max_tokens is not None else ai_config.get_config("agent_max_tokens").data
        self.history: List[ModelMessage] = []
        self._prefix_snapshot: Optional["PrefixSnapshot"] = None
        self._session_toolset_frozen: Optional[List[str]] = None
        self._session_toolset_tags: Optional[frozenset[str]] = None
        self._session_appended_tools: List[str] = []
        self.max_history = _max_history
        self.system_prompt = system_prompt
        # 稳定前缀构建时刻：ai_router 按 TTL 原地刷新 system_prompt（O-3 慢变上下文防僵化）
        self.system_prompt_built_at: float = time.time()
        self.persona_name = persona_name  # 用于热重载检查
        # 用于串行执行 run 方法的锁
        self._run_lock = asyncio.Lock()
        # A: 同 Session 新消息抢答时 set，当前 generation 在节点间隙 abort
        self._cancel_generation = asyncio.Event()
        # 当前锁内是否在跑框架回灌：与真人消息互不 supersede，只排队
        self._running_framework: bool = False
        # iter 进行中禁止 compact 换掉 self.history（与 pydantic-ai 共用同一 list）
        self._history_iter_active: bool = False
        # 4.7 supersede 交接语已删：在途根任务由 build_task_context 每轮从库注入。
        self.max_tokens = _max_tokens
        self.max_iterations = max_iterations  # 自定义迭代次数限制，None时使用配置默认值
        # C-4 墙钟软预算(秒)覆写：None=沿用全局 scaffold_wall_clock_budget；<=0=本 Agent 关闭软预算。
        # 长流程入口（编排 / 多轮工具 + 等人确认）必须放宽，否则永远跑不到终态。
        self.wall_clock_budget = wall_clock_budget
        # 轨迹观察者：让宿主前端看见模型推理与工具调用，不必去翻 session log。
        # 契约见 _emit_trace。
        self.on_trace = on_trace
        self.task_level: Literal["high", "low"] = task_level  # 任务级别，用于选择对应的模型配置

        self.create_by = create_by
        # 能力代理 node_id（仅 CapabilityAgent）；契约/日志用，勿靠 session 子串猜
        self.capability_node_id: str = (capability_node_id or "").strip()
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
        # 本用户轮内模型对框架校验的申辩理由（dispute_directive 写，settle 读）。
        # 与 _run_sent_texts 同样同引用透传进纠正轮，使外层能看到内层的申辩。
        self._run_disputes: List[str] = []
        # 最近一次 attempt 内已执行的工具名（与 _execute_run_once 的局部列表同引用）：
        self._last_attempt_tool_calls: List[str] = []
        # 纠正轮是新 RunOnceState；cleanup 把结构事实写到宿主，外层 settle 再并回父 st。
        self._last_attempt_delegated_render: bool = False
        self._last_attempt_image_sent: bool = False
        self._last_attempt_pending_async: bool = False
        self._last_attempt_has_status_tool: bool = False
        self._last_attempt_thinking: str = ""
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
        # 本次 run 实际路由到的配置全名：故障切换时会临时指向备用配置，与 model 同步；
        # 显式传 model 的会话恒为 None（无配置文件可读）。
        self._active_config_name: Optional[str] = self.model_config_name

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

    @property
    def last_run_visible_texts(self) -> tuple[str, ...]:
        """本轮已出站的可见台词（插入序）。评测 HTTP 在 SILENCE 返回时拼回这条。"""
        return tuple(self._run_sent_texts)

    def _emit_trace(self, kind: TraceKind, text: str) -> None:
        """把模型思考 / 工具调用轨迹推给观察者（``on_trace``）。

        ``kind="tool"`` 的 text 形如 ``"<工具名>|<参数JSON>"``。

        宿主可据此把"Agent 在想什么、调了什么工具"实时呈现给用户
        （例如前端「思考过程」折叠块），而不必去翻 session log 文件。

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
        3. 非 iter 中才 compact；iter 内只追加，避免与 pydantic-ai 分叉。

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
        """裁剪 message_history：超水位时**保头裁中段**，绝不改写 system_prompt。

        前缀缓存红线：
        - system_prompt 会话内只建一次、只追加契约到 user 侧（见 loop UserPromptPart）；
        - history 头部字节跨 compact 不变（``compact_session_history`` / keep_prefix）；
        - 禁止把角色锚点等消息插回头部（会整体平移前缀）。
        iter 中途换 list 会让下一跳 ModelRequest 与缓存分叉，禁止。
        """
        if self._history_iter_active:
            return
        before: int = len(self.history)
        self.history, did_truncate = compact_session_history(
            self.history,
            self.max_history,
            trim_ratio=_HISTORY_TRIM_RATIO,
        )
        after: int = len(self.history)
        # 仅「因超长主动裁剪且确有条目被丢弃」才打 auto_compact（供 webconsole 画独立色块）
        if did_truncate and after < before:
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
            from gsuid_core.ai_core.persona.appearance import (
                bytes_from_image_ref,
                format_look_identity_note,
            )

            result: list[UserContent] = []
            notes: list[str] = []
            seen_notes: set[str] = set()
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
                    if self.persona_name:
                        note = format_look_identity_note(self.persona_name, bytes_from_image_ref(url))
                        if note and note not in seen_notes:
                            seen_notes.add(note)
                            notes.append(note)
                else:
                    result.append(item)
            if notes:
                joined = "\n".join(notes)
                if result and isinstance(result[0], str):
                    result[0] = f"{result[0]}\n{joined}"
                else:
                    result.insert(0, joined)
            return result

        # 模型不支持图片，调用图片理解模块转述
        if image_urls:
            logger.info(i18n_t("log.agent.imgund_images_image_paraphrasing", p0=len(image_urls)))
            # 用户问题：用于把冗长的图片描述按需精简到与问题相关的部分
            user_question = "\n".join(text_parts).strip()
            from gsuid_core.ai_core.persona.appearance import (
                bytes_from_image_ref,
                format_look_identity_note,
            )

            descriptions: list[str] = []
            for idx, url in enumerate(image_urls):
                try:
                    description = await understand_image(
                        image_url=url,
                        parent_session_id=self.session_id,
                        persona_name=self.persona_name,
                    )
                    description = await self._summarize_image_description(description, user_question)
                    line = f"图片{idx + 1}: {description}"
                    if self.persona_name:
                        note = format_look_identity_note(self.persona_name, bytes_from_image_ref(url))
                        if note:
                            line = f"{line}\n{note}"
                    descriptions.append(line)
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
        turn_graph: Optional[TurnGraph] = None,
        cheap_gate: Optional[CheapGate] = None,
        is_framework_injection: bool = False,
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
        *,
        output_type: type[_T],
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[TurnGraph] = None,
        cheap_gate: Optional[CheapGate] = None,
        is_framework_injection: bool = False,
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
        turn_graph: Optional[TurnGraph] = None,
        cheap_gate: Optional[CheapGate] = None,
        is_framework_injection: bool = False,
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
        self._run_disputes = []

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
                    is_framework_injection=is_framework_injection,
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
        if not out or is_silence_marker(out):
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
        _ooc_fb = output_firewall.fallback_ooc_text(self.persona_name)
        if not rewritten:
            rewritten = _ooc_fb
        if first_hit.category in output_firewall.NEVER_RELEASE_CATEGORIES:
            _user_text = ev.raw_text if ev is not None and ev.raw_text else ""
            _recheck = output_firewall.check_ooc(rewritten, user_text=_user_text)
            if _recheck is not None and _recheck.category in output_firewall.NEVER_RELEASE_CATEGORIES:
                logger.warning(i18n_t("log.agent.firewall_rewrite_output_hit_non"))
                rewritten = _ooc_fb
        if angle_bracket_guard.has_illegal_angle_tags(rewritten):
            rewritten = angle_bracket_guard.sanitize_illegal_angle_tags(rewritten) or _ooc_fb
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
            return output_firewall.fallback_machine_text(self.persona_name)
        # never-release 与其它 OOC：收尾单次路径用角色兜底，避免 ooc_check=False 漏放
        return output_firewall.fallback_ooc_text(self.persona_name)

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
        clean_sent = [
            t
            for t in self._run_sent_texts
            if t and not is_silence_marker(t) and not angle_bracket_guard.has_illegal_angle_tags(t)
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
        """假完成收尾：删纠正 nudge 与未发出的编造声明（与闸门 scrub 共用编辑器）。

        纠正 nudge 一律剥掉**全部**系统校验 user turn（不止假完成那条）：
        它们是框架内部指令，留在 history 会污染上下文并破坏前缀缓存（方案四/五）。
        """
        self._edit_history_tail(
            tail_n=8,
            drop_user_markers=_correction_nudge_markers(),
            drop_text_parts=fabricated_texts,
        )

    # ──────────────────────────────────────────────────────────────

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
        turn_graph: Optional[TurnGraph] = None,
        cheap_gate: Optional[CheapGate] = None,
        is_framework_injection: bool = False,
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
        *,
        output_type: type[_T],
        enqueue_ts: Optional[float] = None,
        intent: Optional[str] = None,
        has_active_task: bool = False,
        budget_gate: bool = False,
        suppress_intermediate_text: bool = False,
        turn_graph: Optional[TurnGraph] = None,
        cheap_gate: Optional[CheapGate] = None,
        is_framework_injection: bool = False,
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
        turn_graph: Optional[TurnGraph] = None,
        cheap_gate: Optional[CheapGate] = None,
        is_framework_injection: bool = False,
    ) -> object:
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
            suppress_intermediate_text: True 时，本轮中**只要出现过函数 ToolCallPart**，其前后
                伴随的规划/内心 OS 默认不发送。例外：主人格尚未出站过的一句接任务应仍发送
                一次（不按 12 字；结构垃圾仍压）。无工具的最终回复照常发送。
            turn_graph: 入口构建的 TurnGraph（可选）；缺省时在装配层现场构建。
            cheap_gate: CheapGate 成本档（可选）；驱动 light 零工具 / 群聊瘦保底。

        Returns:
            Agent 执行结果。默认返回 str，当 output_type 指定时返回对应模型实例
        """
        # A: 同 Session 抢答——仅「真人 vs 真人」才 cancel；
        # 框架回灌与真人互不 supersede（排队等锁），避免交付被闲聊顶掉 / 回灌打断用户。
        if self.create_by in _INTERACTIVE_CREATE_BY and self._run_lock.locked():
            if is_framework_injection or self._running_framework:
                logger.info(i18n_t("log.agent.supersede_skip_framework_queue"))
            else:
                self._cancel_generation.set()
                logger.info(i18n_t("log.agent.supersede_cancel_current"))

        async with self._run_lock:
            logger.info(i18n_t("log.agent.acquired_lock"))
            # 本 generation 独立 cancel 事件；上轮 set 过的不得污染本轮
            self._cancel_generation = asyncio.Event()
            self._running_framework = bool(is_framework_injection)
            try:
                return await self._run_under_lock(
                    user_message=user_message,
                    bot=bot,
                    ev=ev,
                    rag_context=rag_context,
                    tools=tools,
                    return_mode=return_mode,
                    output_type=output_type,
                    enqueue_ts=enqueue_ts,
                    intent=intent,
                    has_active_task=has_active_task,
                    budget_gate=budget_gate,
                    suppress_intermediate_text=suppress_intermediate_text,
                    turn_graph=turn_graph,
                    cheap_gate=cheap_gate,
                    is_framework_injection=is_framework_injection,
                )
            finally:
                self._running_framework = False

    async def _run_under_lock(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot],
        ev: Optional[Event],
        rag_context: Optional[str],
        tools: Optional[ToolList],
        return_mode: Literal["always", "return", "by_bot"],
        output_type: Optional[type],
        enqueue_ts: Optional[float],
        intent: Optional[str],
        has_active_task: bool,
        budget_gate: bool,
        suppress_intermediate_text: bool,
        turn_graph: Optional[TurnGraph],
        cheap_gate: Optional[CheapGate],
        is_framework_injection: bool,
    ) -> Union[str, Any]:
        """已持锁：TTL 校验 + provider 路由 + 真正执行。"""
        # O-A：队头阻塞过久丢弃（框架回灌不受 TTL）
        if (
            not is_framework_injection
            and enqueue_ts is not None
            and self.create_by == "Chat"
            and (time.time() - enqueue_ts) > STALE_CHAT_REQUEST_TTL
        ):
            waited = time.time() - enqueue_ts
            logger.info(i18n_t("log.agent.queue_wait_waited_exceeded", waited=waited))
            return "" if output_type is None else None
        await self.refresh_model_if_changed()

        async def _do_run() -> Union[str, Any]:
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
                is_framework_injection=is_framework_injection,
            )

        if self.model_config_name is None:
            result = await _do_run()
            logger.info(i18n_t("log.agent.lock_ok"))
            return result

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
                orig_active_cfg = self._active_config_name
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
                self._active_config_name = routed_name or self.model_config_name
                try:
                    result = await _do_run()
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
                        self.model = orig_model
                    self._active_config_name = orig_active_cfg
        return "" if output_type is None else None


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
    capability_node_id: Optional[str] = None,
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
            （如前端「思考过程」折叠块）。旁路钩子，异常会被吞掉，不影响 run

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
        capability_node_id=capability_node_id,
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
