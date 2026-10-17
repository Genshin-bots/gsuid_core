"""系统提示词组的字段表。正文默认值来自现有常量，启动时写进默认组。"""

from __future__ import annotations

from dataclasses import dataclass

from gsuid_core.ai_core.persona.prompts import (
    ROLE_PLAYING_START,
    SYSTEM_CONSTRAINTS,
    TOOL_ORCHESTRATION_CONSTRAINTS,
)
from gsuid_core.ai_core.heartbeat.decision import (
    DECISION_USER_TEMPLATE,
    REACTIVE_GATE_TEMPLATE,
    PROACTIVE_MESSAGE_USER_TEMPLATE,
)
from gsuid_core.ai_core.persona.chat_style import ANSWER_CONTRACT
from gsuid_core.ai_core.prompt_group.render import format_template_to_mustache
from gsuid_core.ai_core.scheduled_task.executor import SCHEDULED_TASK_EXECUTOR_PROMPT

DEFAULT_GROUP_NAME = "默认"
BUILTIN_PERSONA_NAME = "早柚"

_SPEECH_BLOCK = "台词长度：建议不超过 {{soft}} 字（用户明确要求详细时除外）。\n发言形态：{{segment_hint}}"
_APPEARANCE_LINE = "我的样子：{{appearance}}\n图中角色与此一致才是你；不要人称混乱。"
_DATE_LINE = "当前日期：{{date}}"
_CAPABILITY_HEADING = "## 可委派能力代理"
_ROSTER_WRAP = (
    "（可用能力代理——须 "
    '`create_subagent(agent_profile="<node_id>", task=...)` 委派，'
    "agent_profile 只填下列 node_id，禁止自造名字：\n"
    "{{lines}}\n"
    "提醒的增删改查在主会话；禁止声称没有对应工具。）"
)
_CLOSING = "你首先是「{{char_name}}」：口吻是角色；该查就调工具；{{render_clause}}未点名优先 <SILENCE>。"
RENDER_CLAUSE_WHEN_ALLOWED = "多项数据出图；"


@dataclass(frozen=True)
class PromptSpec:
    key: str
    section: str
    title: str
    description: str
    placeholders: tuple[str, ...]


# 入戏、外貌包装、日期行、能力代理标题和花名册包装是程序固定句，不进编辑页。
SPECS: tuple[PromptSpec, ...] = (
    PromptSpec(
        "system_constraints",
        "会话·系统提示",
        "基本规矩",
        "主人 ID 列表与口头称呼在发送前填入。",
        ("masters", "master_title"),
    ),
    PromptSpec(
        "tool_orchestration",
        "会话·系统提示",
        "工具决策",
        "主会话 system 里的工具规程。",
        (),
    ),
    PromptSpec(
        "closing",
        "会话·系统提示",
        "收束句",
        "人格允许出图时 {{render_clause}} 为「多项数据出图；」，否则为空。{{char_name}} 是人格名。"
        "发送时这一段和上一段之间空一行。行首单独的 --- 不用写，保存时会去掉。",
        ("char_name", "render_clause"),
    ),
    PromptSpec(
        "speech_block",
        "会话·系统提示",
        "台词长度",
        "字数和分段句由人格的说话强度填入。",
        ("soft", "segment_hint"),
    ),
    PromptSpec(
        "answer_contract",
        "会话·系统提示",
        "作答契约",
        "发送时排在台词长度后面。承认不知道时不要接着解释这份不知道从哪来。",
        (),
    ),
    PromptSpec(
        "soft_trigger",
        "会话·每轮注入",
        "续聊软触发",
        "免唤醒续聊、且这条没有 @ 时，加在本轮 user 末尾。",
        (),
    ),
    PromptSpec(
        "identity",
        "会话·每轮注入",
        "身份锚",
        "只在私聊注入。群聊身份留在会话 system，避免双写。",
        ("char_name",),
    ),
    PromptSpec(
        "transaction_priority",
        "会话·每轮注入",
        "先办事",
        "这轮看得出要查数或办事时注入。群聊里这句会被后缀裁掉。",
        (),
    ),
    PromptSpec(
        "followup",
        "会话·每轮注入",
        "省略跟进",
        "同一人用很短的句子接着上一轮动作时注入。",
        (),
    ),
    PromptSpec(
        "soft_continue",
        "会话·每轮注入",
        "同人短续聊",
        "你刚回过，同一个人马上又说一句时注入。",
        (),
    ),
    PromptSpec(
        "speaker_recall",
        "会话·每轮注入",
        "先回忆说话人",
        "群里办事要填说话人身上的事实、而这句没写时注入。",
        (),
    ),
    PromptSpec(
        "memory_qa",
        "会话·每轮注入",
        "问已有记忆",
        "在问以前说过的话，或私聊问答时注入。",
        (),
    ),
    PromptSpec(
        "multi_speaker",
        "会话·每轮注入",
        "多人发言",
        "一条消息里混了至少两个说话人，且有人在找你时注入。",
        (),
    ),
    PromptSpec(
        "address_gate",
        "会话·每轮注入",
        "在叫别人",
        "这条 @ 的是另一个人时注入。本轮没有工具。",
        (),
    ),
    PromptSpec(
        "quote_tome",
        "会话·每轮注入",
        "引用了你",
        "对方引用了你的上一条，但不是直接吩咐时注入。",
        (),
    ),
    PromptSpec(
        "heartbeat_decision",
        "定时巡检",
        "巡检决策",
        "决定要不要开口。末尾的 JSON 输出要求由程序固定，显示在正文下面。群况由程序填入。",
        (
            "current_time",
            "history_context",
            "group_summary_section",
            "proactive_merge_section",
            "masters_section",
            "recent_speak_section",
            "staleness_section",
        ),
    ),
    PromptSpec(
        "heartbeat_speak",
        "定时巡检",
        "巡检发言",
        "决策通过之后，用这段让人格说出那一句。",
        (
            "history_context",
            "proactive_merge_section",
            "masters_section",
            "staleness_section",
            "mood",
            "master_title",
        ),
    ),
    PromptSpec(
        "reactive_gate",
        "定时巡检",
        "续聊软门",
        "免唤醒续聊时，判断这条是不是还在跟人格说话。末尾的 JSON 输出要求由程序固定，显示在正文下面。",
        ("speaker_desc", "history_context"),
    ),
    PromptSpec(
        "scheduled_executor",
        "定时任务",
        "定时任务执行体",
        "替换人格。条件不满足时要保持只输出沉默标记。",
        (),
    ),
)

