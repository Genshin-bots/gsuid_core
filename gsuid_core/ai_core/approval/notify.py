"""审批私聊通知。

用户级私聊发给发起用户本人；主人级私聊发给 masters 名单中的主人。
审批挂起后在群聊保持静默，不向群聊追问或催促。
"""

import time

from gsuid_core.i18n import t
from gsuid_core.config import core_config
from gsuid_core.logger import logger

from .halt import DEFAULT_APPROVAL_TTL
from .models import AIApprovalRequest

WAITING_GROUP_TEXT = ""


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


async def _dm_user(bot_id: str, bot_self_id: str, user_id: str, ws_bot_id: str, text: str) -> bool:
    from gsuid_core.gss import gss

    if not user_id:
        return False
    bot = gss.active_bot[ws_bot_id] if ws_bot_id in gss.active_bot else None
    if bot is None and bot_id in gss.active_bot:
        bot = gss.active_bot[bot_id]
    if bot is None and gss.active_bot:
        bot = next(iter(gss.active_bot.values()))
    if bot is None:
        return False
    await bot.target_send(text, "direct", user_id, bot_id or bot.bot_id, bot_self_id or "")
    return True


async def _dm_master(row: AIApprovalRequest, text: str) -> bool:
    """主人级审批：直接私聊发给 masters 名单中的主人，不依赖订阅表。"""
    masters = core_config.get_config("masters")
    if not masters:
        return False
    from gsuid_core.gss import gss

    ws_bot_id = _payload_str(row.payload_json, "ws_bot_id")
    bot = gss.active_bot.get(ws_bot_id) if ws_bot_id else None
    if bot is None and row.bot_id in gss.active_bot:
        bot = gss.active_bot[row.bot_id]
    if bot is None and gss.active_bot:
        bot = next(iter(gss.active_bot.values()))
    if bot is None:
        return False
    bot_id = row.bot_id or bot.bot_id
    bot_self_id = row.bot_self_id or ""
    sent = 0
    for master_id in masters:
        uid = str(master_id).strip()
        if not uid:
            continue
        try:
            await bot.target_send(text, "direct", uid, bot_id, bot_self_id)
            sent += 1
        except Exception:
            logger.warning(t("log.ai.approval_confirm_dm_missing", p0=row.short_id))
    return sent > 0


async def send_confirm_dm(row: AIApprovalRequest, code: str) -> bool:
    """把确认码私聊给裁决人（用户级发给发起人，主人级发给主人）。"""
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
        ok = await _dm_master(row, text)
    if ok:
        logger.info(t("log.ai.approval_confirm_dm_sent", p0=row.short_id))
    else:
        logger.info(t("log.ai.approval_confirm_dm_missing", p0=row.short_id))
    return ok


async def notify_parent_group(row: AIApprovalRequest, text: str) -> None:
    """群里一句固定事实。若 text 为空则不发送，避免打扰群聊。"""
    if not text or not text.strip():
        return
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
    """拒绝或超时：私聊告知裁决人，不发群。"""
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
        await _dm_master(row, text)
    logger.info(t("log.ai.approval_expired_stop", p0=row.short_id, status="expired" if expired else "rejected"))


async def ack_direct(bot_id: str, bot_self_id: str, user_id: str, ws_bot_id: str, text: str) -> None:
    await _dm_user(bot_id, bot_self_id, user_id, ws_bot_id, text)
