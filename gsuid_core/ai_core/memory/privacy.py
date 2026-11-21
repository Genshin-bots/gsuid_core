"""跨群隔离与按人私聊隐私边界的单一事实源。

对称公理：
- 群聊（group:{gid}）：仅可见本群数据 + 迁移后纯净公共 self + 本人跨群沟通偏好。
- 私聊（user_id=U）：跨私聊与全部群聊聚合，但严格只返回与本人 U 相关的记忆与消息，
  多说话人群片段按消息行裁剪，绝不向 U 泄露同群他人的对话、事实边或笔记。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Sequence
from datetime import datetime, timezone

if TYPE_CHECKING:
    from gsuid_core.message_history import MessageRecord
    from gsuid_core.ai_core.memory.retrieval.types import Edge, Entity, Episode, MemoryEventCue
    from gsuid_core.ai_core.memory.retrieval.dual_route import MemoryContext

_DM_GROUP_SCOPE_CAP = 50
_PERSON_ENTITY_TYPE_MARKERS: tuple[str, ...] = (
    "speaker",
    "familiar",
    "person",
    "user",
    "member",
    "人物",
    "群友",
    "成员",
    "用户",
    "说话人",
    "联系人",
)
_LINE_SPEAKER_RE = re.compile(
    r"^\s*[^\n]{1,60}?\((?:用户ID[:：])?(?P<uid>[0-9A-Za-z_.-]{1,64})\)"
    r"(?:\s*(?:->\s*回复[^\n:：]*|@[^\n:：]*))?\s*[:：]"
)
# 行内「名(uid):」标注：会话摘要等单行汇总会把多人原话拼进一行，行首解析看不到。
_INLINE_SPEAKER_RE = re.compile(
    r"\((?:用户ID[:：])?(?P<uid>[0-9A-Za-z_.-]{1,64})\)"
    r"(?:\s*(?:->\s*回复[^\n:：]*|@[^\n:：]*))?\s*[:：]"
)


def utc_timestamp(dt: datetime | None) -> float | None:
    """将数据库 naive UTC 或带时区 datetime 统一转为 UTC Unix 时间戳，消除本地时区偏移。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).timestamp()
    return dt.timestamp()


def _contains_exact_uid(text: str, uid: str) -> bool:
    """按词边界匹配 uid；短 ID（<7 位）要求身份前缀或括号，防普通词/价格/股号误命中。"""
    if not text or not uid:
        return False
    escaped = re.escape(uid)
    if len(uid) < 7:
        return (
            re.search(
                rf"(?:用户ID[:：]|<@|@|替[:：]|用户|\buid[:：=]|\()\s*{escaped}(?![0-9A-Za-z_])",
                text,
                re.IGNORECASE,
            )
            is not None
        )
    return re.search(rf"(?<![0-9A-Za-z_]){escaped}(?![0-9A-Za-z_])", text) is not None


def parse_group_from_scope(scope_key: str) -> str:
    """从 ``group:{gid}`` 提取群号；非群 scope 返回空串。"""
    return scope_key[len("group:") :].strip() if scope_key.startswith("group:") else ""


def parse_iso_ts(raw: str) -> float:
    """解析 Episode valid_at 字符串为 UTC Unix 时间戳；失败返回 0.0。"""
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[: len(fmt)], fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
        return 0.0
    ts = utc_timestamp(dt)
    return ts if ts is not None else 0.0


def _is_self_or_empty(scope_key: str) -> bool:
    return not scope_key or scope_key.startswith("self:")


def _is_assistant_line(text: str) -> bool:
    s = (text or "").lstrip()
    low = s.lower()
    return low.startswith("assistant:") or s.startswith("[我此前说过]") or s.startswith("__assistant_")


def _is_assistant_episode(content: str, speaker_ids: Sequence[str]) -> bool:
    return any(str(s).startswith("__assistant_") for s in speaker_ids) or _is_assistant_line(content)


