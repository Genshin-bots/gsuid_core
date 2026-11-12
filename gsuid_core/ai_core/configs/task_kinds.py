"""AI 任务类型注册表（叶子模块，不依赖配置系统）。

每个发起 LLM 调用的来源声明一个 ``task_kind``。默认走调用方给的 high/low 档位；
用户可在网页控制台「任务配置 → 高级：按任务覆盖模型」里为单个任务改档或指定配置文件。
覆盖值的解析在 ``models.resolve_task_route``，本模块只登记元数据。
"""

from __future__ import annotations

from typing import Final, Literal
from dataclasses import dataclass

TaskLevel = Literal["high", "low"]
TaskGroup = Literal["conversation", "agent", "proactive", "memory", "multimodal", "plugin"]

TASK_GROUP_ORDER: Final[tuple[TaskGroup, ...]] = (
    "conversation",
    "agent",
    "proactive",
    "memory",
    "multimodal",
    "plugin",
)


@dataclass(frozen=True)
class AITaskKind:
    id: str
    title: str
    desc: str
    group: TaskGroup
    default_level: TaskLevel
    # True=调用方传入什么档位就用什么档位（如图片理解跟随主对话），default_level 仅作展示
    follows_caller: bool = False
    # True=走无思考快模型通道（get_fast_model_*）
    fast: bool = False
    # 该任务在 token 统计（by_type）里的记账键，值为 lower(create_by / stats_chat_type)；
    # 统计页据此把用量 join 回任务，多个记账键可列多个。空表示不参与用量聚合
    stats_keys: tuple[str, ...] = ()


_REGISTRY: dict[str, AITaskKind] = {}


def register_ai_task_kind(kind: AITaskKind) -> AITaskKind:
    """登记任务类型；同 id 重复登记以后者为准（插件热重载时会再次登记）。"""
    _REGISTRY[kind.id] = kind
    return kind


def get_ai_task_kind(kind_id: str) -> AITaskKind | None:
    return _REGISTRY[kind_id] if kind_id in _REGISTRY else None


def list_ai_task_kinds() -> list[AITaskKind]:
    order = {g: i for i, g in enumerate(TASK_GROUP_ORDER)}
    return sorted(_REGISTRY.values(), key=lambda k: order[k.group])


def _builtin(
    kind_id: str,
    title: str,
    desc: str,
    group: TaskGroup,
    level: TaskLevel,
    *,
    follows_caller: bool = False,
    fast: bool = False,
    stats: tuple[str, ...] = (),
) -> str:
    register_ai_task_kind(AITaskKind(kind_id, title, desc, group, level, follows_caller, fast, stats))
    return kind_id


# ── 对话 ──
TASK_CHAT: Final = _builtin(
    "chat",
    "主对话",
    "群聊 / 私聊里与人格的正常对话，含工具调用。",
    "conversation",
    "high",
    stats=("chat", "http_chat", "命令补充"),
)
TASK_TURN_ROUTER: Final = _builtin(
    "turn_router",
    "前置路由",
    "每轮回复前判断消息意图与是否需要工具，调用频繁，适合快模型。",
    "conversation",
    "low",
    fast=True,
    stats=("turnrouter",),
)
TASK_TEMP_ASSISTANT: Final = _builtin(
    "temp_assistant",
    "临时任务助手",
    "/btw 等无人格、无历史的一次性问答。",
    "conversation",
    "high",
    stats=("coreaicontrolbtw",),
)
TASK_EVAL_JUDGE: Final = _builtin(
    "eval_judge",
    "评测裁判",
    "网页控制台 / HTTP 评测里给回复打分的裁判模型。",
    "conversation",
    "low",
    stats=("evaljudge",),
)