_SPEC_BY_KEY: dict[str, PromptSpec] = {spec.key: spec for spec in SPECS}


def spec_keys() -> frozenset[str]:
    return frozenset(_SPEC_BY_KEY)


def spec_of(key: str) -> PromptSpec:
    return _SPEC_BY_KEY[key]


def default_body(key: str) -> str:
    """代码里的默认正文。默认组运行时读这里，不读磁盘上被人改过的副本。"""
    if key == "role_playing_start":
        return ROLE_PLAYING_START
    if key == "appearance_line":
        return _APPEARANCE_LINE
    if key == "speech_block":
        return _SPEECH_BLOCK
    if key == "answer_contract":
        return ANSWER_CONTRACT
    if key == "system_constraints":
        return SYSTEM_CONSTRAINTS.replace("__MASTERS__", "{{masters}}").replace("__MASTER_TITLE__", "{{master_title}}")
    if key == "tool_orchestration":
        return TOOL_ORCHESTRATION_CONSTRAINTS
    if key == "date_line":
        return _DATE_LINE
    if key == "capability_heading":
        return _CAPABILITY_HEADING
    if key == "roster_wrap":
        return _ROSTER_WRAP
    if key == "closing":
        return _CLOSING
    if key == "heartbeat_decision":
        return format_template_to_mustache(DECISION_USER_TEMPLATE)
    if key == "heartbeat_speak":
        return format_template_to_mustache(PROACTIVE_MESSAGE_USER_TEMPLATE)
    if key == "reactive_gate":
        return format_template_to_mustache(REACTIVE_GATE_TEMPLATE)
    if key == "scheduled_executor":
        return SCHEDULED_TASK_EXECUTOR_PROMPT
    if key == "soft_trigger":
        from gsuid_core.ai_core.context_assembly import SOFT_TRIGGER_NOTE

        return SOFT_TRIGGER_NOTE
    if key == "identity":
        return (
            "（身份：你是「{{char_name}}」。自我指称只按角色卡；他人绰号不等于你的身份，禁止改物种/性别/名字去迎合。）"
        )
    if key == "transaction_priority":
        return "（本轮有实际事务，优先调工具完成；人设口吻不是跳过理由。）"
    if key == "followup":
        from gsuid_core.ai_core.interaction_scaffold import FOLLOWUP_HINT

        return FOLLOWUP_HINT
    if key == "soft_continue":
        from gsuid_core.ai_core.interaction_scaffold import SOFT_CONTINUE_HINT

        return SOFT_CONTINUE_HINT
    if key == "speaker_recall":
        from gsuid_core.ai_core.interaction_scaffold import SPEAKER_RECALL_HINT

        return SPEAKER_RECALL_HINT
    if key == "memory_qa":
        from gsuid_core.ai_core.interaction_scaffold import MEMORY_QA_HINT

        return MEMORY_QA_HINT
    if key == "multi_speaker":
        from gsuid_core.ai_core.interaction_scaffold import MULTI_SPEAKER_HINT

        return MULTI_SPEAKER_HINT
    if key == "address_gate":
        from gsuid_core.ai_core.interaction_scaffold import ADDRESS_GATE_HINT

        return ADDRESS_GATE_HINT
    if key == "quote_tome":
        from gsuid_core.ai_core.interaction_scaffold import QUOTE_TOME_HINT

        return QUOTE_TOME_HINT
    raise KeyError(key)


def default_prompts() -> dict[str, str]:
    return {spec.key: default_body(spec.key) for spec in SPECS}