def episode_belongs_to_user(speaker_ids: Sequence[str], content: str, user_id: str) -> bool:
    """判断一条 Episode 是否直接涉及目标用户 ``user_id``。"""
    uid = (user_id or "").strip()
    if not uid:
        return False
    if any(str(spk).strip() == uid for spk in speaker_ids):
        return True
    return bool(content) and _contains_exact_uid(content, uid)


def _line_speaker_uid(line: str) -> str:
    m = _LINE_SPEAKER_RE.match(line)
    if m is not None:
        return m.group("uid").strip()
    return "__assistant__" if _is_assistant_line(line) else ""


def _speaker_uids(content: str, speaker_ids: Sequence[str]) -> set[str]:
    """Episode 里出现的全部非助手说话人：元数据 speaker_ids + 正文「名(uid):」标注。"""
    found = {str(s).strip() for s in speaker_ids}
    found |= {m.group("uid").strip() for m in _INLINE_SPEAKER_RE.finditer(content or "")}
    return {s for s in found if s and not s.startswith("__assistant_")}


def excise_group_episode_content_for_user(
    content: str,
    uid: str,
    speaker_ids: Sequence[str] = (),
) -> str | None:
    """私聊读群聊 Episode：只留本人行、紧跟本人的助手行及续行；无法逐行归属的条目整条拒绝。

    正文出现 uid 不算归属：会话摘要等汇总常把多人原话和 uid 拼在同一行。
    """
    if not content or not uid:
        return None
    if _speaker_uids(content, speaker_ids) == {uid}:
        return content
    lines = content.splitlines()
    if not any(_line_speaker_uid(ln) == uid for ln in lines):
        return None

    kept: list[str] = []
    last_kept = False
    last_was_uid = False
    for ln in lines:
        luid = _line_speaker_uid(ln)
        # 行内出现他人「(uid):」说话标注（如单行多段会话摘要）：整行不可归属，拒放
        if {m.group("uid").strip() for m in _INLINE_SPEAKER_RE.finditer(ln)} - {uid}:
            last_kept = False
            last_was_uid = False
            continue
        if not luid:
            if last_kept:
                kept.append(ln)
            continue
        if luid.startswith("__assistant_"):
            last_kept = last_was_uid or _contains_exact_uid(ln, uid)
            if last_kept:
                kept.append(ln)
            last_was_uid = False
        elif luid == uid:
            kept.append(ln)
            last_kept = True
            last_was_uid = True
        else:
            last_kept = False
            last_was_uid = False
    return "\n".join(kept) if kept else None


async def resolve_user_group_scopes(
    user_id: str,
    *,
    bot_id: str = "",
    cap: int = _DM_GROUP_SCOPE_CAP,
) -> tuple[str, ...]:
    """解析用户在私聊中可跨群检索的 ``group:*`` 列表（仅限该用户有交互记录的群）。"""
    uid = (user_id or "").strip()
    if not uid:
        return ()

    found: list[str] = []
    seen: set[str] = set()

    def _add_gid(gid: str) -> None:
        g = gid.strip()
        if not g:
            return
        sk = f"group:{g}"
        if sk not in seen and len(found) < cap:
            seen.add(sk)
            found.append(sk)

    from gsuid_core.message_history import get_history_manager

    hm = get_history_manager()
    for ev in hm.list_sessions():
        if not ev.group_id or (bot_id and ev.bot_id and str(ev.bot_id) != bot_id):
            continue
        if any(str(r.user_id) == uid for r in hm.get_history(ev)):
            _add_gid(str(ev.group_id))

    from gsuid_core.utils.database.base_models import async_maker

    if async_maker is not None:
        from gsuid_core.ai_core.memory.database.models import AIMemEntity

        for sk_raw in await AIMemEntity.get_group_scopes_for_user(uid, cap=cap):
            if gid := parse_group_from_scope(sk_raw):
                _add_gid(gid)
        if len(found) < cap:
            from gsuid_core.utils.database.models import CoreUser

            for gid_raw in await CoreUser.get_group_ids_for_user(uid, bot_id=bot_id, cap=cap):
                _add_gid(gid_raw)
    return tuple(found)


