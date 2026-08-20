"""出站世界的写/读入口：审计、幂等、A 轨占位、决策备忘。

引用消解走 SQL 等值/前缀；闲聊回忆走认知联邦 OUTBOUND。
"""

from __future__ import annotations

import time
from typing import Optional
from datetime import datetime
from contextvars import Token, ContextVar
from dataclasses import dataclass

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event

_outbound_image_label: ContextVar[str] = ContextVar("outbound_image_label", default="")


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


def topic_from_extra(extra: dict[str, object]) -> str:
    raw = extra["outbound_topic"] if "outbound_topic" in extra else ""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:12]
    return ""


def remember_outbound_topic(extra: dict[str, object], task_head: str) -> None:
    head = (task_head or "").strip().split("\n", 1)[0].strip()[:12]
    if head:
        extra["outbound_topic"] = head


@dataclass(frozen=True)
class QuoteResolve:
    line: str
    topic: str
    handle: str
    target: str


def _fmt_hm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


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
    try:
        await OutboundAudit.record(
            session_id=sid,
            group_id=gid,
            text=(text or "").strip(),
            image_handles=handles,
            topic=(topic or "").strip()[:12],
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
    title = topic or ("图片" if handles else "台词")
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


async def resolve_quote(ev: Event) -> Optional[QuoteResolve]:
    raw = (ev.reply or "").strip()
    if not raw:
        return None
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from gsuid_core.ai_core.database.outbound import OutboundAudit

    if not _db_ready():
        return None
    gid = ledger_group_key(ev)
    try:
        hit = await OutboundAudit.match_by_text(group_id=gid, text=raw)
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))
        return None
    if hit is None:
        return None
    who = hit.target_name or hit.target_user or "对方"
    topic = hit.topic or "图片"
    handle = (hit.image_handles or "").split(",", 1)[0].strip()
    extra = f"，句柄 {handle}" if handle else ""
    line = f"（系统：引用对象：你于 {_fmt_hm(hit.ts)} 发给{who}的「{topic}」{extra}）"
    return QuoteResolve(line=line, topic=topic, handle=handle, target=who)


async def ownership_hint(ev: Event) -> str:
    """当前发言者不是最近交付对象时给一句归属。"""
    gid = ledger_group_key(ev)
    if not gid or ev.user_id is None:
        return ""
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from gsuid_core.ai_core.database.outbound import OutboundAudit

    if not _db_ready():
        return ""
    try:
        rows = await OutboundAudit.recent_for_group(gid, limit=1)
    except (OperationalError, ProgrammingError) as e:
        logger.debug(i18n_t("log.ai.outbound_read_skip", e=e))
        return ""
    if not rows:
        return ""
    last = rows[0]
    if not last.target_user or last.target_user == str(ev.user_id):
        return ""
    who = last.target_name or last.target_user
    topic = last.topic or "图片"
    return f"（系统：归属：你刚把「{topic}」发给了{who}。）"


@dataclass(frozen=True)
class ImageClaim:
    occupied: bool
    refuse: str | None


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


async def claim_image_delivery(ev: Event | None, image_id: str, *, session_id: str) -> Optional[str]:
    """同 res_id 二发则返回拒发文案。兼容旧调用。"""
    return (await try_claim_image_delivery(ev, image_id, session_id=session_id)).refuse


async def write_decision_memo(
    *,
    bot_self_id: str,
    text: str,
    ref: str = "",
    handle: str = "",
    owner_user_id: str = "",
) -> None:
    """决策点即写。零 LLM，memo ≤60 字，7 天 TTL。"""
    body = " ".join((text or "").split())[:60]
    if not body:
        return
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
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
                scope_key=make_scope_key(ScopeType.SELF, bid),
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
