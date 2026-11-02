"""会话管理（消息记录）API。

控制台的「会话管理」页要像 QQ 客户端那样看消息：按段渲染、能实时收到新消息、能引用 / @ /
置顶 / 备注 / 拉黑。这里只做 HTTP + WS 的搬运，数据仍由 ``message_history`` 提供：

- 列表与消息默认来自 ``HistoryManager`` 的内存窗口（12 小时）；
  开了「消息缓存」后还会读 ``coremessagecache`` 表，补上重启后丢掉的那段；
- 置顶 / 备注是面板自己的状态，落在 ``data/webconsole_chat_prefs.json``；
  拉黑直接写框架级全局黑名单 ``sp_config.BlackList``（与「拉黑」命令同源），真的会拦消息；
- 新消息由 ``message_history.hub`` 推给 WS 订阅者，前端不用轮询。

路由统一挂在 ``/api/history/chat`` 下，避开 ``/api/history/{session_id}`` 的单段匹配。
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Set, Dict, List, Tuple, Optional, Sequence
from collections import Counter

from fastapi import Body, File, Form, Depends, WebSocket, UploadFile, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.models import Event
from gsuid_core.message_history import segments_preview, get_history_manager
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, livechat_ws_authorized
from gsuid_core.message_history.hub import get_history_hub
from gsuid_core.message_history.cache import (
    cache_size,
    cache_hours,
    cache_enabled,
    purge_expired,
    query_messages,
    fetch_latest_rows,
    query_session_stats,
)
from gsuid_core.webconsole.chat_prefs import (
    set_note,
    snapshot,
    set_pinned,
)
from gsuid_core.webconsole.history_api import _parse_session_id
from gsuid_core.message_history.segments import RecordLike, chat_message_from_record
from gsuid_core.utils.database.base_models import with_read_session

from ._api_tags import SESSION_CHAT

# WS 心跳间隔：没有新消息时也发一帧，既保活也顺便发现掉线的客户端
_WS_PING_SECONDS = 25.0
# 一次最多回多少条历史，够翻完 12 小时窗口
_MESSAGE_LIMIT_MAX = 500
# 首屏只给一页：会话 30 个、每个会话 20 条，滚到底 / 划到顶再往后拉
_SESSION_PAGE_DEFAULT = 30
_SESSION_LIMIT_MAX = 200
_MESSAGE_PAGE_DEFAULT = 20
# 列表项要的「最后一条 + 发送者快照」只在尾部这段里找，不整份读历史
_SENDER_SCAN = 50


def _fail(msg: str) -> Dict[str, Any]:
    return {"status": 1, "msg": msg, "data": None}


def _ok(data: Any) -> Dict[str, Any]:
    return {"status": 0, "msg": "ok", "data": data}


_SEND_ERROR_PREFIX = "__send_error__:"


def _send_error(receipts: Optional[List[str]], bot: Any) -> str:
    """从 ``wait_recall`` 回执里读发送结果，空串表示成功。"""
    if receipts is None:
        return ""
    for item in receipts:
        text = str(item or "")
        if text.startswith(_SEND_ERROR_PREFIX):
            return text[len(_SEND_ERROR_PREFIX) :] or "发送失败"
    if receipts:
        return ""
    if getattr(bot, "_supports_recall", None) is False:
        return ""
    return "适配器没有返回回执，消息可能没有发出去"


def _event_of(
    ws_bot_id: str,
    bot_id: str,
    bot_self_id: str,
    group_id: Optional[str],
    user_id: str,
) -> Event:
    return Event(
        bot_id=bot_id,
        real_bot_id=bot_id,
        bot_self_id=bot_self_id,
        user_id=user_id,
        group_id=group_id,
        user_type="group" if group_id else "direct",
        WS_BOT_ID=ws_bot_id,
    )


def _resolve(session_id: str) -> Optional[Event]:
    """session_id → Event。格式非法返回 None。"""
    parsed = _parse_session_id(session_id)
    if parsed is None:
        return None
    ws_bot_id, bot_id, bot_self_id, group_id, user_id = parsed
    return _event_of(ws_bot_id, bot_id, bot_self_id, group_id, user_id)


async def _group_meta(group_id: str) -> Dict[str, Any]:
    """群名 / 群头像 / 群人数。库里没有就留空，前端自己退化成群号。"""
    try:
        from gsuid_core.utils.database.models import CoreGroup

        row = await CoreGroup.base_select_data(group_id=group_id)
    except Exception:
        return {}
    if row is None:
        return {}
    return _pick_group_meta(row)


def _pick_group_meta(row: Any) -> Dict[str, Any]:
    def _pick(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text in ("", "1") else text

    try:
        count = int(row.group_count or 0)
    except (TypeError, ValueError):
        count = 0

    return {
        "group_name": _pick(row.group_name),
        "group_icon": _pick(row.group_icon),
        "group_count": count,
    }


@with_read_session
async def _select_group_rows(_self: Any, session: AsyncSession, group_ids: List[str]) -> List[Any]:
    """一次取回这一页用到的群。会话列表别再按会话逐个点查。

    第一个参数是 ``with_read_session`` 要求的「self」占位（它按 ``func(self, session, *args)``
    调用），模块级函数用不上，传 None 即可。
    """
    from sqlmodel import col as _col, select as _select

    from gsuid_core.utils.database.models import CoreGroup

    statement = _select(CoreGroup).where(_col(CoreGroup.group_id).in_(group_ids)).order_by(_col(CoreGroup.id))
    return list((await session.execute(statement)).scalars().all())


async def _group_metas(group_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """批量版 ``_group_meta``：一页会话只查一次库，不是每个会话查一次。"""
    wanted = [gid for gid in dict.fromkeys(str(item or "") for item in group_ids) if gid]
    if not wanted:
        return {}
    try:
        rows = await _select_group_rows(None, wanted)
    except Exception:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        gid = str(row.group_id or "")
        # 同一群号可能有多行（历史遗留），取 id 最小的那条，和 base_select_data 的顺序一致
        if gid and gid not in result:
            result[gid] = _pick_group_meta(row)
    return result


@with_read_session
async def _select_user_rows(_self: Any, session: AsyncSession, user_ids: List[str]) -> List[Any]:
    """一次取回这一页用到的用户，顺序按 id，和 ``base_select_data`` 取第一条一致。"""
    from sqlmodel import col as _col, select as _select

    from gsuid_core.utils.database.models import CoreUser

    statement = _select(CoreUser).where(_col(CoreUser.user_id).in_(user_ids))
    return list((await session.execute(statement.order_by(_col(CoreUser.id)))).scalars().all())


async def _user_metas(pairs: Sequence[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict[str, str]]:
    """批量版 ``_user_meta``：一页会话只查一次库，不是每个会话查一次。

    入参是 ``(bot_id, user_id)``。返回的键同时有 ``(bot_id, user_id)`` 和 ``("", user_id)``，
    后者给平台对不上的情况兜底（同一用户在不同平台的头像本来就一样）。
    """
    wanted = [(str(b or ""), str(u or "")) for b, u in pairs]
    user_ids = list(dict.fromkeys(uid for _, uid in dict.fromkeys(wanted) if uid))
    if not user_ids:
        return {}
    try:
        rows = await _select_user_rows(None, user_ids)
    except Exception:
        return {}

    def _pick(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text in ("", "1") else text

    result: Dict[Tuple[str, str], Dict[str, str]] = {}
    for row in rows:
        meta = {"user_name": _pick(row.user_name), "user_icon": _pick(row.user_icon)}
        if not meta["user_name"] and not meta["user_icon"]:
            continue
        result.setdefault((str(row.bot_id or ""), str(row.user_id or "")), meta)
        result.setdefault(("", str(row.user_id or "")), meta)
    return result


async def _user_meta(user_id: str, bot_id: str) -> Dict[str, str]:
    """早柚用户库里的昵称 / 头像。给 @ 补昵称、会话列表补标题读的都是这一份。"""
    query: Dict[str, str] = {"user_id": user_id}
    if bot_id:
        query["bot_id"] = bot_id
    try:
        from gsuid_core.utils.database.models import CoreUser

        row = await CoreUser.base_select_data(**query)
    except Exception:
        return {}
    if row is None:
        return {}

    def _pick(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text in ("", "1") else text

    return {
        "user_name": _pick(row.user_name),
        "user_icon": _pick(row.user_icon),
    }


def _last_sender(records: Sequence[RecordLike], user_id: str = "", need: str = "") -> Dict[str, str]:
    """倒着找最近一条 sender 快照；给了 user_id 就只认这个人，need 要求带上某个键。"""
    for record in reversed(records):
        meta = record.metadata if isinstance(record.metadata, dict) else {}
        sender = meta.get("sender")
        if not isinstance(sender, dict):
            continue
        if user_id and str(sender.get("user_id") or "") != str(user_id):
            continue
        if need and not sender.get(need):
            continue
        return {str(k): str(v) for k, v in sender.items()}
    return {}


def _session_sender(records: Sequence[RecordLike], user_id: str = "") -> Tuple[Dict[str, str], Dict[str, str]]:
    """一次倒扫同时拿两样：这个人最近的 sender 快照、任意带群名的快照。"""
    sender: Dict[str, str] = {}
    named: Dict[str, str] = {}
    for record in reversed(records):
        meta = record.metadata if isinstance(record.metadata, dict) else {}
        snapshot = meta.get("sender")
        if not isinstance(snapshot, dict):
            continue
        plain = {str(k): str(v) for k, v in snapshot.items()}
        if not sender and (not user_id or str(snapshot.get("user_id") or "") == str(user_id)):
            sender = plain
        if not named and snapshot.get("group_name"):
            named = plain
        if sender and named:
            break
    return sender, named


async def _cached_sender(session_id: str, user_id: str = "") -> Dict[str, str]:
    """内存历史里没有就翻消息缓存。

    消息只在内存里留 12 小时，进程一重启就空了；缓存里存了完整的 sender 快照，
    否则刚重启那会儿点开头像只能看到一串 ID。
    """
    from gsuid_core.message_history.cache import CoreMessageCache

    try:
        rows = await CoreMessageCache.select_recent(session_id, 50)
    except Exception:
        return {}
    for row in reversed(rows):
        try:
            sender = json.loads(row.sender or "{}")
        except ValueError:
            continue
        if not isinstance(sender, dict):
            continue
        if user_id and str(sender.get("user_id") or "") != str(user_id):
            continue
        return {str(k): str(v) for k, v in sender.items()}
    return {}


# ---------------------------------------------------------------------------
# @ 昵称
# ---------------------------------------------------------------------------


async def _fill_at_names(messages: List[Dict[str, Any]], bot_id: str) -> None:
    """给 at 段补昵称。"""
    candidates: List[str] = []
    for item in messages:
        for seg in item.get("segments") or []:
            if seg.get("type") != "at" or seg.get("name"):
                continue
            value = str(seg.get("qq") or "")
            if value and value not in candidates:
                candidates.append(value)
    if not candidates:
        return

    from gsuid_core.utils.database.models import CoreUser

    try:
        names = await CoreUser.get_names_by_ids(candidates[:200], bot_id)
    except Exception:
        return
    if not names:
        return

    for item in messages:
        for seg in item.get("segments") or []:
            if seg.get("type") != "at" or seg.get("name"):
                continue
            value = str(seg.get("qq") or "")
            if value in names:
                seg["name"] = names[value]


# ---------------------------------------------------------------------------
# 全局黑名单
# ---------------------------------------------------------------------------


def _blacklist_ids() -> List[str]:
    """框架级全局黑名单，和「拉黑」命令读的是同一份 ``sp_config.BlackList``。"""
    try:
        from gsuid_core.utils.plugins_config.gs_config import sp_config

        return [str(item) for item in sp_config.get_config("BlackList").data]
    except Exception:
        return []


def _is_blacklisted(target_id: str) -> bool:
    return bool(target_id) and target_id in _blacklist_ids()


def _set_blacklisted(target_id: str, on: bool) -> None:
    from gsuid_core.utils.plugins_config.gs_config import sp_config

    current = _blacklist_ids()
    if on:
        if target_id in current:
            return
        current.append(target_id)
    else:
        if target_id not in current:
            return
        current = [item for item in current if item != target_id]
    sp_config.set_config("BlackList", current)


# ---------------------------------------------------------------------------
# 会话列表
# ---------------------------------------------------------------------------


async def _title_avatar(
    is_group: bool,
    group_id: Any,
    user_id: Any,
    sender: Dict[str, str],
    group_meta: Optional[Dict[str, Any]] = None,
    user_meta: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """会话标题与头像。"""
    if is_group:
        name = str(sender.get("group_name") or "")
        icon = str(sender.get("group_icon") or "")
        if name:
            return name, icon
        meta = group_meta if group_meta is not None else await _group_meta(str(group_id))
        return meta.get("group_name", "") or str(group_id), icon or meta.get("group_icon", "")

    return (
        str(sender.get("card") or sender.get("nickname") or user_id or "未知"),
        str(sender.get("avatar") or (user_meta or {}).get("user_icon") or ""),
    )


async def _session_item(
    session_id: str,
    parsed: Tuple[str, str, str, Optional[str], str],
    *,
    message_count: int,
    last_segments: List[Any],
    last_content: str = "",
    last_role: str,
    last_time: float,
    last_access: Any,
    created_at: Any,
    sender: Dict[str, str],
    prefs: Dict[str, Any],
    blacklist: Set[str],
    group_meta: Optional[Dict[str, Any]] = None,
    user_meta: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """拼一条会话列表项。内存与缓存两条来源共用，避免两份字段各写一遍。"""
    ws_bot_id, bot_id, bot_self_id, group_id, user_id = parsed
    is_group = bool(group_id)
    title, avatar = await _title_avatar(is_group, group_id, user_id, sender, group_meta, user_meta)
    item: Dict[str, Any] = {
        "session_id": session_id,
        "type": "group" if is_group else "private",
        "WS_BOT_ID": ws_bot_id,
        "bot_id": bot_id,
        "bot_self_id": bot_self_id,
        "group_id": group_id,
        "user_id": user_id,
        "title": title,
        "avatar": avatar,
        "message_count": message_count,
        "last_access": last_access,
        "created_at": created_at,
        "last_preview": segments_preview(last_segments) if last_segments else last_content,
        "last_role": last_role,
        "last_time": last_time,
        "pinned": session_id in prefs["pins"],
        "blacklisted": (str(group_id) if is_group else str(user_id)) in blacklist,
        "note": prefs["notes"].get(session_id, ""),
    }
    return item


async def _session_headers(
    prefs: Dict[str, Any],
    only: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """排序与分页只需要元数据：会话号、最后时间、条数。

    ``only`` 给了就只构造这几个会话（``/sessions?ids=`` 用），内存和缓存两侧
    都只碰这些，不会为了一项去扫全表。
    """
    headers: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for session_id, info in get_history_manager().get_all_sessions_info().items():
        if only is not None and session_id not in only:
            continue
        seen.add(session_id)
        headers.append(
            {
                "session_id": session_id,
                "pinned": session_id in prefs["pins"],
                "parsed": (
                    str(info.get("WS_BOT_ID") or ""),
                    str(info.get("bot_id") or ""),
                    str(info.get("bot_self_id") or ""),
                    info.get("group_id"),
                    str(info.get("user_id") or ""),
                ),
                "message_count": int(info.get("history_length") or 0),
                "last_time": float(info.get("last_access") or info.get("created_at") or 0.0),
                "last_access": info.get("last_access"),
                "created_at": info.get("created_at"),
                "cached": False,
            }
        )

    if cache_enabled():
        for session_id, stats in (await query_session_stats(only)).items():
            if session_id in seen:
                continue
            parsed = _parse_session_id(session_id)
            if parsed is None:
                continue
            timestamp = float(stats["last_time"] or 0.0)
            headers.append(
                {
                    "session_id": session_id,
                    "pinned": session_id in prefs["pins"],
                    "parsed": parsed,
                    "message_count": int(stats["count"] or 0),
                    "last_time": timestamp,
                    "last_access": timestamp,
                    "created_at": timestamp,
                    "cached": True,
                }
            )

    # 置顶优先，其次按最后一条消息时间倒序；会话号兜底，让游标比较是全序
    headers.sort(key=_session_sort_key, reverse=True)
    return headers


def _session_sort_key(header: Dict[str, Any]) -> Tuple[int, float, str]:
    """列表排序键，同时也是游标比较的依据。"""
    return (1 if header["pinned"] else 0, header["last_time"], header["session_id"])


def _decode_cursor(cursor: str) -> Optional[Tuple[int, float, str]]:
    """``{pinned}:{last_time}:{session_id}`` → 排序键。session_id 自带冒号，只切两刀。"""
    if not cursor:
        return None
    try:
        pinned, last_time, session_id = cursor.split(":", 2)
        return (int(pinned), float(last_time), session_id)
    except (TypeError, ValueError):
        return None


def _encode_cursor(header: Dict[str, Any]) -> str:
    pinned, last_time, session_id = _session_sort_key(header)
    # 用 repr 而不是 :.6f —— 时间戳有 17 位有效数字，截断后解码会变小，
    # 游标那条自己就会「排在游标之后」，被下一页重复返回
    return f"{pinned}:{last_time!r}:{session_id}"


async def _session_from_header(
    header: Dict[str, Any],
    prefs: Dict[str, Any],
    blacklist: Set[str],
    latest_row: Any = None,
    group_meta: Optional[Dict[str, Any]] = None,
    user_meta: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """把一行表头补成完整列表项。

    内存会话只读尾部一小段；缓存来的会话用库里那条自带的段数组与 sender 快照，
    整行由调用方按页取（见 ``list_chat_sessions``），这里不碰数据库。
    """
    session_id: str = header["session_id"]
    parsed = header["parsed"]
    group_id, user_id = parsed[3], parsed[4]

    if header["cached"]:
        if latest_row is None:
            # 统计之后那一行刚好被清理掉了，这一项跳过
            return None
        message = latest_row.to_chat_message()
        timestamp = float(message.get("timestamp") or 0.0)
        return await _session_item(
            session_id,
            parsed,
            message_count=header["message_count"],
            last_segments=message.get("segments") or [],
            last_role=message.get("role", ""),
            last_time=timestamp,
            last_access=timestamp,
            created_at=timestamp,
            sender=message.get("sender") or {},
            prefs=prefs,
            blacklist=blacklist,
            group_meta=group_meta,
            user_meta=user_meta,
        )

    records = get_history_manager().get_tail(_event_of(*parsed), _SENDER_SCAN)
    last = records[-1] if records else None
    sender, named = _session_sender(records, "" if group_id else user_id)
    if group_id and not sender.get("group_name") and named:
        sender = {**sender, "group_name": named["group_name"]}

    return await _session_item(
        session_id,
        parsed,
        message_count=header["message_count"] or len(records),
        last_segments=[],
        last_content=str(last.content or "") if last is not None else "",
        last_role=last.role if last is not None else "",
        last_time=last.timestamp if last is not None else float(header["last_time"] or 0.0),
        last_access=header["last_access"],
        created_at=header["created_at"],
        sender=sender,
        prefs=prefs,
        blacklist=blacklist,
        group_meta=group_meta,
        user_meta=user_meta,
    )


@app.get("/api/history/chat/sessions", summary="会话列表（带置顶 / 备注 / 最后一条）", tags=SESSION_CHAT)
async def list_chat_sessions(
    limit: int = _SESSION_PAGE_DEFAULT,
    cursor: str = "",
    ids: str = "",
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """会话管理页左侧列表，置顶优先、其余按最后消息时间倒序。

    ``limit`` 默认 30；``cursor`` 传上一页返回的 ``next_cursor``，不传就是第一页。
    游标按「置顶 + 最后时间 + 会话号」比较，新消息插进来也不会漏项。

    ``ids`` 是逗号分隔的会话号：只回这几个会话、不分页。给前端「新会话第一句话」
    用 —— 那时只需要往列表里插一项，不必把已经翻过的页整份重拉。
    """
    try:
        capped = max(1, min(int(limit or _SESSION_PAGE_DEFAULT), _SESSION_LIMIT_MAX))
        after = _decode_cursor(cursor)
        prefs = snapshot()
        # 全局黑名单一次取全，别在循环里反复读配置
        blacklist = set(_blacklist_ids())
        wanted = {item for item in ids.split(",") if item}
        # 只构造这几个会话，别为了一项去扫全表；空集合传 None 表示「不筛」
        headers = await _session_headers(prefs, wanted or None)
        if wanted:
            capped = len(headers)
        total = len(headers)
        if after is not None:
            # 只留严格排在游标之后的，避免同一条被两页都返回
            headers = [item for item in headers if _session_sort_key(item) < after]
        page = headers[:capped]
        # 缓存来的会话只给这一页取整行，别为整张表拉 segments / sender
        cached_ids = [item["session_id"] for item in page if item["cached"]]
        latest_rows = await fetch_latest_rows(cached_ids)
        # 群名 / 群头像一页只查一次库，别按会话逐个点查。
        # 查不到的群传 {} 而不是 None：None 表示「没查过、你自己去查」，
        # 那会让没进 coregroup 表的群每个都多一次注定查不到的点查
        page_groups = await _group_metas([str(item["parsed"][3]) for item in page if item["parsed"][3]])
        # 私聊头像同理：sender 里没带就去用户库补，一页只查一次
        page_users = await _user_metas(
            [(str(item["parsed"][1]), str(item["parsed"][4])) for item in page if not item["parsed"][3]]
        )
        sessions = [
            item
            for item in [
                await _session_from_header(
                    header,
                    prefs,
                    blacklist,
                    latest_rows.get(header["session_id"]),
                    page_groups.get(str(header["parsed"][3] or ""), {}),
                    page_users.get((str(header["parsed"][1]), str(header["parsed"][4])))
                    or page_users.get(("", str(header["parsed"][4])), {}),
                )
                for header in page
            ]
            if item is not None
        ]
        return _ok(
            {
                "sessions": sessions,
                "count": len(sessions),
                "total": total,
                "has_more": len(headers) > capped,
                "next_cursor": _encode_cursor(page[-1]) if page and len(headers) > capped else "",
            }
        )
    except Exception as e:
        return _fail(f"获取会话列表失败: {e}")


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


@app.get("/api/history/chat/{session_id}/messages", summary="会话消息（按段返回）", tags=SESSION_CHAT)
async def get_chat_messages(
    session_id: str,
    limit: int = _MESSAGE_PAGE_DEFAULT,
    before: float = 0.0,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """返回会话里**最新的一页**消息，每条带可渲染的 ``segments``。"""
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")
    try:
        capped = max(1, min(int(limit or _MESSAGE_PAGE_DEFAULT), _MESSAGE_LIMIT_MAX))
        cursor = float(before or 0.0)
        # 多取一条用来判断「还有没有更早的」，返回前再截掉
        want = capped + 1
        # 从队尾倒扫，够这一页就停，不复制整份历史
        records = get_history_manager().get_before(ev, cursor, want)
        # 内存里没有段数组（原始数据只落库），所以只能按纯文本还原
        messages: List[Dict[str, Any]] = [chat_message_from_record(record) for record in records]

        # 缓存开着就以库为准：库里是完整段数组，内存那份只补还没落库的
        if cache_enabled():
            cached = await query_messages(session_id, want, cursor or None)
            if cached:
                # 按条数扣减，不能只用一个 set：库里 1 条、内存 2 条同内容时，
                # set 会把内存那两条都当重复丢掉，只剩一条
                cached_left = Counter((item["role"], round(item["timestamp"], 3), item["content"]) for item in cached)
                extra: List[Dict[str, Any]] = []
                for item in messages:
                    fingerprint = (item["role"], round(item["timestamp"], 3), item["content"])
                    if cached_left[fingerprint] > 0:
                        cached_left[fingerprint] -= 1
                        continue
                    extra.append(item)
                messages = sorted(cached + extra, key=lambda item: item["timestamp"])

        has_more = len(messages) > capped
        if has_more:
            # 截到一页时别把同一时间戳的一批切开，否则下一页会跳过它们
            cut = len(messages) - capped
            boundary = messages[cut]["timestamp"]
            while cut > 0 and messages[cut - 1]["timestamp"] == boundary:
                cut -= 1
            messages = messages[cut:]

        # 官机的 at 只有 openid，用 coreuser 里的昵称补上（查不到就留空，前端显示 id）
        await _fill_at_names(messages, str(ev.bot_id or ""))

        for item in messages:
            item["self"] = item["role"] == "assistant"
        return _ok(
            {
                "session_id": session_id,
                "messages": messages,
                "count": len(messages),
                "has_more": has_more,
            }
        )
    except Exception as e:
        return _fail(f"获取消息失败: {e}")


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


# 图文卡片（tuwen）
_CARD_FIELDS = ("title", "description", "pic_url", "url")


def _card_segments(raw: str) -> List[Any]:
    """Card 段：前端传 ``{type, content}``，``type`` 缺省 tuwen。"""
    from gsuid_core.segment import MessageSegment

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Card 内容必须是一个 JSON 对象")
    raw_content = payload.get("content")
    if not isinstance(raw_content, dict) or not raw_content:
        raise ValueError("Card 至少要填一个字段")

    content: Dict[str, str] = {}
    for key, value in raw_content.items():
        name = str(key).strip()
        text = str(value or "").strip()
        if name in _CARD_FIELDS and text:
            content[name] = text
    if not content:
        raise ValueError("Card 至少要填一个字段")

    card_type = str(payload.get("type") or "tuwen")
    return MessageSegment.card(content, card_type)


@app.post("/api/history/chat/{session_id}/send", summary="向会话发送消息", tags=SESSION_CHAT)
async def send_chat_message(
    session_id: str,
    mode: str = Form("plain"),
    text: str = Form(""),
    markdown: str = Form(""),
    card: str = Form(""),
    at: str = Form(""),
    reply: str = Form(""),
    images: List[UploadFile] = File(default=[]),
    records: List[UploadFile] = File(default=[]),
    videos: List[UploadFile] = File(default=[]),
    files: List[UploadFile] = File(default=[]),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """四种输入方式（对应面板上的四个页签）：

    - ``plain``：普通文本，可带 @ / 引用 / 图片
    - ``markdown``：markdown 正文，平台不支持时适配器自己会降级
    - ``card``：图文卡片（tuwen），传 ``{type, content}``
    - ``rich``：富媒体，图片 / 语音 / 视频 / 文件上传

    引用（``reply``）放在最前面，@ 放在正文之后，跟 QQ 客户端的段顺序一致。
    """
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")

    from gsuid_core.bot import Bot
    from gsuid_core.gss import gss
    from gsuid_core.segment import MessageSegment

    target_id = ev.group_id if ev.group_id else ev.user_id
    if not target_id:
        return _fail("session_id 中缺少有效的发送目标")

    ws_bot_id = str(ev.WS_BOT_ID or "")
    if not gss.active_bot:
        return _fail("当前没有任何已连接的 Bot")
    ws_bot = gss.active_bot.get(ws_bot_id)
    if ws_bot is None:
        return _fail(f"Bot WS '{ws_bot_id}' 当前未连接，无法发送消息")

    segments: List[Any] = []
    try:
        if reply.strip():
            from gsuid_core.models import Message

            segments.append(Message(type="reply", data=reply.strip()))

        if mode == "card":
            if not card.strip():
                return _fail("Card 内容不能为空")
            segments.extend(_card_segments(card))
        elif mode == "markdown":
            if markdown.strip():
                segments.extend(MessageSegment.markdown(markdown))
        elif text.strip():
            segments.append(MessageSegment.text(text))

        for uid in [item.strip() for item in at.split(",") if item.strip()]:
            segments.append(MessageSegment.at(uid))

        for upload in images:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.image(raw))
        for upload in records:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.record(raw))
        for upload in videos:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.video(raw))
        for upload in files:
            raw = await upload.read()
            if raw:
                segments.append(MessageSegment.file(raw, upload.filename or "file"))
    except ValueError as e:
        return _fail(str(e))
    except Exception as e:
        return _fail(f"组装消息失败: {e}")

    if not segments:
        return _fail("消息内容不能为空")

    try:
        # 旧的空 bot_self_id 会话并到真实会话，避免发完出现两个 session
        get_history_manager().merge_session(
            _event_of(
                ws_bot_id,
                ev.bot_id,
                "",
                ev.group_id,
                ev.user_id,
            ),
            ev,
        )
        receipts = await Bot(ws_bot, ev).send(segments, wait_recall=True)
        error = _send_error(receipts, ws_bot)
        if error:
            return _fail(f"发送消息失败: {error}")
        return _ok({"session_id": session_id, "segments": len(segments)})
    except Exception as e:
        return _fail(f"发送消息失败: {e}")


# ---------------------------------------------------------------------------
# 会话偏好：置顶 / 备注 / 拉黑
# ---------------------------------------------------------------------------


@app.post("/api/history/chat/{session_id}/prefs", summary="置顶 / 备注 / 拉黑", tags=SESSION_CHAT)
async def update_chat_prefs(
    session_id: str,
    payload: Dict[str, Any] = Body(default={}),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """三个开关可以一次改，也可以只改一个。

    请求体：``{"pinned": true, "note": "自家小群", "blacklisted": false}``。
    ``note`` 传空串就是清除备注。

    ``blacklisted`` 写的是**框架级全局黑名单**（``sp_config.BlackList``，与「拉黑」命令同源）：
    群聊记群号、私聊记 QQ 号，命中后该会话不再触发任何命令 / AI / 插件。
    """
    body = payload or {}
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")

    # 黑名单存的是群号 / QQ 号，不带 session 前缀，这样才能和命令侧对上
    target = str(ev.group_id or ev.user_id or "")
    try:
        if "pinned" in body:
            set_pinned(session_id, bool(body["pinned"]))
        if "note" in body:
            set_note(session_id, body["note"])
        if "blacklisted" in body:
            if not target:
                return _fail("session_id 中缺少群号 / QQ 号，无法拉黑")
            _set_blacklisted(target, bool(body["blacklisted"]))
    except Exception as e:
        return _fail(f"保存会话偏好失败: {e}")

    prefs = snapshot()
    return _ok(
        {
            "session_id": session_id,
            "pinned": session_id in prefs["pins"],
            "blacklisted": _is_blacklisted(target),
            "note": prefs["notes"].get(session_id, ""),
        }
    )


# ---------------------------------------------------------------------------
# 群 / 用户信息卡
# ---------------------------------------------------------------------------


@app.get("/api/history/chat/{session_id}/info", summary="群 / 用户信息卡", tags=SESSION_CHAT)
async def get_chat_target_info(
    session_id: str,
    user_id: str = "",
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """点头像弹出的那张卡。

    群聊返回群名 / 人数 / 群号；私聊返回昵称 / 群名片 / 群身份 / openid / 账号 / unionid。
    昵称头像优先用消息里的 sender 快照，没有就查早柚用户库（和 @ 补昵称同一份数据）。
    """
    ev = _resolve(session_id)
    if ev is None:
        return _fail("无效的 session_id 格式")

    try:
        manager = get_history_manager()
        records = manager.get_history(ev)
        bot_id = str(ev.bot_id or "")

        if ev.group_id:
            meta = await _group_meta(str(ev.group_id))
            return _ok(
                {
                    "type": "group",
                    "key": session_id,
                    "id": ev.group_id,
                    "name": str(meta.get("group_name") or ev.group_id),
                    "avatar": str(meta.get("group_icon") or ""),
                    "member_count": meta.get("group_count") or 0,
                }
            )

        target = user_id or ev.user_id
        sender = _last_sender(records, target) or await _cached_sender(session_id, target)
        meta = await _user_meta(target, bot_id)
        return _ok(
            {
                "type": "user",
                "key": session_id,
                "id": target,
                "name": str(sender.get("card") or sender.get("nickname") or meta.get("user_name") or target),
                "avatar": str(sender.get("avatar") or meta.get("user_icon") or ""),
                "card": str(sender.get("card") or ""),
                "role": str(sender.get("role") or ""),
                "title": str(sender.get("title") or ""),
                "openid": str(sender.get("openid") or ""),
                "unionid": str(sender.get("unionid") or ""),
            }
        )
    except Exception as e:
        return _fail(f"获取信息失败: {e}")


# ---------------------------------------------------------------------------
# 消息缓存配置
# ---------------------------------------------------------------------------


def _apply_cache_hours(hours: int) -> Dict[str, Any]:
    from gsuid_core.utils.plugins_config.gs_config import message_cache_config
    from gsuid_core.utils.plugins_config.message_cache_config import MAX_CACHE_HOURS

    value = max(0, min(int(hours), MAX_CACHE_HOURS))
    message_cache_config.set_config("CacheHours", value)
    return {"hours": value, "enabled": value > 0}


@app.get("/api/history/chat/cache", summary="消息缓存配置与占用", tags=SESSION_CHAT)
async def get_chat_cache_config(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """当前缓存时长（小时）、是否开启、库里已存多少条。

    0 表示不缓存。数据表是 ``coremessagecache``，只在开启后才会有行。
    """
    try:
        return _ok(
            {
                "hours": cache_hours(),
                "enabled": cache_enabled(),
                "count": await cache_size(),
            }
        )
    except Exception as e:
        return _fail(f"获取缓存配置失败: {e}")


@app.post("/api/history/chat/cache", summary="设置消息缓存时长", tags=SESSION_CHAT)
async def set_chat_cache_config(
    payload: Dict[str, Any] = Body(default={}),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """请求体 ``{"hours": 24}``，单位小时；传 0 就是关掉缓存。

    改小之后立刻清一次超期数据，不用等下一次定时清理。
    """
    body = payload or {}
    try:
        hours = int(body.get("hours") or 0)
    except (TypeError, ValueError):
        return _fail("hours 必须是整数")
    try:
        result = _apply_cache_hours(hours)
    except Exception as e:
        return _fail(f"保存缓存配置失败: {e}")

    removed = await purge_expired() if result["enabled"] else 0
    result["count"] = await cache_size()
    result["removed"] = removed
    return _ok(result)


@app.post("/api/history/chat/cache/clear", summary="清空消息缓存", tags=SESSION_CHAT)
async def clear_chat_cache(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """把已经缓存的消息全部删掉，不影响缓存开关本身。"""
    from gsuid_core.message_history.cache import CoreMessageCache

    try:
        removed = await CoreMessageCache.clear_all()
    except Exception as e:
        return _fail(f"清空缓存失败: {e}")
    return _ok({"removed": removed})


# ---------------------------------------------------------------------------
# 实时推送
# ---------------------------------------------------------------------------


@app.websocket("/api/history/chat/ws")
async def chat_ws(websocket: WebSocket) -> None:
    """新消息实时推送。帧格式：

    ``{"type": "message", "session_id": "...", "message": {...}}``
    ``{"type": "cleared" | "deleted", "session_id": "..."}``
    ``{"type": "ping"}``
    """
    token = websocket.query_params.get("token")
    if not livechat_ws_authorized(token):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    hub = get_history_hub()
    sub_id, queue = hub.subscribe()

    async def _pump() -> None:
        while True:
            payload: Dict[str, Any]
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=_WS_PING_SECONDS)
            except asyncio.TimeoutError:
                payload = {"type": "ping"}
            if payload.get("type") == "message":
                # 和 HTTP 路径对齐：补 self 标记与 at 昵称，否则实时推来的这条
                # 会把 openid 原样显示出来，刷新一下才对
                msg_val = payload.get("message")
                if isinstance(msg_val, dict):
                    message = dict(msg_val)
                    message["self"] = message.get("role") == "assistant"
                    ev = _resolve(str(payload.get("session_id") or ""))
                    if ev is not None:
                        await _fill_at_names([message], str(ev.bot_id or ""))
                    payload = dict(payload)
                    payload["message"] = message
            await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))

    async def _watch() -> None:
        # 客户端不会主动发东西，这里只为尽早发现断开
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break

    pump_task = asyncio.create_task(_pump())
    watch_task = asyncio.create_task(_watch())
    try:
        # 任一结束就收工：发送端报错说明对端没了，接收端返回说明对端主动断开
        _, pending = await asyncio.wait(
            {pump_task, watch_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        hub.unsubscribe(sub_id)
        for task in (pump_task, watch_task):
            if not task.done():
                task.cancel()
