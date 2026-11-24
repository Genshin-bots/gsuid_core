"""系统提示词组的字段表。正文默认值来自现有常量，启动时写进默认组。"""

from __future__ import annotations

from dataclasses import dataclass

from gsuid_core.ai_core.base_prompts import FRAMEWORK_CORE_CONSTRAINTS
from gsuid_core.ai_core.persona.prompts import (
    ROLE_PLAYING_START,
    SYSTEM_CONSTRAINTS,
    TOOL_ORCHESTRATION_CONSTRAINTS,
)
from gsuid_core.ai_core.heartbeat.decision import (
    DECISION_USER_TEMPLATE,
    PROACTIVE_MESSAGE_USER_TEMPLATE,
)
from gsuid_core.ai_core.persona.chat_style import ANSWER_CONTRACT
from gsuid_core.ai_core.prompt_group.render import format_template_to_mustache
from gsuid_core.ai_core.scheduled_task.executor import SCHEDULED_TASK_EXECUTOR_PROMPT

DEFAULT_GROUP_NAME = "默认"
BUILTIN_PERSONA_NAME = "早柚"

_SPEECH_BLOCK = "台词长度：建议不超过 {{soft}} 字（用户明确要求详细时除外）。\n发言形态：{{segment_hint}}"
_APPEARANCE_LINE = (
    "我的外貌特征：{{appearance}}\n"
    "（仅当有人问及外貌或指认图中角色是否为你时参考；日常表情包或普通配图不要主动辨认是不是自己。）"
)
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
        "framework_core",
        "会话·系统提示",
        "框架底线",
        "主人格、定时任务执行体、巡检发言和自主发言会附上这一段。留空则不附。默认组每次启动从程序写回。",
        (),
    ),
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
        "meme_protocol",
        "会话·系统提示",
        "表情协议",
        "人格打开「允许表情回复」时写入系统提示。情绪词和场景词由程序填入。{{meme_frequency}} 由人格的表情频率决定。",
        ("emotion_words", "scene_words", "meme_frequency"),
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
        "scheduled_executor",
        "定时任务",
        "定时任务执行体",
        "替换人格。条件不满足时要保持只输出沉默标记。",
        (),
    ),
    PromptSpec(
        "autonomy_system_guide",
        "自主行动",
        "自主行动",
        "人格空闲时自己观察和行动使用。正文不含当天时间和具体业务词。",
        (),
    ),
    PromptSpec(
        "autonomy_speak",
        "自主行动",
        "自主发言",
        "自主行动决定开口时，将草稿收束成人格口吻的短句。上下文、对象称呼与草稿由程序填入。",
        ("context_section", "addressing_section", "draft", "length_rule", "meme_rule"),
    ),
)

_SPEC_BY_KEY: dict[str, PromptSpec] = {spec.key: spec for spec in SPECS}


def spec_keys() -> frozenset[str]:
    return frozenset(_SPEC_BY_KEY)


def default_body(key: str) -> str:
    """代码里的默认正文。默认组运行时读这里，不读磁盘上被人改过的副本。"""
    if key == "framework_core":
        return FRAMEWORK_CORE_CONSTRAINTS
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
    if key == "meme_protocol":
        return (
            "发表情只有一种写法：单独写一行 `<meme:情绪>`，或 `<meme:情绪 场景>`。\n"
            "情绪只能是：{{emotion_words}}。\n"
            "场景可以不写，只能是：{{scene_words}}。\n"
            "不发：查资料、交付结果、讲正事、回答要准确的问题；拒绝不当请求；对方认真求助时。\n"
            "{{meme_frequency}}\n"
            "一条回复最多一个表情。表情不算台词字数，也不是括号动作。"
            "可以只写这一行，也可以这一行后面再写一句台词。"
        )
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
    if key == "scheduled_executor":
        return SCHEDULED_TASK_EXECUTOR_PROMPT
    if key == "autonomy_system_guide":
        from gsuid_core.ai_core.autonomy.runner import AUTONOMY_SYSTEM_GUIDE

        return AUTONOMY_SYSTEM_GUIDE
    if key == "autonomy_speak":
        from gsuid_core.ai_core.proactive.speak import AUTONOMY_SPEAK_USER_TEMPLATE

        return format_template_to_mustache(AUTONOMY_SPEAK_USER_TEMPLATE)
    if key == "identity":
        return (
            "（身份：你是「{{char_name}}」。自我指称只按角色卡；他人绰号不等于你的身份，禁止改物种/性别/名字去迎合。）"
        )
    if key == "transaction_priority":
        return "（本轮有实际事务，优先调工具完成；人设口吻不是跳过理由。）"
    if key == "followup":
        return (
            "\n\n（系统提示：这句是对你们上一轮动作的省略式跟进，对象承接上文。"
            "若要改/取消/暂停一个已建立的安排：先用列表类工具（如 list_scheduled_tasks）定位到目标那一条，"
            "再用对应的 modify/cancel/pause 工具精确操作——绝不要新建一条重复的；"
            "若是换个对象再查一遍，沿用上一轮的查询方式补全参数后真正去查。没调工具前不要说已完成。）"
        )
    if key == "speaker_recall":
        return (
            "\n\n（系统提示：办眼前的事若要填说话人身上的事实且本句/上文没写，"
            "先 search_cognition，query 只写「说话人ID + 要填的槽」，不要把本次外部题目的词拼进去；"
            "回想不到就问一句，禁止空槽硬查。）"
        )
    if key == "memory_qa":
        return (
            "\n\n（系统提示：这是在问或用已有记忆。"
            "已注入【相关对话片段】/【核心事实】只说明谁在何时说过，"
            "不是句中日期、数量或状态的当前事实。"
            "问谁说过什么时按片段原文作答并带上说话时点；"
            "问现在如何而原话会过期时，没有本轮工具结果就只转述说过的话。"
            "片段不够再 search_cognition，query 保留说话人原文语言的专名/主题词（不要把英文专名全译成中文），"
            "群聊可带说话人ID。"
            "目录卡不是全文。作答点名命中里的专名原话。"
            "点查过往记录且片段确实对不上才说没有，不要用相近事实顶替；"
            "问方法/步骤/举例则按问句本身作答，不因记忆不全拒答。）"
        )
    if key == "multi_speaker":
        return (
            "\n\n（系统提示：本条混入了多人发言。"
            "只处理明确找你的那一位的请求；旁白/对别人说的话不抢答、不串请求、不把甲的槽位继承给乙。"
            "若没人找你，输出 <SILENCE>。）"
        )
    if key == "address_gate":
        return (
            "\n\n（系统提示：这条消息 @ 的是群里另一个人、并不是在叫你，本轮已不提供任何工具。"
            "与你无关就输出 <SILENCE> 保持沉默，至多旁观轻带一句；绝不替被 @ 的人回话、绝不演成 TA。）"
        )
    if key == "quote_tome":
        return (
            "\n\n（系统提示：对方引用了你的消息，不等于在找你。"
            "若对方只是借你之前的消息当指代物/证据回答群里别人的提问（如前一句别人刚问、对方引用你只回“这/这个/看上面”），"
            "或是围观感叹/随口评价、在跟别人说话、故意惹你，没有向你提出具体问题或请求，输出 <SILENCE>；"
            "仅在向你追问/反驳/吩咐时才开口。）"
        )
    raise KeyError(key)


def default_prompts() -> dict[str, str]:
    return {spec.key: default_body(spec.key) for spec in SPECS}