async def load_episode_speaker_map(episode_ids: Sequence[str]) -> dict[str, list[str]]:
    """批量加载 Episode.speaker_ids 供私聊按人过滤复用。"""
    ids = [eid for eid in episode_ids if eid]
    from gsuid_core.utils.database.base_models import async_maker

    if not ids or async_maker is None:
        return {}
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    return await AIMemEpisode.get_speaker_map_by_ids(ids)


async def resolve_user_entity_ids(user_id: str, group_scopes: Sequence[str]) -> frozenset[str]:
    """查询用户在指定群 scope 内对应的全部 ``AIMemEntity.id``（供私聊跨群 Edge 过滤与下推）。"""
    uid = (user_id or "").strip()
    from gsuid_core.utils.database.base_models import async_maker

    if not uid or not group_scopes or async_maker is None:
        return frozenset()
    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    return await AIMemEntity.get_ids_for_user_in_scopes(uid, group_scopes)


def _entity_ts(ent: "Entity", ts_map: dict[str, float]) -> float:
    if "created_at_ts" in ent and isinstance(ent["created_at_ts"], (int, float)):
        return float(ent["created_at_ts"])
    eid = ent["id"] if "id" in ent else ""
    return ts_map[eid] if eid in ts_map else 0.0


def _group_scope_allowed(scope_key: str, allowed_group: str, allowed_uig: str) -> bool:
    """严格白名单：群聊仅放行本群、本人在本群、公共 self:* 或历史空 scope（由水位线把关）。"""
    if not scope_key or scope_key == allowed_group or scope_key.startswith("self:"):
        return True
    return bool(allowed_uig and scope_key == allowed_uig)


def _group_watermark_ok(scope_key: str, ts: float, watermark_ts: float) -> bool:
    return watermark_ts <= 0.0 or not _is_self_or_empty(scope_key) or ts >= watermark_ts


def filter_group_entities_by_watermark(
    entities: Sequence["Entity"],
    *,
    watermark_ts: float,
    created_ts_map: dict[str, float] | None = None,
) -> list["Entity"]:
    """群聊读侧：对 ``self:*`` 及历史无 scope 实体按水位线屏蔽升级前混群旧摘要（零删除）。"""
    if watermark_ts <= 0.0:
        return list(entities)
    ts_map = created_ts_map if created_ts_map is not None else {}
    return [
        ent
        for ent in entities
        if _group_watermark_ok(ent["scope_key"] if "scope_key" in ent else "", _entity_ts(ent, ts_map), watermark_ts)
    ]


def filter_group_episodes_by_watermark(
    episodes: Sequence["Episode"],
    *,
    watermark_ts: float,
) -> list["Episode"]:
    """群聊读侧：对 ``self:*`` 及历史无 scope 条目按水位线屏蔽升级前混群旧记忆（零删除）。"""
    if watermark_ts <= 0.0:
        return list(episodes)
    return [
        ep
        for ep in episodes
        if _group_watermark_ok(
            ep["scope_key"] if "scope_key" in ep else "",
            parse_iso_ts(ep["valid_at"] if "valid_at" in ep else ""),
            watermark_ts,
        )
    ]


def _episode_speakers(ep: "Episode", sp_map: dict[str, list[str]]) -> list[str]:
    inline = ep["speaker_ids"] if "speaker_ids" in ep and isinstance(ep["speaker_ids"], list) else []
    eid = ep["id"] if "id" in ep else ""
    mapped = sp_map[eid] if eid in sp_map else []
    if not inline or not mapped:
        return inline or mapped
    return [*inline, *(s for s in mapped if s not in inline)]


