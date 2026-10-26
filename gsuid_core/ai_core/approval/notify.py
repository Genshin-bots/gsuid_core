"""审批私聊。只发给主人名单里、且订阅了「主人用户」的人。"""

import time

from gsuid_core.i18n import t
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.subscribe import gs_subscribe

from .halt import DEFAULT_APPROVAL_TTL
from .models import AIApprovalRequest

WAITING_GROUP_TEXT = "在等你私聊确认。"


def _window_phrase(seconds: int) -> str:
    left = seconds if seconds > 0 else DEFAULT_APPROVAL_TTL
    if left % 60 == 0:
        return f"{left // 60} 分钟"
    if left >= 60:
        return f"{(left + 30) // 60} 分钟"
    return f"{left} 秒"


def format_confirm_dm(title: str, effect: str, code: str, short_id: str, ttl_seconds: int) -> str:
    shown = effect.strip() or "（调用方没有写批准后会发生什么）"
    window = _window_phrase(ttl_seconds)
    return (
        f"需要你确认一件事，{window}内有效。\n"
        "\n"
        f"事项：{title}\n"
        f"批准后会发生：{shown}\n"
        "\n"
        f"只回复这 6 位确认码：{code}\n"
        f"或打开网页控制台 → 审批中心 → 找到 #{short_id} → 批准 / 拒绝。\n"
        "\n"
        "超时未确认则此事取消，不会自动重做。"
    )


def _payload_str(raw: str, key: str) -> str:
    import json

    data = json.loads(raw or "{}")
    if not isinstance(data, dict) or key not in data:
        return ""
    val = data[key]
    if isinstance(val, str):
        return val
    return ""


async def _dm_via_subscribe(text: str) -> int:
    """发给主人名单与「主人用户」订阅的交集。重复订阅只发一次，不改订阅表。"""
    masters = core_config.get_config("masters")
    if not masters:
        return 0
    master_ids = {str(item) for item in masters}
    datas = await gs_subscribe.get_subscribe("主人用户")
    if not datas:
        return 0
    seen: set[tuple[str, str]] = set()
    sent = 0
    for subscribe in datas:
        if str(subscribe.user_id) not in master_ids:
            continue
        key = (subscribe.user_id, subscribe.bot_id)
        if key in seen:
            continue
        seen.add(key)
        await subscribe.send(text, force_direct=True)
        sent += 1
    return sent


async def _dm_user(bot_id: str, bot_self_id: str, user_id: str, ws_bot_id: str, text: str) -> bool:
    from gsuid_core.gss import gss

    if not user_id:
        return False
    bot = gss.active_bot[ws_bot_id] if ws_bot_id in gss.active_bot else None
    if bot is None and bot_id in gss.active_bot:
        bot = gss.active_bot[bot_id]
    if bot is None:
        return False
    await bot.target_send(text, "direct", user_id, bot_id or ws_bot_id, bot_self_id or "")
    return True


async def send_confirm_dm(row: AIApprovalRequest, code: str) -> bool:
    """把确认码私聊给裁决人。日志只记短号，不记正文。"""
    left = max(1, int(row.expires_at) - int(time.time())) if row.expires_at else DEFAULT_APPROVAL_TTL
    text = format_confirm_dm(row.title, row.effect, code, row.short_id, left)
    if row.audience == "user":
        ok = await _dm_user(
            row.bot_id,
            row.bot_self_id,
            row.operator_user_id,
            _payload_str(row.payload_json, "ws_bot_id"),
            text,
        )
    else:
        ok = (await _dm_via_subscribe(text)) > 0
    if ok:
        logger.info(t("log.ai.approval_confirm_dm_sent", p0=row.short_id))
    else:
        logger.info(t("log.ai.approval_confirm_dm_missing", p0=row.short_id))
    return ok


async def notify_parent_group(row: AIApprovalRequest, text: str) -> None:
    """群里一句固定事实。私聊会话不重复发。"""
    if row.user_type not in ("group", "channel", "sub_channel"):
        return
    target = row.group_id or ""
    if not target:
        return
    if row.user_type == "group":
        target_type = "group"
    elif row.user_type == "channel":
        target_type = "channel"
    elif row.user_type == "sub_channel":
        target_type = "sub_channel"
    else:
        return
    from gsuid_core.gss import gss

    ws_bot_id = _payload_str(row.payload_json, "ws_bot_id")
    bot = gss.active_bot[ws_bot_id] if ws_bot_id in gss.active_bot else None
    if bot is None and row.bot_id in gss.active_bot:
        bot = gss.active_bot[row.bot_id]
    if bot is None:
        return
    await bot.target_send(
        text,
        target_type,
        target,
        row.bot_id,
        row.bot_self_id or "",
        group_id=row.group_id,
    )


async def notify_closed(row: AIApprovalRequest, *, expired: bool) -> None:
    """拒绝或超时：私聊和群里同一句事实，不回灌。"""
    text = f"#{row.short_id} 已超时，未执行。" if expired else f"#{row.short_id} 已拒绝，未执行。"
    if row.audience == "user":
        await _dm_user(
            row.bot_id,
            row.bot_self_id,
            row.operator_user_id,
            _payload_str(row.payload_json, "ws_bot_id"),
            text,
        )
    else:
        await _dm_via_subscribe(text)
    await notify_parent_group(row, text)
    logger.info(t("log.ai.approval_expired_stop", p0=row.short_id, status="expired" if expired else "rejected"))


async def ack_direct(bot_id: str, bot_self_id: str, user_id: str, ws_bot_id: str, text: str) -> None:
    await _dm_user(bot_id, bot_self_id, user_id, ws_bot_id, text)