# ── 智能体与编排 ──
TASK_SUBAGENT: Final = _builtin(
    "subagent", "子 Agent", "主 Agent 派生的子任务（含出图 render_agent）。", "agent", "high", stats=("autoplanner",)
)
TASK_CAPABILITY_AGENT: Final = _builtin(
    "capability_agent", "能力代理", "按能力画像执行的长任务 Agent。", "agent", "high", stats=("capabilityagent",)
)
TASK_CAPABILITY_EVAL: Final = _builtin(
    "capability_eval", "能力代理评估", "评估能力代理的产出是否达标。", "agent", "low", stats=("capabilityevaluator",)
)
TASK_KANBAN_RELAY: Final = _builtin(
    "kanban_relay", "看板转述", "把长任务结果转述成人格口吻发出。", "agent", "low", stats=("kanban_relay",)
)
TASK_SCHEDULED: Final = _builtin(
    "scheduled_task", "定时任务", "到点执行用户预约的任务。", "agent", "high", stats=("scheduledtask_exec",)
)
TASK_AUTONOMY: Final = _builtin(
    "autonomy", "自主行动", "人格在无人触发时自主规划并执行的行动。", "agent", "high", stats=("autonomy",)
)
TASK_BUILD_PERSONA: Final = _builtin(
    "build_persona", "人格生成", "根据描述生成新的人格提示词。", "agent", "high", stats=("buildpersona",)
)
TASK_MCP_PROFILE: Final = _builtin(
    "mcp_profile", "MCP 画像生成", "为接入的 MCP 服务生成能力描述。", "agent", "high", stats=("mcpprofilegenerator",)
)

# ── 主动发言 ──
TASK_HEARTBEAT_DECISION: Final = _builtin(
    "heartbeat_decision",
    "巡检决策",
    "定时巡检时判断要不要主动发言。",
    "proactive",
    "high",
    stats=("heartbeat_decision",),
)
TASK_HEARTBEAT_OUTPUT: Final = _builtin(
    "heartbeat_output", "巡检发言", "巡检决定发言后生成实际内容。", "proactive", "high", stats=("heartbeat_output",)
)
TASK_REACTIVE_GATE: Final = _builtin(
    "reactive_gate", "免唤醒续聊判定", "未被 @ 时判断是否接话。", "proactive", "high", stats=("reactive_gate",)
)

# ── 记忆与关系 ──
TASK_MEMORY_ALGEBRA: Final = _builtin(
    "memory_algebra",
    "记忆抽取",
    "从对话窗口中抽取事实、实体与偏好。",
    "memory",
    "low",
    fast=True,
    stats=("memalgebra",),
)
TASK_MEMORY_WINDOW_ROUTER: Final = _builtin(
    "memory_window_router",
    "记忆话题归类",
    "把对话片段归入已有话题线程。",
    "memory",
    "low",
    fast=True,
    stats=("memwindowrouter",),
)
TASK_JOURNAL_COMPACT: Final = _builtin(
    "journal_compact", "日志压缩", "把人格日志压缩成摘要。", "memory", "low", stats=("journal_compact",)
)
TASK_RELATIONSHIP_DISTILL: Final = _builtin(
    "relationship_distill",
    "关系提炼",
    "提炼人格与单个用户之间的相处约定。",
    "memory",
    "high",
    stats=("dyadic_distill",),
)

# ── 多模态 ──
TASK_IMAGE_UNDERSTAND: Final = _builtin(
    "image_understand",
    "图片理解",
    "把图片转述成文字。固定跟随调用方的档位，不可修改。",
    "multimodal",
    "high",
    follows_caller=True,
    stats=("imageunderstand",),
)
TASK_IMAGE_DESC_SUMMARY: Final = _builtin(
    "image_desc_summary",
    "图片描述精简",
    "把过长的图片描述压缩成与问题相关的摘要。",
    "multimodal",
    "low",
    stats=("imagedescsummary",),
)
TASK_MEME_TAGGER: Final = _builtin(
    "meme_tagger", "表情包打标", "为收集到的表情包生成标签与描述。", "multimodal", "low", stats=("memetagger",)
)