def _is_pure_user_turn(spks: Sequence[str], content: str, uid: str) -> bool:
    """确认 turn_index-1 只有本人发言（混有他人或无法归属的窗口不算，防助手回复错挂）。"""
    return _speaker_uids(content, spks) == {uid}


def collect_dm_user_turns(
    episodes: Sequence["Episode"],
    uid: str,
    sp_map: dict[str, list[str]],
) -> set[tuple[str, str, int]]:
    user_turns: set[tuple[str, str, int]] = set()
    for ep in episodes:
        sk = ep["scope_key"] if "scope_key" in ep else ""
        if not sk.startswith("group:"):
            continue
        spks = _episode_speakers(ep, sp_map)
        content = ep["content"] if "content" in ep else ""
        if _is_pure_user_turn(spks, content, uid):
            sid = ep["session_id"] if "session_id" in ep else ""
            tidx = ep["turn_index"] if "turn_index" in ep else -1
            if sid and isinstance(tidx, int) and tidx >= 0:
                user_turns.add((sk, sid, tidx))
    return user_turns


def filter_dm_episodes_for_user(
    episodes: Sequence["Episode"],
    *,
    user_id: str,
    watermark_ts: float,
    speaker_map: dict[str, list[str]] | None = None,
    precomputed_user_turns: set[tuple[str, str, int]] | None = None,
) -> list["Episode"]:
    """私聊读侧：仅保留本人私聊、纯净新 self 及群聊本人相关片段（多说话人按行裁剪）。"""
    uid = (user_id or "").strip()
    if not uid:
        return []
    user_scope = f"user_global:{uid}"
    sp_map = speaker_map if speaker_map is not None else {}
    user_turns = (
        precomputed_user_turns if precomputed_user_turns is not None else collect_dm_user_turns(episodes, uid, sp_map)
    )

    out: list["Episode"] = []
    for ep in episodes:
        sk = ep["scope_key"] if "scope_key" in ep else ""
        spks = _episode_speakers(ep, sp_map)
        content = ep["content"] if "content" in ep else ""
        if sk == user_scope:
            out.append(ep)
        elif _is_self_or_empty(sk):
            ts = parse_iso_ts(ep["valid_at"] if "valid_at" in ep else "")
            if watermark_ts <= 0.0 or ts >= watermark_ts or episode_belongs_to_user(spks, content, uid):
                out.append(ep)
        elif sk.startswith("group:"):
            excised = excise_group_episode_content_for_user(content, uid, spks)
            if excised is not None:
                out.append({**ep, "content": excised} if excised != content else ep)
                continue
            # 紧跟本人纯发言的助手回复按 turn 结构挂回；正文夹带他人原话（如会话摘要）不放行
            sid = ep["session_id"] if "session_id" in ep else ""
            tidx = ep["turn_index"] if "turn_index" in ep else -1
            if (
                sid
                and isinstance(tidx, int)
                and (sk, sid, tidx - 1) in user_turns
                and _is_assistant_episode(content, spks)
                and not (_speaker_uids(content, spks) - {uid})
            ):
                out.append(ep)
    return out


def filter_dm_edges_for_user(
    edges: Sequence["Edge"],
    *,
    user_id: str,
    user_entity_ids: frozenset[str],
    watermark_ts: float,
) -> list["Edge"]:
    """私聊读侧：群内事实边仅放行连接本人实体的边，绝不泄露同群他人事实。"""
    uid = (user_id or "").strip()
    if not uid:
        return []
    user_scope = f"user_global:{uid}"
    out: list["Edge"] = []
    for edge in edges:
        sk = edge["scope_key"] if "scope_key" in edge else ""
        src = edge["source_id"] if "source_id" in edge else ""
        tgt = edge["target_id"] if "target_id" in edge else ""
        fact = edge["fact"] if "fact" in edge else ""
        linked_self = src in user_entity_ids or tgt in user_entity_ids
        if sk == user_scope or (sk.startswith("group:") and linked_self):
            out.append(edge)
        elif _is_self_or_empty(sk):
            ts_raw = edge["valid_at_ts"] if "valid_at_ts" in edge else None
            ts = float(ts_raw) if isinstance(ts_raw, (int, float)) else 0.0
            if watermark_ts <= 0.0 or ts >= watermark_ts or linked_self or _contains_exact_uid(fact, uid):
                out.append(edge)
    return out


