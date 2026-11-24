"""出站世界的写/读入口：审计、幂等、A 轨占位、决策备忘。

引用消解走 SQL 等值/前缀；闲聊回忆走认知联邦 OUTBOUND。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Match, Callable, Iterator, Optional, Sequence, NamedTuple
from datetime import datetime
from contextvars import Token, ContextVar
from dataclasses import field, dataclass

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event

if TYPE_CHECKING:
    from gsuid_core.message_history.manager import EventLink, MessageRecord


@dataclass
class _RunImageDeliveryState:
    """单轮 run 的图片已发送状态（可变盒，确保子 Task 调用发图后父协程可见且跨 run 隔离）。"""

    delivered: bool = False
    image_ids: set[str] = field(default_factory=set)


_outbound_image_label: ContextVar[str] = ContextVar("outbound_image_label", default="")
# 泄漏句柄补发不经过 send_message_by_ai，用每轮可变盒记录「已出图」与已发图片 ID 去重。
_run_image_state: ContextVar[_RunImageDeliveryState | None] = ContextVar("run_image_state", default=None)
_OUTBOUND_TOPIC_N: int = 12


def _db_ready() -> bool:
    from gsuid_core.utils.database.base_models import async_maker

    return async_maker is not None


def ledger_group_key(ev: Event | None, *, group_id: str = "", user_id: str = "") -> str:
    if ev is not None and ev.group_id:
        return str(ev.group_id)
    if group_id:
        return group_id
    uid = str(ev.user_id) if ev is not None and ev.user_id else user_id
    return f"direct:{uid}" if uid else ""


def format_meme_placeholder(mood: str) -> str:
    """群聊历史里的表情占位。只放情绪词，方便记忆摄入当文字表情跳过。"""
    word = (mood or "").strip()
    return f"[表情:{word}]" if word else "[表情]"


def format_outbound_image_placeholder(topic: str, res_id: str) -> str:
    topic_s = (topic or "").strip()[:12]
    rid = (res_id or "").strip()
    if topic_s and rid:
        return f"[图片·{topic_s}·{rid}]"
    if rid:
        return f"[图片·{rid}]"
    return "[图片]"


def set_outbound_image_label(label: str) -> Token:
    return _outbound_image_label.set(label or "")


def get_outbound_image_label() -> str:
    return _outbound_image_label.get()


def reset_outbound_image_label(token: Token) -> None:
    _outbound_image_label.reset(token)


def note_run_image_delivered(image_id: str = "") -> None:
    state = _run_image_state.get()
    if state is None:
        state = _RunImageDeliveryState()
        _run_image_state.set(state)
    state.delivered = True
    hid = (image_id or "").strip()
    if hid:
        state.image_ids.add(hid)


def has_run_delivered_image(image_id: str) -> bool:
    hid = (image_id or "").strip()
    state = _run_image_state.get()
    return bool(hid) and state is not None and hid in state.image_ids


def take_run_image_delivered() -> bool:
    state = _run_image_state.get()
    if state is None or not state.delivered:
        return False
    state.delivered = False
    return True


def reset_run_image_delivered() -> None:
    _run_image_state.set(_RunImageDeliveryState())


# 话题标题里去掉裸 @平台ID。聊天强提醒不走这条。
_AT_ID_RE = re.compile(r"@([0-9A-Za-z][0-9A-Za-z_\-]{2,64})")
_PLATFORM_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_\-]{2,64}")
# 转义括号放在裸尖括号前，避免 \<@id\> 只吃掉中间的 <。
_MENTION_OPEN = r"(?:\\<|&lt;|&#60;|＜|<)"
_MENTION_CLOSE = r"(?:\\>|&gt;|&#62;|＞|>)"
_MENTION_RE = re.compile(
    _MENTION_OPEN + r"\s*@\s*([^<>]{1,64}?)\s*" + _MENTION_CLOSE,
    re.IGNORECASE,
)
_CQ_AT_RE = re.compile(r"\[CQ:at\s*,\s*qq=([^,\]\s]+)(?:,[^\]]*)?\]")
_DELIVERY_TASK_TITLE_RE = re.compile(r"任务#\d+「([^」]+)」")


def normalize_mention_token(raw: str) -> str:
    """括号内的空格去掉。名称和用户 ID 都不靠空格区分。"""
    return re.sub(r"\s+", "", raw)


def is_mention_id(token: str) -> bool:
    """纯数字，或带数字的平台 ID。纯单词不是接收人。"""
    if not token:
        return False
    if token.isdigit():
        return True
    return _PLATFORM_ID_RE.fullmatch(token) is not None and any(ch.isdigit() for ch in token)


def iter_mentions(text: str) -> Iterator[Match[str]]:
    """扫描 <@用户ID> / <@名称>。认转义尖括号和括号内外的空格。"""
    return _MENTION_RE.finditer(text or "")


def sub_mentions(text: str, repl: Callable[[Match[str]], str]) -> str:
    if not text:
        return text
    return _MENTION_RE.sub(repl, text)


def strip_mention_tags(text: str) -> str:
    if not text:
        return text
    return _MENTION_RE.sub("", text)


def rewrite_cq_at(text: str) -> str:
    """把 [CQ:at,qq=ID] 收成 <@ID>。出站解析、主动消息历史、话题标题共用。"""
    if not text:
        return text
    return _CQ_AT_RE.sub(r"<@\1>", text)


def _clean_topic_head(raw: str, max_len: int = 12) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    m = _DELIVERY_TASK_TITLE_RE.search(s)
    if m is not None:
        s = m.group(1).strip()
    s = _AT_ID_RE.sub("", strip_mention_tags(rewrite_cq_at(s))).strip()
    head = s.split("\n", 1)[0].strip()
    return head[:max_len]


def topic_from_extra(extra: dict[str, object]) -> str:
    raw = extra["outbound_topic"] if "outbound_topic" in extra else ""
    if isinstance(raw, str) and raw.strip():
        return _clean_topic_head(raw, 12)
    return ""


def remember_outbound_topic(extra: dict[str, object], task_head: str) -> None:
    head = _clean_topic_head(task_head, 12)
    if head:
        extra["outbound_topic"] = head


async def ensure_outbound_topic_from_artifact(extra: dict[str, object], image_id: str) -> None:
    if not image_id.startswith("res_") or topic_from_extra(extra) or not _db_ready():
        return
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from gsuid_core.ai_core.planning.models import AIAgentArtifact

    try:
        art = await AIAgentArtifact.get_by_id(image_id)
    except (OperationalError, ProgrammingError):
        return
    if art is not None and art.summary:
        remember_outbound_topic(extra, art.summary)


@dataclass(frozen=True)
class QuoteResolve:
    line: str
    topic: str
    handle: str
    target: str


def _fmt_hm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def proactive_directed_target(event: Event, message: str) -> str:
    """群播报只有正文里的 <@用户ID> 才算定向。裸 @ 不算。

    没有点名时不要把 Event.user_id 写成接收人，否则下一条别人的话会被记成发给这个人。
    """
    if event.group_id:
        rewritten = rewrite_cq_at(message or "")
        for match in iter_mentions(rewritten):
            token = normalize_mention_token(match.group(1))
            if is_mention_id(token):
                return token
        return ""
    return str(event.user_id) if event.user_id else ""


async def record_outbound(
    *,
    ev: Event | None,
    session_id: str,
    text: str,
    image_id: str,
    topic: str,
    target_user: str,
    target_name: str = "",
) -> None:
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.database.outbound import OutboundAudit, purge_outbound_expired
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    gid = ledger_group_key(ev)
    if not gid:
        return
    from sqlalchemy.exc import OperationalError, ProgrammingError

    if not _db_ready():
        return
    sid = session_id or (ev.session_id if ev is not None else "")
    owner = str(ev.user_id) if ev is not None and ev.user_id else target_user
    handles = (image_id or "").strip()
    topic_s = _clean_topic_head(topic, 12)
    if not topic_s and handles.startswith("res_"):
        from gsuid_core.ai_core.planning.models import AIAgentArtifact

        try:
            art = await AIAgentArtifact.get_by_id(handles)
        except (OperationalError, ProgrammingError):
            art = None
        if art is not None and art.summary:
            topic_s = _clean_topic_head(art.summary, 12)
    if not topic_s:
        topic_s = _clean_topic_head((text or "").replace("\n", " "), _OUTBOUND_TOPIC_N)
    try:
        await OutboundAudit.record(
            session_id=sid,
            group_id=gid,
            text=(text or "").strip(),
            image_handles=handles,
            topic=topic_s,
            target_user=target_user,
            target_name=target_name,
            owner_user_id=owner,
        )
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))
        return
    scope_key = (
        make_scope_key(ScopeType.GROUP, gid)
        if not gid.startswith("direct:")
        else make_scope_key(ScopeType.USER_GLOBAL, owner)
    )
    title = topic_s or ("图片" if handles else "台词")
    summary = (text or title).strip().replace("\n", " ")[:80]
    if handles:
        summary = f"{title} {handles} {summary}".strip()[:80]
    ref = f"ob:{sid}:{int(time.time())}:{handles or 't'}"[:160]
    await remember(
        MemoryWrite(
            kind=CogKind.OUTBOUND,
            ref=ref,
            scope_key=scope_key,
            owner_user_id=owner,
            title=title[:60],
            summary=summary,
            as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
            source="outbound",
            handle=handles,
        )
    )
    try:
        await purge_outbound_expired()
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_purge_skip", e=e))


def inspect_quote_relation(
    ev: Event,
    *,
    records: Optional[Sequence["MessageRecord"]] = None,
    primary_link: Optional["EventLink"] = None,
) -> tuple[bool | None, bool, bool]:
    """检查当前事件引用的消息与 Bot、当前说话人的物理关系（100% 结构化事实源）。

    优先消费当前 ``EventLink.reply_to_seq`` 命中内存记录，无缓存链接时回退按 ``reply_id`` 检索。
    """
    rec: Optional["MessageRecord"] = None
    if primary_link is not None and primary_link.reply_to_seq is not None and records is not None:
        rec = next(
            (r for r in records if r.link is not None and r.link.seq == primary_link.reply_to_seq),
            None,
        )
    if rec is None:
        reply_id = (ev.reply_id or "").strip()
        if not reply_id:
            return None, False, False

        from gsuid_core.message_history import get_history_manager

        rec = get_history_manager().find_by_msg_id(ev, reply_id)
    if rec is None:
        return None, False, False

    bot_self_id = str(ev.bot_self_id or "")
    is_bot = (rec.role == "assistant") or (bool(bot_self_id) and str(rec.user_id) == bot_self_id)
    if not is_bot:
        return False, False, False

    meta = rec.metadata or {}
    reply_to_uid = str(meta["reply_to_user_id"]) if "reply_to_user_id" in meta else ""
    cur_uid = str(ev.user_id or "")
    is_bystander = bool(reply_to_uid and cur_uid and reply_to_uid != cur_uid)
    time_delta = max(0.0, time.time() - rec.timestamp)
    is_stale = time_delta > 180.0
    return True, is_bystander, is_stale


async def resolve_quote(ev: Event) -> Optional[QuoteResolve]:
    raw = (ev.reply or "").strip()
    reply_id = (ev.reply_id or "").strip()
    is_image_quote = bool(
        (reply_id or ev.is_tome) and (not raw or raw.startswith("[图片")) and (ev.image_id_list or ev.image)
    )
    if not raw and not is_image_quote and not reply_id:
        return None

    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.control.lines import (
        quote_bot_line,
        quote_self_line,
        quote_other_line,
        quote_stale_line,
        quote_object_line,
        quote_bystander_line,
        quote_command_card_line,
    )

    # 1. 优先通过 reply_id 在 A 轨历史中按平台 msg_id 精确查找
    if reply_id:
        rec = get_history_manager().find_by_msg_id(ev, reply_id)
        if rec is not None:
            time_str = datetime.fromtimestamp(rec.timestamp).strftime("%H:%M:%S")
            time_delta = max(0.0, time.time() - rec.timestamp)
            bot_self_id = str(ev.bot_self_id or "")
            is_self_echo = bool(bot_self_id) and str(rec.user_id) == bot_self_id
            if rec.role == "assistant" or is_self_echo:
                meta = rec.metadata or {}
                channel = meta["speech_channel"] if "speech_channel" in meta else None
                reply_to_uid = str(meta["reply_to_user_id"]) if "reply_to_user_id" in meta else ""
                reply_to_uname = str(meta["reply_to_user_name"]) if "reply_to_user_name" in meta else ""
                is_bystander = bool(reply_to_uid and ev.user_id and reply_to_uid != str(ev.user_id))
                is_stale = time_delta > 180.0
                target_desc = reply_to_uname or (f"用户{reply_to_uid}" if reply_to_uid else "群内")
                if channel == "command":
                    return QuoteResolve(
                        line=quote_command_card_line(time_str),
                        topic="命令卡片",
                        handle="",
                        target="群内",
                    )
                gid = ledger_group_key(ev)
                hit = None
                if _db_ready() and gid:
                    from sqlalchemy.exc import OperationalError, ProgrammingError

                    from gsuid_core.ai_core.database.outbound import OutboundAudit

                    try:
                        if raw and not raw.startswith("[图片"):
                            hit = await OutboundAudit.match_by_text(group_id=gid, text=raw)
                    except (OperationalError, ProgrammingError) as e:
                        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))
                if hit is not None:
                    who = hit.target_name or hit.target_user or "对方"
                    topic = _clean_topic_head(hit.topic or hit.text, 12) or "图片"
                    handle = (hit.image_handles or "").split(",", 1)[0].strip()
                    extra = f"，句柄 {handle}" if handle else ""
                    line = quote_object_line(_fmt_hm(hit.ts), who, topic, extra)
                    return QuoteResolve(
                        line=line,
                        topic=topic,
                        handle=handle,
                        target=who,
                    )
                if is_bystander:
                    line = quote_bystander_line(time_str, target_desc)
                elif is_stale:
                    line = quote_stale_line(time_str, int(time_delta // 60))
                else:
                    line = quote_bot_line(time_str)
                return QuoteResolve(
                    line=line,
                    topic="你的发言",
                    handle="",
                    target=target_desc,
                )
            elif rec.role == "user":
                if str(rec.user_id) == str(ev.user_id):
                    return QuoteResolve(
                        line=quote_self_line(time_str),
                        topic="说话人自己的发言",
                        handle="",
                        target="当前说话人",
                    )
                else:
                    from gsuid_core.ai_core.utils import sanitize_display_name

                    speaker = sanitize_display_name(rec.user_name, str(rec.user_id)) or f"用户{rec.user_id}"
                    return QuoteResolve(
                        line=quote_other_line(speaker, time_str),
                        topic="群友发言",
                        handle="",
                        target=speaker,
                    )

    # 2. 兜底：A 轨未查到 msg_id 时，仅对有正文文本做 OutboundAudit 匹配（禁止全群盲抓图片）
    if not _db_ready():
        return None
    gid = ledger_group_key(ev)
    if not gid or not raw or raw.startswith("[图片"):
        return None

    from sqlalchemy.exc import OperationalError, ProgrammingError

    from gsuid_core.ai_core.database.outbound import OutboundAudit

    hit = None
    try:
        hit = await OutboundAudit.match_by_text(group_id=gid, text=raw)
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))
        return None
    if hit is None:
        return None
    who = hit.target_name or hit.target_user or "对方"
    topic = _clean_topic_head(hit.topic or hit.text, 12) or "图片"
    handle = (hit.image_handles or "").split(",", 1)[0].strip()
    extra = f"，句柄 {handle}" if handle else ""
    from gsuid_core.ai_core.control.lines import quote_object_line

    line = quote_object_line(_fmt_hm(hit.ts), who, topic, extra)
    return QuoteResolve(line=line, topic=topic, handle=handle, target=who)


@dataclass(frozen=True)
class ImageClaim:
    occupied: bool
    refuse: str | None


class ResOwner(NamedTuple):
    found: bool
    owner_user_id: str
    root_task_id: str = ""
    is_draft: bool = False
    task_running: bool = False


async def lookup_res_owner(res_id: str) -> ResOwner:
    """res_ 存在性、归属人与定稿态（单次查询供出站汇点把关在途草稿与反向销账）。"""
    rid = (res_id or "").strip()
    if not rid.startswith("res_"):
        return ResOwner(False, "")
    from gsuid_core.ai_core.planning.models import AIAgentTask, AIAgentArtifact

    art = await AIAgentArtifact.get_by_id(rid)
    if art is None:
        return ResOwner(False, "")
    is_draft = art.artifact_kind == "draft"
    source = await AIAgentTask.get_by_id(art.root_task_id)
    if source is None:
        return ResOwner(True, "", art.root_task_id or "", is_draft, False)
    node = source if art.task_id == art.root_task_id else await AIAgentTask.get_by_id(art.task_id)
    running = (node is not None and node.status == "running") or source.status == "running"
    return ResOwner(True, (source.owner_user_id or "").strip(), source.id, is_draft, running)


def speaker_may_take_res(owner: ResOwner, speaker_user_id: str) -> bool:
    """产物不存在时交给原有的解析失败路径；存在则必须是当前说话人。"""
    if not owner.found:
        return True
    speaker = (speaker_user_id or "").strip()
    if not speaker or not owner.owner_user_id:
        return False
    return owner.owner_user_id == speaker


def res_owner_refusal(owner: ResOwner, speaker_user_id: str) -> str:
    if not (speaker_user_id or "").strip():
        return "⚠️ 当前对话没有说话人，未发送该图。"
    if not owner.owner_user_id:
        return "⚠️ 该图没有发起人记录，未发送。"
    return "⚠️ 该图属于其它发起人，未发送。请用本帧交付的句柄发给当前发起人。"


async def try_claim_image_delivery(ev: Event | None, image_id: str, *, session_id: str) -> ImageClaim:
    """占位。occupied=本次插入；refuse=已发过的拒发文案。"""
    rid = (image_id or "").strip()
    if not rid.startswith("res_"):
        return ImageClaim(occupied=False, refuse=None)
    gid = ledger_group_key(ev)
    if not gid:
        return ImageClaim(occupied=False, refuse=None)
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from gsuid_core.ai_core.database.outbound import DeliveryLedger

    if not _db_ready():
        return ImageClaim(occupied=False, refuse=None)
    try:
        hit = await DeliveryLedger.check_and_claim(gid, rid, session_id=session_id)
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))
        return ImageClaim(occupied=False, refuse=None)
    if hit is None:
        return ImageClaim(occupied=True, refuse=None)
    logger.warning(
        i18n_t(
            "log.ai.delivery_ledger_duplicate",
            res_id=rid,
            group_id=gid,
            ts=_fmt_hm(hit.ts),
        )
    )
    return ImageClaim(
        occupied=False,
        refuse=f"⚠️ 该图（{rid}）本群已于 {_fmt_hm(hit.ts)} 发送过，本次未重发。",
    )


async def release_image_delivery(ev: Event | None, image_id: str) -> None:
    """发送失败时释放本次占位，避免 7 天内无法重试。"""
    rid = (image_id or "").strip()
    gid = ledger_group_key(ev)
    if not rid.startswith("res_") or not gid:
        return
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from gsuid_core.ai_core.database.outbound import DeliveryLedger

    if not _db_ready():
        return
    try:
        await DeliveryLedger.release(gid, rid)
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))


async def write_decision_memo(
    *,
    bot_self_id: str,
    text: str,
    ref: str = "",
    handle: str = "",
    owner_user_id: str = "",
    persona_name: str = "",
) -> None:
    """决策点即写。零 LLM，memo ≤60 字，7 天 TTL。"""
    body = " ".join((text or "").split())[:60]
    if not body:
        return
    from gsuid_core.ai_core.memory.scope import self_scope_key
    from gsuid_core.ai_core.cognition.nodes import AICogNode
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    bid = bot_self_id or "default"
    key = (ref or f"decision:{bid}:{int(time.time())}")[:160]
    from sqlalchemy.exc import OperationalError, ProgrammingError

    if not _db_ready():
        return
    try:
        await remember(
            MemoryWrite(
                kind=CogKind.SELF_NOTE,
                ref=key,
                scope_key=self_scope_key(bid, persona_name),
                owner_user_id=owner_user_id,
                title="决策备忘",
                summary=body,
                as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
                source="decision_memo",
                handle=handle[:64],
            )
        )
        cutoff = int(time.time()) - 7 * 24 * 3600
        await AICogNode.purge_source_before("decision_memo", before_ts=cutoff)
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))
