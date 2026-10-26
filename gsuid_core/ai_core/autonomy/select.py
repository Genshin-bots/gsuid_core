"""观察和交付共用的纯规则。没有数据库，也没有模型。"""

from collections.abc import Sequence

from gsuid_core.ai_core.autonomy.models import (
    SpokenLine,
    DeliveryPin,
    ObservationDigest,
    CareTargetSnapshot,
)
from gsuid_core.ai_core.relationship.zones import Zone, zone_of, is_at_least, zone_level_name

CARE_COOLDOWN_SECONDS = 12 * 3600
PRIVATE_LOOKBACK_SECONDS = 24 * 3600
GROUP_LOOKBACK_SECONDS = 24 * 3600
BUSY_SECONDS = 60
ASSISTANT_RECENT_SECONDS = 10 * 60
GROUP_FRESH_SECONDS = 30 * 60
CARE_CAP = 10
GROUP_CAP = 5

_FAVOR_ROW = tuple[str, int]


def care_floor(zone_name: str) -> Zone:
    """配置只允许 familiar / close。别的写法收成更严的亲密。"""
    if zone_name == "familiar":
        return Zone.FAMILIAR
    return Zone.CLOSE


def minimum_score_for(floor: Zone) -> int:
    """用温区分档反查下界，避免在这里再写一套分数。"""
    for score in range(-100, 101):
        if is_at_least(zone_of(score), floor):
            return score
    return 80


def eligible_user_ids(
    favors: Sequence[_FAVOR_ROW],
    master_ids: Sequence[str],
    floor: Zone,
    served: set[str],
) -> list[str]:
    """主人始终留下。其他人必须达到温区，且 12 小时内没被关心过。"""
    best: dict[str, int] = {}
    for user_id, score in favors:
        uid = user_id.strip()
        if not uid:
            continue
        previous = best[uid] if uid in best else None
        if previous is None or score > previous:
            best[uid] = score
    masters = {item.strip() for item in master_ids if item.strip()}
    found: list[str] = []
    for uid, score in best.items():
        if uid in served:
            continue
        if uid in masters or is_at_least(zone_of(score), floor):
            found.append(uid)
    for uid in masters:
        if uid not in served and uid not in found:
            found.append(uid)
    return found


def preferred_line(
    lines: Sequence[SpokenLine],
    user_id: str,
    *,
    now: float,
    allowed_groups: set[str] | None,
    allow_private: bool,
) -> SpokenLine | None:
    """24 小时内的私聊优先，否则用最后一条仍在范围内的群消息。"""
    mine = [line for line in lines if line.role == "user" and line.user_id == user_id]
    private = [
        line
        for line in mine
        if allow_private and not line.group_id and now - line.ts <= PRIVATE_LOOKBACK_SECONDS and now >= line.ts
    ]
    if private:
        return max(private, key=lambda line: line.ts)
    groups = [
        line
        for line in mine
        if line.group_id and (allowed_groups is None or line.group_id in allowed_groups) and now >= line.ts
    ]
    if not groups:
        return None
    return max(groups, key=lambda line: line.ts)


def assemble_care_targets(
    user_ids: Sequence[str],
    scores: dict[str, int],
    masters: set[str],
    lines: Sequence[SpokenLine],
    binds: dict[str, dict[str, str]],
    names: dict[str, str],
    *,
    now: float,
    allowed_groups: set[str] | None,
    allow_private: bool,
) -> list[CareTargetSnapshot]:
    """有发言或有绑定的排前面。没有痕迹的仍保留，但排在后面，最多 10 人。"""

    def rank(uid: str) -> tuple[int, int, str]:
        talked = any(line.role == "user" and line.user_id == uid and line.text.strip() for line in lines)
        bound = bool(binds[uid]) if uid in binds else False
        signal = 0 if talked or bound else 1
        return (signal, -scores[uid] if uid in scores else 0, uid)

    ordered = sorted(user_ids, key=rank)[:CARE_CAP]
    targets: list[CareTargetSnapshot] = []
    for uid in ordered:
        score = scores[uid] if uid in scores else 0
        zone = zone_of(score)
        chosen = preferred_line(
            lines,
            uid,
            now=now,
            allowed_groups=allowed_groups,
            allow_private=allow_private,
        )
        group_id = chosen.group_id if chosen is not None else None
        recent = _recent_texts(lines, uid, group_id=group_id, now=now, limit=6)
        bot_id = chosen.bot_id if chosen is not None else ""
        bot_self_id = chosen.bot_self_id if chosen is not None else ""
        ws_bot_id = chosen.ws_bot_id if chosen is not None else ""
        last_ts = chosen.ts if chosen is not None else 0.0
        last_ai_ts = _latest_assistant_ts(lines, group_id=group_id, user_id=uid)
        bound = dict(binds[uid]) if uid in binds else {}
        targets.append(
            CareTargetSnapshot(
                user_id=uid,
                user_name=names[uid] if uid in names and names[uid] else uid,
                score=score,
                zone_value=zone.value,
                is_master=uid in masters,
                bound_uids=bound,
                recent_lines=recent,
                group_id=group_id,
                bot_id=bot_id,
                bot_self_id=bot_self_id,
                ws_bot_id=ws_bot_id,
                session_key=_session_key(group_id, uid),
                last_human_ts=last_ts,
                last_assistant_ts=last_ai_ts,
            )
        )
    return targets