def filter_dm_entities_for_user(
    entities: Sequence["Entity"],
    *,
    user_entity_ids: frozenset[str],
    user_id: str,
    linked_entity_ids: frozenset[str] | None = None,
    watermark_ts: float = 0.0,
    created_ts_map: dict[str, float] | None = None,
) -> list["Entity"]:
    """私聊读侧：放行本人私聊实体、本人实体及与已过滤事实边关联的非人物实体，并按水位线过滤旧 self:*。"""
    uid = (user_id or "").strip()
    user_scope = f"user_global:{uid}" if uid else ""
    ts_map = created_ts_map if created_ts_map is not None else {}
    out: list["Entity"] = []
    for ent in entities:
        eid = ent["id"] if "id" in ent else ""
        sk = ent["scope_key"] if "scope_key" in ent else ""
        etype = (ent["entity_type"] if "entity_type" in ent else "").lower()
        name = ent["name"] if "name" in ent else ""
        if user_scope and sk == user_scope:
            out.append(ent)
            continue
        is_self_owner = eid in user_entity_ids or _contains_exact_uid(name, uid)
        if not is_self_owner and not _group_watermark_ok(sk, _entity_ts(ent, ts_map), watermark_ts):
            continue
        if is_self_owner:
            out.append(ent)
        elif not any(m in etype for m in _PERSON_ENTITY_TYPE_MARKERS) and (
            _is_self_or_empty(sk) or linked_entity_ids is None or (eid and eid in linked_entity_ids)
        ):
            out.append(ent)
    return out


def _filter_dm_events(
    events: Sequence["MemoryEventCue"],
    *,
    uid: str,
    user_entity_ids: frozenset[str],
    allowed_episode_ids: frozenset[str],
    watermark_ts: float,
) -> list["MemoryEventCue"]:
    user_scope = f"user_global:{uid}"
    out: list["MemoryEventCue"] = []
    for cue in events:
        sk = cue["scope_key"] if "scope_key" in cue else ""
        src = cue["source_id"] if "source_id" in cue else ""
        tgt = cue["target_id"] if "target_id" in cue else ""
        ep_id = cue["turn_episode_id"] if "turn_episode_id" in cue else ""
        summary = cue["summary"] if "summary" in cue else ""
        linked = src in user_entity_ids or tgt in user_entity_ids or (bool(ep_id) and ep_id in allowed_episode_ids)
        if sk == user_scope or (sk.startswith("group:") and linked):
            out.append(cue)
        elif _is_self_or_empty(sk):
            stamp = (cue["stated_at"] or cue["event_at"] or "").strip()
            if (
                watermark_ts <= 0.0
                or parse_iso_ts(stamp) >= watermark_ts
                or linked
                or _contains_exact_uid(summary, uid)
            ):
                out.append(cue)
    return out


def prefix_group_tag(scope_key: str, text: str) -> str:
    """私聊跨群展示时给来自 ``group:{gid}`` 的条目自动加上 ``[群:{gid}]`` 标签（仅在渲染层调用）。"""
    gid = parse_group_from_scope(scope_key)
    if not gid:
        return text
    tag = f"[群:{gid}]"
    return text if text.lstrip().startswith(tag) else f"{tag} {text}"


