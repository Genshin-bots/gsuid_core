"""交互脚手架（C-1/C-2/C-3 + TurnGraph + 群聊结构门）。

- C-1 跨轮省略式跟进：省略短句 → 先 list 再 modify 的结构提示。
- C-2 会话级漂移预算：立规矩次数累积提醒。
- C-3 寻址前置门：@别人且未点自己 → 零工具 / 硬静音。
- TurnGraph：本轮话语结构的一等公民（说话人/呼叫/跟进），门与装配只读它。
- 两段门辅助：`admit_pre_router`（零 LLM 结构门）与 `allow_group_speak`（策略门）。

判据均为结构/语言学范畴，不含评测载荷词。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Tuple, Optional, Sequence
from dataclasses import field, dataclass

from pydantic_ai.messages import (
    TextPart,
    ModelMessage,
    ModelRequest,
    ToolCallPart,
    ModelResponse,
    UserPromptPart,
)

from gsuid_core.ai_core.const import FOLLOWUP_MAXLEN_DEFAULT as FOLLOWUP_MAXLEN_DEFAULT
from gsuid_core.ai_core.control.lines import (
    DRIFT_REMINDER,
    AT_OTHER_MARKER,
    MEDIA_HANDLE_RE,
    MSG_SECTION_HEAD as _MSG_SECTION_HEAD,
    CLOCK_LINE_PATTERN,
    SCHED_TASK_NOUN_BASE,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.relationship import RelationshipView
    from gsuid_core.ai_core.classifier.turn_router import TurnRouteDecision

# ── 通用：说话人前缀剥离（群聊/评测消息形如「昵称(用户ID:123)：正文」）──
_SPEAKER_PREFIX_RE = re.compile(r"^[^：:（()）\n]{1,16}\(用户ID:[^)]{1,24}\)[：:]\s*")
# 生产 payload 的装饰（prepare_content_payload / handle_ai）：关系行 + 「--- 消息 ---」
# 分节 + 附件/@ 标注段落 + 每轮追加的「[当前时间：…]」行（兼容旧「【当前时间】」）。
_TIME_LINE_RE = re.compile(rf"\n?{CLOCK_LINE_PATTERN}")
_SECTION_LINE_RE = re.compile(r"^---[^\n]*---\s*$", re.MULTILINE)


def _strip_speaker_prefix(text: str) -> str:
    return _SPEAKER_PREFIX_RE.sub("", text.strip())


def extract_message_body(text: str) -> str:
    """从消息文本中提取用户正文，供本模块所有**长度/内容**类判定使用。

    兼容三种形态：生产 payload（关系行 + 「--- 消息 ---」+ 正文 + 附件/@ 段 +
    [当前时间：…] 行）、评测消息（「昵称(用户ID:x)：正文」）、裸文本。长度门必须经此剥离
    装饰行，避免生产 payload 撑爆 `references_task_management`（≤60 字）等长度上限。
    """
    t = text
    idx = t.find(_MSG_SECTION_HEAD)
    if idx != -1:
        t = t[idx + len(_MSG_SECTION_HEAD) :]
        m = _SECTION_LINE_RE.search(t)
        if m:
            t = t[: m.start()]
    t = _TIME_LINE_RE.sub("", t)
    return _strip_speaker_prefix(t)


def recent_history_texts(history: List[ModelMessage], limit: int = 6) -> List[Tuple[str, str]]:
    """从 pydantic_ai 历史中抽出最近 ``limit`` 条 (role, text)，旧→新。"""
    out: List[Tuple[str, str]] = []
    for msg in history[-limit * 2 :]:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    out.append(("user", part.content))
        elif isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content.strip():
                    out.append(("assistant", part.content))
    return out[-limit:]


# 跨轮省略跟进：依据上一轮真实 ToolCallPart 证据与当前句省略特征判定。
_SCHED_MUTATE_VERBS = r"换成|提前|推迟|延后|取消|去掉|恢复"
_FOLLOWUP_VERB_RE = re.compile(
    rf"改[成到为回]|换个|挪到|往[前后]挪|{_SCHED_MUTATE_VERBS}|不要了|不用了|"
    r"删[掉了]|别删|停了?|停一?下|别提醒|暂停|再查|再看|重新查"
)
_FOLLOWUP_THAT_RE = re.compile(r"^那[^。！？，,]{1,8}呢[？?]?$")
# 群聊消息说话人锚点：与 history_format / 评测合成前缀一致
_SPEAKER_ID_RE = re.compile(r"用户ID:([^)）\s，,]+)")


def extract_speaker_id(text: str) -> str:
    """从消息文本抽出首个「用户ID:xxx」；没有则空串。"""
    m = _SPEAKER_ID_RE.search(text or "")
    return m.group(1) if m else ""


def list_speaker_ids(text: str) -> List[str]:
    """消息中出现的全部说话人 ID（去重保序）。"""
    seen: list[str] = []
    for m in _SPEAKER_ID_RE.finditer(text or ""):
        sid = m.group(1)
        if sid not in seen:
            seen.append(sid)
    return seen


def has_recent_tool_call(history: Sequence[ModelMessage], limit: int = 6) -> bool:
    """近几条助手消息里是否有真实工具调用——「上一轮存在可跟进动作」的唯一结构证据。"""
    for msg in history[-limit * 2 :]:
        if isinstance(msg, ModelResponse) and any(isinstance(p, ToolCallPart) for p in msg.parts):
            return True
    return False


def _last_user_speaker_id(recent: List[Tuple[str, str]]) -> str:
    """近历史最近一条 user 的说话人 ID；无则空串。"""
    last_user = next((txt for r, txt in reversed(recent) if r == "user"), "")
    return extract_speaker_id(last_user)


def _history_has_any_speaker_id(recent: List[Tuple[str, str]]) -> bool:
    """近历史 user 消息是否出现过「用户ID:」锚点。"""
    return any(extract_speaker_id(txt) for r, txt in recent if r == "user")


_INTERPERSONAL_REWRITE_RE = re.compile(r"把[他她你我它]们?的.{1,6}(?:改[成为]|换成)")


def is_manage_ellipsis_form(current_text: str, *, max_len: int = FOLLOWUP_MAXLEN_DEFAULT) -> bool:
    """本句是否短省略管理形（闭类改/取消/那X呢 + 长度）。不要求 history 有 ToolCall。"""
    t = extract_message_body(current_text)
    if not t or len(t) > max_len:
        return False
    if _INTERPERSONAL_REWRITE_RE.search(t) and not _TASK_NOUN_RE.search(t):
        return False
    return bool(_FOLLOWUP_VERB_RE.search(t) or _FOLLOWUP_THAT_RE.match(t))


def detect_ellipsis_followup(
    current_text: str,
    recent: List[Tuple[str, str]],
    recent_tool_call: bool = False,
    max_len: int = FOLLOWUP_MAXLEN_DEFAULT,
    speaker_id: str = "",
) -> bool:
    """当前消息是否为「继承上一轮动作」的省略式跟进（需要先定位再操作）。

    条件（全结构，不扫历史话题词）：
    1. 本句是短省略形（闭类改/取消/那X呢 + 长度门）
    2. 近历史有真实工具调用（``recent_tool_call`` / ``has_recent_tool_call``）
    3. 群聊说话人隔离：有 ID 锚点时，最近一条 user 须为同人（防乙继承甲的槽）
    """
    if not recent or not is_manage_ellipsis_form(current_text, max_len=max_len):
        return False
    if not recent_tool_call:
        return False
    sid = speaker_id or extract_speaker_id(current_text)
    if not sid or not _history_has_any_speaker_id(recent):
        return True
    last_sid = _last_user_speaker_id(recent)
    return (not last_sid) or last_sid == sid


# 任务管理意图：查/改/删/停 已有的提醒/定时任务/日程——无论是否省略跟进，都需要调度族工具
# （list/modify/cancel/pause）才能定位既有条目。用于把这些工具补进池（比省略跟进更宽的触发面）。
_TASK_NOUN_RE = re.compile(rf"{SCHED_TASK_NOUN_BASE}|任务列表")
_TASK_MANAGE_VERB_RE = re.compile(rf"改|挪|删|停|{_SCHED_MUTATE_VERBS}|列|看看|哪些|几个|多少个|查")


def references_task_management(current_text: str) -> bool:
    """当前消息是否在「管理已有的提醒/定时任务」（查询或增删改停）——需要调度族工具定位。

    判据：同句出现调度实体名词（提醒/定时任务/日程…）+ 管理动词（查/改/删/停/列…）。
    比 :func:`detect_ellipsis_followup` 触发面更宽，覆盖"查我有哪些提醒""把X的提醒去掉"
    这类非省略、但同样需要 list/modify/cancel 才能完成的请求。
    """
    t = extract_message_body(current_text)
    if not t or len(t) > 60:
        return False
    return bool(_TASK_NOUN_RE.search(t) and _TASK_MANAGE_VERB_RE.search(t))


# ── C-2 会话级漂移预算 ──────────────────────────────────────────────
# 「立持久规矩」的结构：时间持续量词 + 说话方式/格式类宾语。两者须同时出现。
_PERSIST_QUANT_RE = re.compile(
    r"以后|从现在起|从今往后|从今天起|下次开始|往后|每次回|每句|每条|每一?句|每次说|"
    r"from now on|every (message|sentence|reply)",
    re.IGNORECASE,
)
# 人设核心档：改说话方式/自称/人设/语言/格式——这是漂移攻击面，计入 push。
# 称呼偏好（叫我X/给你起昵称）**不在此**：那是正常群社交，由人设层自行决定接不接
_STYLE_CORE_RE = re.compile(
    r"说话|口吻|口癖|语气|腔调?|敬语|自称|开场白?|开头|结尾|结束语|前缀|后缀|"
    r"每句(话)?(都|先)|都要?[加带说]|emoji|表情符?号?|换.{0,3}语言|英文|中文|日语|方言|"
    r"规矩|人设|人格|角色|风格|speak|style|tone|in\s+english|only\s+\w+",
    re.IGNORECASE,
)


def _is_style_push(text: str) -> bool:
    return bool(_PERSIST_QUANT_RE.search(text) and _STYLE_CORE_RE.search(text))


def is_persistent_style_rule(text: str) -> bool:
    """「立持久说话规矩」判定的公开入口——除 C-2 注入外，自我认知写入闸也用它
    （把攻击者的"以后每句加xx"当偏好存进 bot 级 self_model = 攻击跨会话持久化）。"""
    return _is_style_push(text or "")


def count_style_pushes(current_text: str, recent: List[Tuple[str, str]], speaker_id: str = "") -> int:
    """当前消息 + 近几轮**同一说话人**「立持久说话规矩」的尝试总数（无会话状态，逐轮重算）。

    注入阈值与去重（≥2 且计数比上轮增加才注入）在 gs_agent 装配层：单次 push 交给
    prompt 层既有条款处理，提醒只在**累积**试探时出现——这才是「预算」的本义，
    也避免一次 push 滞留在窗口里导致后续每轮都重复注入。

    ``speaker_id``：当前说话人的用户 ID。群聊共享 session 下历史 user turn 混着所有人，
    不过滤会把两个用户各提一次正常风格意见凑成「连续软磨」；传入时只累计历史里带
    「用户ID:<speaker_id>」标识的消息，不传（私聊/无 event 场景）保持全量计数。
    """
    n = 1 if _is_style_push(extract_message_body(current_text)) else 0
    for role, txt in recent:
        if role != "user" or not _is_style_push(txt):
            continue
        if speaker_id and speaker_id not in _SPEAKER_ID_RE.findall(txt):
            continue
        n += 1
    return n


# 引用同时带第二人称祈使：按直接找你，不用 quoted_tome。
_QUOTE_DIRECTED_RE = re.compile(
    r"^(?:你|您)?(?:\s|仔细|好好|重新|快){0,2}"
    r"(?:帮|给|查|看|搜|翻|算|设|改|取消|删|列|如何|怎么|怎样|评价|觉得|认为)"
)

# 呼语：角色名后紧接第二人称/祈使（模板，运行时 escape 名）
_VOCATIVE_AFTER_NAME_TMPL = (
    r"(?:^|[\s，,、：:（(]){name}(?:\s*[，,、]?\s*)"
    r"(?:你|您|帮|查|看|在|醒|听|说|来|给|最近|咋样|怎么样)"
)
_DIRECTED_REQ_RE = re.compile(r"^(?:帮我|请帮|麻烦你|你(?:帮|给|查|看)|给我(?:查|看|设|改|搜|找|订))")
_SELF_ASK_RE = re.compile(r"我.{0,16}(?:来着|是哪|几[个天票月]|多少[钱个天次度点钟]|有没有)")
# 句首呼名后的合法词界（空格/标点/语气）。不含「的」——「名的图」是旁述。
_NAME_TAIL_OK = frozenset(" \t，,、：:!！?？~～啊呀呢吧嘛哦喔诶喂哈")
# 粘在中文名后会把名字加长的昵称后缀（名+子 / 小姐姐）。今/记/帮 不是后缀。
_CJK_DIMINUTIVE = frozenset("子酱君桑哥姐妹宝喵")


def _rest_after_name(body: str, name: str) -> str | None:
    """正文以该表面开头则返回后缀；ASCII 名大小写不敏感。对不上则 None。"""
    n = len(name)
    if n == 0 or len(body) < n:
        return None
    head = body[:n]
    if head == name or (name.isascii() and head.casefold() == name.casefold()):
        return body[n:]
    return None


def _call_boundary_ok(name: str, rest: str) -> bool:
    """句首名后是否算呼叫：分隔/语气、脚本切换、或非昵称后缀的 CJK 粘连。"""
    if not rest:
        return True
    ch = rest[0]
    if ch in _NAME_TAIL_OK:
        return True
    name_ascii = name.isascii()
    if name_ascii and not ch.isascii():
        return True
    if (not name_ascii) and ch.isascii() and ch.isalpha():
        return True
    if (not name_ascii) and (not ch.isascii()) and ch not in _CJK_DIMINUTIVE:
        return True
    return False


def _at_mentions_name(message_text: str, name: str) -> bool:
    if f"@{name}" in message_text:
        return True
    if name.isascii():
        return re.search(rf"@{re.escape(name)}\b", message_text, re.IGNORECASE) is not None
    return False


def is_addressed_to_self(
    message_text: str,
    persona_name: str,
    is_tome: bool,
    *,
    extra_names: Sequence[str] = (),
) -> bool:
    """结构判据：消息是否在**呼叫**自己（非旁述提及）。

    is_tome / DIRECT / @名 / 句首呼名 / 名+呼语 → 呼叫。无 @ 的帮我/自问见 TurnGraph。
    句首名可紧贴 CJK（脚本边界或非昵称后缀）；名+子/酱 这类加长仍不算。
    """
    if is_tome:
        return True
    names: list[str] = []
    for n in (persona_name, *extra_names):
        s = (n or "").strip()
        if s and s not in names:
            names.append(s)
    names.sort(key=len, reverse=True)
    if not names:
        return False
    for name in names:
        if _at_mentions_name(message_text, name):
            return True
    body = extract_message_body(message_text)
    if not body:
        return False
    for name in names:
        rest = _rest_after_name(body, name)
        if rest is not None and _call_boundary_ok(name, rest):
            return True
        voc_flags = re.IGNORECASE if name.isascii() else 0
        pat = _VOCATIVE_AFTER_NAME_TMPL.format(name=re.escape(name))
        if re.search(pat, body, voc_flags):
            return True
    return False


def build_tool_search_query(
    current: str,
    recent_user_texts: Sequence[str] = (),
    context_tags: Sequence[str] = (),
    *,
    include_recent: bool = False,
    max_chars: int = 800,
) -> str:
    """拼工具向量检索 query。默认只用本轮原话，避免「早安+天气」串味漏召。

    include_recent 仅省略跟进时打开：拼近轮用户句 + 群语境标签。超长截尾留当前句。
    """
    cur = (current or "").strip()
    if not include_recent:
        return cur or (current or "")
    parts: list[str] = []
    for t in recent_user_texts[-3:]:
        s = (t or "").strip()
        if s:
            parts.append(s)
    if cur:
        parts.append(cur)
    tags = [x.strip() for x in context_tags if x and x.strip()]
    if tags:
        parts.append(" ".join(tags[:8]))
    if not parts:
        return current or ""
    q = "\n".join(parts)
    if len(q) <= max_chars:
        return q
    return q[-max_chars:]


# ── TurnGraph / 群聊门控 ────────────────────────────────────────────


@dataclass
class TurnGraph:
    """本轮话语结构的一等公民——门、脚手架、工具装配只读此对象。"""

    user_type: str
    message_text: str
    persona_name: str
    is_tome: bool
    primary_speaker: str
    speaker_ids: List[str] = field(default_factory=list)
    multi_speaker: bool = False
    call_to_self: bool = False
    quoted_tome: bool = False
    address_gated: bool = False
    ellipsis_followup: bool = False
    task_management: bool = False
    soft_triggered: bool = False
    style_push_count: int = 0
    recent: List[Tuple[str, str]] = field(default_factory=list)
    recent_user_bodies: Optional[List[Tuple[str, str]]] = None

    @property
    def is_group(self) -> bool:
        return self.user_type != "direct"

    @property
    def needs_task_tools(self) -> bool:
        """调度族应进池：省略跟进或明确任务管理。"""
        return self.ellipsis_followup or self.task_management

    @property
    def wants_tool_search(self) -> bool:
        """是否值得做向量工具检索（有呼叫/跟进/任务，非纯旁观）。"""
        return self.call_to_self or self.needs_task_tools


def build_turn_graph(
    message_text: str,
    *,
    persona_name: str,
    is_tome: bool,
    user_type: str,
    primary_speaker: str = "",
    recent: Sequence[Tuple[str, str]] | None = None,
    recent_user_bodies: Sequence[Tuple[str, str]] | None = None,
    soft_triggered: bool = False,
    recent_tool_call: bool = False,
    followup_max_len: int = FOLLOWUP_MAXLEN_DEFAULT,
    has_reply: bool = False,
    at_bot: bool = False,
    has_at_other: bool = False,
    is_reply_to_bot: Optional[bool] = None,
    is_bystander_quote: bool = False,
    is_stale_quote: bool = False,
) -> TurnGraph:
    """从本轮消息 + 近历史构建 TurnGraph（唯一权威结构源）。"""
    recent_list = list(recent) if recent is not None else []
    user_bodies_list = list(recent_user_bodies) if recent_user_bodies is not None else None
    text = message_text or ""
    speakers = list_speaker_ids(text)
    primary = primary_speaker or extract_speaker_id(text) or ""
    extra: tuple[str, ...] = ()
    if persona_name:
        from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces

        extra = collect_persona_surfaces(persona_name)
    # 显式点名（在文本中打出角色名）不是借楼
    textual = is_addressed_to_self(text, persona_name, False, extra_names=extra)
    if is_reply_to_bot is True:
        quoted_tome = is_bystander_quote or is_stale_quote or not textual
    elif is_reply_to_bot is False:
        quoted_tome = False
    else:
        quoted_tome = bool(at_bot or is_tome) and has_reply and not textual

    if quoted_tome and not is_bystander_quote:
        body = extract_message_body(text)
        if references_task_management(body) or _QUOTE_DIRECTED_RE.match(body):
            quoted_tome = False
    call = is_addressed_to_self(text, persona_name, is_tome, extra_names=extra)
    multi = len(speakers) >= 2
    # C-3：明确 @ 了别人（协议 at_list 或兼容标注）且未同时呼叫自己 → 零工具硬静音
    at_other_present = has_at_other or (AT_OTHER_MARKER in text)
    addr = (not is_tome) and at_other_present and (not call)
    ellipsis = detect_ellipsis_followup(
        text,
        recent_list,
        recent_tool_call=recent_tool_call,
        max_len=followup_max_len,
        speaker_id=primary,
    )
    task_mgmt = references_task_management(text)
    if not call and not addr and not at_other_present:
        body = extract_message_body(text)
        if body and _SELF_ASK_RE.search(body):
            call = True
        elif body and _DIRECTED_REQ_RE.match(body) and (ellipsis or task_mgmt or recent_tool_call):
            call = True
    pushes = count_style_pushes(text, recent_list, speaker_id=primary)
    return TurnGraph(
        user_type=user_type or "direct",
        message_text=text,
        persona_name=persona_name or "",
        is_tome=bool(is_tome),
        primary_speaker=primary,
        speaker_ids=speakers,
        multi_speaker=multi,
        call_to_self=call,
        quoted_tome=quoted_tome,
        address_gated=addr,
        ellipsis_followup=ellipsis,
        task_management=task_mgmt,
        soft_triggered=soft_triggered,
        style_push_count=pushes,
        recent=recent_list,
        recent_user_bodies=user_bodies_list,
    )


def _same_body_repeat_count(tg: TurnGraph) -> int:
    """当前说话人在近窗内与本轮相同正文的条数（含本轮）。"""
    body = extract_message_body(tg.message_text)
    if not body:
        return 0
    n = 1
    sid = tg.primary_speaker
    if tg.recent_user_bodies is not None:
        for uid, content in reversed(tg.recent_user_bodies):
            if sid and uid and uid != sid:
                continue
            if extract_message_body(content) == body:
                n += 1
        return n
    for role, text in reversed(tg.recent):
        if role != "user" or extract_message_body(text) != body:
            continue
        last_sid = extract_speaker_id(text)
        if sid and last_sid and last_sid != sid:
            continue
        n += 1
    return n


def admit_pre_router(tg: TurnGraph) -> bool:
    """一段结构准入门（零 DB / 零 LLM）：寻址门、多人无呼叫互聊与复读刷屏拦截。"""
    if not tg.is_group:
        return True
    if tg.address_gated or (tg.multi_speaker and not tg.call_to_self):
        return False
    from gsuid_core.ai_core.configs.ai_config import ai_config

    repeat_n = int(ai_config.get_config("group_repeat_body_n").data)
    if repeat_n > 1 and _same_body_repeat_count(tg) >= repeat_n:
        return False
    return True


def allow_group_speak(
    tg: TurnGraph,
    *,
    has_active_task: bool = False,
    rel: Optional["RelationshipView"] = None,
) -> bool:
    """二段群聊策略门（结合潜水开关与冷淡/敌对关系档位）；私聊一律放行。"""
    if not tg.is_group:
        return True
    if not admit_pre_router(tg):
        return False
    from gsuid_core.ai_core.configs.ai_config import ai_config

    engaged = tg.is_tome or tg.call_to_self or tg.ellipsis_followup or tg.needs_task_tools or has_active_task
    is_master = rel is not None and rel.is_master
    if bool(ai_config.get_config("group_lurk_mode").data):
        if not is_master and not engaged and not is_manage_ellipsis_form(tg.message_text):
            return False
    if rel is not None and rel.is_quiet_zone and not is_master and not engaged:
        return False
    return True


def scaffold_hints_from_graph(
    tg: TurnGraph,
    *,
    speaker_recall: bool = True,
    intent: str = "",
    persona_name: str | None = None,
    route_decision: "TurnRouteDecision | None" = None,
) -> List[str]:
    """由 TurnGraph + 前置路由结论生成 user 侧脚手架提示（单一出口）。"""
    from gsuid_core.ai_core.prompt_group.store import render_prompt

    def _turn(key: str) -> str:
        body = render_prompt(persona_name, key)
        return f"\n\n{body}" if body else ""

    hints: list[str] = []
    if tg.address_gated:
        hints.append(_turn("address_gate"))
        return hints
    if tg.quoted_tome:
        hints.append(_turn("quote_tome"))
    if tg.ellipsis_followup:
        hints.append(_turn("followup"))
    if tg.multi_speaker and tg.call_to_self:
        hints.append(_turn("multi_speaker"))
    if tg.style_push_count >= 2:
        hints.append(DRIFT_REMINDER)
    if (not tg.is_group or tg.call_to_self) and speaker_recall:
        if route_decision is None:
            # 未接前置路由（第三方替换 classifier 槽）：只按既有 intent 结构分流。
            if intent == "工具" and tg.is_group:
                hints.append(_turn("speaker_recall"))
            elif not tg.is_group and intent in ("问答", ""):
                hints.append(_turn("memory_qa"))
            return hints
        # 纯实时查数不要催 search_cognition，否则会占满思考轮。
        if route_decision.is_live_lookup and route_decision.memory_intent == "none":
            pass
        elif (intent == "工具" or route_decision.needs_committed_work) and tg.is_group:
            hints.append(_turn("speaker_recall"))
        elif route_decision.memory_intent != "none":
            hints.append(_turn("memory_qa"))
    return hints


# 通道核：发现、回想、委派、发送、提醒，以及有媒体才露的读图/读视频。
MAIN_AGENT_CORE_TOOLS: tuple[str, ...] = (
    "find_tools",
    "create_subagent",
    "web_search_tool",
    "send_message_by_ai",
    "search_cognition",
    "read_handle",
    "run_code",
    "read_image",
    "read_video",
    "dispute_directive",
    "add_once_task",
    "add_interval_task",
)


_MEDIA_LABEL_MARKERS = ("图片ID:", "图片ID：", "音频ID:", "音频ID：", "视频ID:", "视频ID：")


def message_has_media_handles(
    text: str = "",
    *,
    image_id_list: Optional[Sequence[str]] = None,
    image_list: Optional[Sequence] = None,
    audio_id: Optional[str] = None,
) -> bool:
    """本轮是否携带可寻址媒体（event 字段或正文里的框架句柄/图片ID 标注）。"""
    if image_id_list:
        return True
    if image_list:
        return True
    if audio_id:
        return True
    t = text or ""
    if not t:
        return False
    if any(m in t for m in _MEDIA_LABEL_MARKERS):
        return True
    return MEDIA_HANDLE_RE.search(t) is not None