def digest_is_empty(digest: ObservationDigest) -> bool:
    return not (digest.care_targets or digest.group_digests or digest.open_trails)


def render_digest(digest: ObservationDigest, *, now: float) -> str:
    """只描述观察底片。不写角色口癖，也不写具体业务名词。"""
    lines: list[str] = [
        "下面是你空闲时能看到的底片。先决定本轮动力，再调用已经挂上的工具。",
        "文末必须单独四行，且各占一行：",
        "DRIVE: care_user 或 group_topic 或 self_explore 或 rest",
        "TARGET: 关心对象的用户 ID，或群 ID；休息时留空",
        "SUMMARY: 一句话，只写工具真正返回的内容",
        "TRAILS: 用 | 分开的未完线索，没有就留空",
        "发消息调用 send_message_by_ai 并指定底片内的群号或用户 ID。run_bot_command 只能传观察名单里的用户 ID。",
        "find_tools 这一轮不会装上新工具。查网页用已挂的搜索和抓取，看图用已挂的读图。",
        "已经带「[你]」回复过、或已在后台任务里推进的事项，不要重复查。",
        f"当前情绪强度约 {digest.mood_score:.2f}。",
        "想自己查什么，以你的人格自述为准。",
    ]
    if digest.in_flight_tasks:
        lines.append("正在推进的后台任务（请勿重复处理）：")
        for task_line in digest.in_flight_tasks[:8]:
            lines.append(f"- {task_line}")
    if digest.care_targets:
        lines.append("关心对象：")
        for target in digest.care_targets:
            zone = _zone_label(target.zone_value)
            role = "主人" if target.is_master else zone
            binds = target.bound_uids if target.bound_uids else {}
            bind_text = "，".join(f"{name}={uid}" for name, uid in binds.items()) or "无"
            lines.append(f"- {target.user_name}（ID {target.user_id}，{role}，好感 {target.score}，绑定 {bind_text}）")
            for text in target.recent_lines:
                lines.append(f"  {text}")
    else:
        lines.append("关心对象：无")
    if digest.group_digests:
        lines.append("群近况：")
        for group in digest.group_digests:
            age = _age_label(group.last_human_ts, now)
            lines.append(f"- 群 {group.group_id}，最后有人说话是{age}")
            if group.summary:
                lines.append(f"  摘要：{group.summary[:400]}")
            for text in group.recent_lines[:10]:
                lines.append(f"  {text}")
    else:
        lines.append("群近况：无")
    if digest.open_trails:
        lines.append("上次没做完：" + "、".join(digest.open_trails[:8]))
    if digest.recently_used_tools:
        lines.append("最近用过的工具：" + "、".join(digest.recently_used_tools[:8]))
    if digest.available_plugins_summary:
        lines.append("在线插件：" + "、".join(digest.available_plugins_summary[:30]))
    return "\n".join(lines)


def pins_from_digest(digest: ObservationDigest) -> tuple[list[DeliveryPin], list[DeliveryPin]]:
    care = [
        DeliveryPin(
            user_id=item.user_id,
            group_id=item.group_id,
            is_master=item.is_master,
            zone_value=item.zone_value,
            bot_id=item.bot_id,
            bot_self_id=item.bot_self_id,
            ws_bot_id=item.ws_bot_id,
            session_key=item.session_key,
            last_human_ts=item.last_human_ts,
            last_assistant_ts=item.last_assistant_ts,
        )
        for item in digest.care_targets
    ]
    groups = [
        DeliveryPin(
            user_id="",
            group_id=item.group_id,
            is_master=False,
            zone_value="",
            bot_id=item.bot_id,
            bot_self_id=item.bot_self_id,
            ws_bot_id=item.ws_bot_id,
            session_key=item.session_key,
            last_human_ts=item.last_human_ts,
            last_assistant_ts=item.last_assistant_ts,
        )
        for item in digest.group_digests
    ]
    return care, groups


def _recent_texts(
    lines: Sequence[SpokenLine],
    user_id: str,
    *,
    group_id: str | None,
    now: float,
    limit: int,
) -> list[str]:
    found: list[str] = []
    for line in sorted(lines, key=lambda item: item.ts, reverse=True):
        if line.role == "user":
            if line.user_id != user_id:
                continue
            prefix = ""
        else:
            if group_id is not None:
                if line.group_id != group_id:
                    continue
            elif line.group_id is not None or line.user_id != user_id:
                continue
            prefix = "[你] "
        text = line.text.strip()
        if not text:
            continue
        found.append(f"{_age_label(line.ts, now)} {prefix}{text[:80]}")
        if len(found) >= limit:
            break
    return found


def _latest_assistant_ts(lines: Sequence[SpokenLine], *, group_id: str | None, user_id: str) -> float:
    matched = [
        line.ts
        for line in lines
        if line.role == "assistant"
        and ((group_id is not None and line.group_id == group_id) or (group_id is None and line.user_id == user_id))
    ]
    return max(matched) if matched else 0.0


def _age_label(ts: float, now: float) -> str:
    delta = max(0, int(now - ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60}分钟前"
    if delta < 86400:
        return f"{delta // 3600}小时前"
    return f"{delta // 86400}天前"


def _session_key(group_id: str | None, user_id: str) -> str:
    if group_id:
        return f"group:{group_id}"
    return f"private:{user_id}"


def _zone_label(zone_value: str) -> str:
    if zone_value in {item.value for item in Zone}:
        return zone_level_name(Zone(zone_value))
    return zone_value