def _record_relates_with_index(
    rec: "MessageRecord",
    uid: str,
    seq_owner: dict[int, str],
    immediate_prev_user_id: str,
) -> bool:
    if str(rec.user_id) == uid:
        return True
    link = rec.link
    if link is not None:
        if uid in link.at_targets:
            return True
        if link.reply_to_seq is not None and link.reply_to_seq in seq_owner:
            if seq_owner[link.reply_to_seq] == uid:
                return True
            if rec.role == "assistant":
                return False
        elif link.reply_to_author == uid:
            return True
        if rec.role == "assistant":
            if link.at_targets and uid not in link.at_targets:
                return False
            if link.reply_to_author and link.reply_to_author not in ("self", uid):
                return False
    return rec.role == "assistant" and immediate_prev_user_id == uid


def filter_records_for_user(records: Sequence["MessageRecord"], user_id: str) -> list["MessageRecord"]:
    """单趟 O(N) 过滤近 12h 群历史，只保留与目标用户直接相关的消息行。"""
    uid = (user_id or "").strip()
    if not uid or not records:
        return []
    seq_owner: dict[int, str] = {}
    immediate_prev_user_id = ""
    out: list["MessageRecord"] = []
    for rec in records:
        if _record_relates_with_index(rec, uid, seq_owner, immediate_prev_user_id):
            out.append(rec)
        if rec.link is not None:
            seq_owner[rec.link.seq] = str(rec.user_id)
        immediate_prev_user_id = str(rec.user_id) if rec.role == "user" else ""
    return out


async def _load_missing_self_entity_ts(entities: Sequence["Entity"], watermark_ts: float) -> dict[str, float] | None:
    if watermark_ts <= 0.0:
        return None
    ids = [
        ent["id"]
        for ent in entities
        if _is_self_or_empty(ent["scope_key"] if "scope_key" in ent else "")
        and "created_at_ts" not in ent
        and "id" in ent
        and ent["id"]
    ]
    from gsuid_core.utils.database.base_models import async_maker

    if not ids or async_maker is None:
        return None
    from gsuid_core.ai_core.memory.database.models import AIMemEntity

    return await AIMemEntity.get_created_ts_map(ids)


async def enforce_memory_privacy(
    mem: "MemoryContext",
    *,
    group_id: str | None,
    user_id: str,
    watermark_ts: float,
    dm_group_scopes_hint: Sequence[str] = (),
    dm_user_entity_ids: frozenset[str] | None = None,
) -> "MemoryContext":
    """统一后置隐私裁决：固定在检索与全部扩展（词法/邻条）完成后收口，不原地改写正文。"""
    gid = (group_id or "").strip()
    uid = (user_id or "").strip()
    if gid:
        allowed_group = f"group:{gid}"
        allowed_uig = f"user_in_group:{uid}@{gid}" if uid else ""

        def _grp_eps(items: Sequence["Episode"]) -> list["Episode"]:
            return [
                ep
                for ep in filter_group_episodes_by_watermark(items, watermark_ts=watermark_ts)
                if _group_scope_allowed(ep["scope_key"] if "scope_key" in ep else "", allowed_group, allowed_uig)
            ]

        mem.episodes = _grp_eps(mem.episodes)
        mem.reserved_episodes = _grp_eps(mem.reserved_episodes)
        mem.order_stages = _grp_eps(mem.order_stages)
        mem.edges = [
            ed
            for ed in mem.edges
            if _group_scope_allowed(ed["scope_key"] if "scope_key" in ed else "", allowed_group, allowed_uig)
            and _group_watermark_ok(
                ed["scope_key"] if "scope_key" in ed else "",
                float(ed["valid_at_ts"])
                if "valid_at_ts" in ed and isinstance(ed["valid_at_ts"], (int, float))
                else 0.0,
                watermark_ts,
            )
        ]
        mem.events = [
            cue
            for cue in mem.events
            if _group_scope_allowed(cue["scope_key"] if "scope_key" in cue else "", allowed_group, allowed_uig)
            and _group_watermark_ok(
                cue["scope_key"] if "scope_key" in cue else "",
                parse_iso_ts((cue["stated_at"] or cue["event_at"] or "").strip()),
                watermark_ts,
            )
        ]
        ent_ts_map = await _load_missing_self_entity_ts(mem.entities, watermark_ts)
        mem.entities = [
            ent
            for ent in filter_group_entities_by_watermark(
                mem.entities, watermark_ts=watermark_ts, created_ts_map=ent_ts_map
            )
            if _group_scope_allowed(ent["scope_key"] if "scope_key" in ent else "", allowed_group, allowed_uig)
        ]
        return mem

    if not uid:
        return mem
    mem.is_private = True
    all_eps = [*mem.episodes, *mem.reserved_episodes, *mem.order_stages]
    ep_ids = [
        ep["id"]
        for ep in all_eps
        if "id" in ep
        and ep["id"]
        and not ("speaker_ids" in ep and isinstance(ep["speaker_ids"], list) and ep["speaker_ids"])
        and not (ep["scope_key"] if "scope_key" in ep else "").startswith("user_global:")
    ]
    sp_map = await load_episode_speaker_map(ep_ids) if ep_ids else {}
    g_scopes = sorted(
        {sk for sk in dm_group_scopes_hint if sk.startswith("group:")}
        | {ep["scope_key"] for ep in all_eps if "scope_key" in ep and ep["scope_key"].startswith("group:")}
        | {ed["scope_key"] for ed in mem.edges if "scope_key" in ed and ed["scope_key"].startswith("group:")}
        | {ev["scope_key"] for ev in mem.events if "scope_key" in ev and ev["scope_key"].startswith("group:")}
    )
    if dm_user_entity_ids is not None and set(g_scopes) <= set(dm_group_scopes_hint):
        user_eids = dm_user_entity_ids
    else:
        user_eids = await resolve_user_entity_ids(uid, g_scopes) if g_scopes else frozenset()

    shared_turns = collect_dm_user_turns(all_eps, uid, sp_map)
    mem.episodes = filter_dm_episodes_for_user(
        mem.episodes, user_id=uid, watermark_ts=watermark_ts, speaker_map=sp_map, precomputed_user_turns=shared_turns
    )
    mem.reserved_episodes = filter_dm_episodes_for_user(
        mem.reserved_episodes,
        user_id=uid,
        watermark_ts=watermark_ts,
        speaker_map=sp_map,
        precomputed_user_turns=shared_turns,
    )
    mem.order_stages = filter_dm_episodes_for_user(
        mem.order_stages,
        user_id=uid,
        watermark_ts=watermark_ts,
        speaker_map=sp_map,
        precomputed_user_turns=shared_turns,
    )
    mem.edges = filter_dm_edges_for_user(mem.edges, user_id=uid, user_entity_ids=user_eids, watermark_ts=watermark_ts)
    allowed_ep_ids = frozenset(
        ep["id"] for ep in (*mem.episodes, *mem.reserved_episodes, *mem.order_stages) if "id" in ep and ep["id"]
    )
    mem.events = _filter_dm_events(
        mem.events,
        uid=uid,
        user_entity_ids=user_eids,
        allowed_episode_ids=allowed_ep_ids,
        watermark_ts=watermark_ts,
    )
    linked_eids = frozenset(
        eid
        for ed in mem.edges
        for eid in (ed["source_id"] if "source_id" in ed else "", ed["target_id"] if "target_id" in ed else "")
        if eid
    )
    dm_ent_ts_map = await _load_missing_self_entity_ts(mem.entities, watermark_ts)
    mem.entities = filter_dm_entities_for_user(
        mem.entities,
        user_entity_ids=user_eids,
        user_id=uid,
        linked_entity_ids=linked_eids,
        watermark_ts=watermark_ts,
        created_ts_map=dm_ent_ts_map,
    )
    return mem
