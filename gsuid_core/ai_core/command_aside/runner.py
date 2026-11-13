"""命令协程返回后：先把回执写入当前会话，再按概率点评。"""

from __future__ import annotations

import time
import random
from typing import Literal
from pathlib import Path
from collections.abc import Awaitable

from gsuid_core.sv import SL
from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.trigger import Trigger
from gsuid_core.ai_core.startup import is_ai_core_ready
from gsuid_core.message_history import get_history_manager
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
from gsuid_core.ai_core.ai_router import get_ai_session
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.persona.config import ASIDE_MODE, normalize_ai_modes, persona_config_manager
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.buildin_plugins.core_command.core_ai_control.state import is_scope_banned

from .recall import lookup_advice
from .capture import SentPiece, end_capture, reply_shape, begin_capture, note_outbound
from .plugin_read import plugin_anchor

ReplyShape = Literal["image", "text", "mixed"]

_last_aside_comment_ts: dict[str, float] = {}
_ASIDE_COOLDOWN_SECONDS = 120.0

_FAILURE_USAGE_MARKERS = (
    "失败",
    "错误",
    "暂无",
    "不存在",
    "未找到",
    "找不到",
    "重试",
    "请发送",
    "请使用",
    "可发送",
    "用法",
    "格式错误",
    "参数错误",
    "未绑定",
    "绑定后",
    "登录",
    "登陆",
    "未开启",
    "缺少",
    "指令错误",
    "无效",
    "异常",
    "超时",
    "拒绝",
    "请先",
    "参考命令",
)
_FAILURE_USAGE_MARKERS_EN = (
    "not found",
    "failed",
    "invalid",
    "usage",
    "timed out",
    "exception",
)


def _is_failure_or_usage_receipt(receipt: str) -> bool:
    text = receipt.strip()
    if any(marker in text for marker in _FAILURE_USAGE_MARKERS):
        return True
    folded = text.casefold()
    return any(marker in folded for marker in _FAILURE_USAGE_MARKERS_EN)


def _all_images_omitted(pieces: list[SentPiece]) -> bool:
    images = [p for p in pieces if p.kind == "image"]
    return bool(images) and all(p.omitted for p in images)


class AsideSettings:
    def __init__(
        self,
        image_probability: float,
        text_probability: float,
        mixed_probability: float,
        allow_tools: bool,
        cooldown_seconds: float = 0.0,
    ) -> None:
        self.image_probability = image_probability
        self.text_probability = text_probability
        self.mixed_probability = mixed_probability
        self.allow_tools = allow_tools
        self.cooldown_seconds = cooldown_seconds

    def probability(self, shape: ReplyShape) -> float:
        if shape == "image":
            return self.image_probability
        if shape == "text":
            return self.text_probability
        return self.mixed_probability


def comment_this_time(shape: ReplyShape, settings: AsideSettings, roll: float) -> bool:
    probability = settings.probability(shape)
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    return roll < probability


def load_aside_settings(persona_name: str) -> AsideSettings | None:
    config = persona_config_manager.get_config(persona_name)
    modes = normalize_ai_modes(list(config.get_config("ai_mode").data))
    if ASIDE_MODE not in modes:
        return None
    cooldown = _ASIDE_COOLDOWN_SECONDS
    if "aside_cooldown_seconds" in config.config:
        cooldown = float(config.get_config("aside_cooldown_seconds").data)
    return AsideSettings(
        image_probability=float(config.get_config("aside_image_probability").data),
        text_probability=float(config.get_config("aside_text_probability").data),
        mixed_probability=float(config.get_config("aside_mixed_probability").data),
        allow_tools=bool(config.get_config("aside_allow_tools").data),
        cooldown_seconds=cooldown,
    )


def raw_receipt_text(pieces: list[SentPiece]) -> str:
    return "\n".join(piece.text.strip() for piece in pieces if piece.kind == "text" and piece.text.strip())


def _get_plugin_commands(plugin_name: str) -> set[str]:
    if not plugin_name:
        return set()
    prefixes: list[str] = []
    cfg_prefixes = core_config.get_config("command_start")
    if isinstance(cfg_prefixes, list):
        prefixes = [str(p) for p in cfg_prefixes if p]
    if not prefixes:
        prefixes = [""]

    commands: set[str] = set()
    for sv in SL.lst.values():
        if sv.self_plugin_name != plugin_name:
            continue
        for type_dict in sv.TL.values():
            for trig in type_dict.values():
                kw = str(trig.keyword)
                commands.add(kw)
                for p in prefixes:
                    commands.add(f"{p}{kw}")
    return commands


def _note_session(session: GsCoreAIAgent | None, content: str, source: str) -> None:
    if isinstance(session, GsCoreAIAgent):
        session._session_logger.log_system_injection(content, source=source)


async def apply_aside(
    session: GsCoreAIAgent | None,
    bot: Bot | None,
    event: Event,
    pieces: list[SentPiece],
    settings: AsideSettings,
    plugin_root: Path | None,
    plugin_label: str,
    roll: float,
    persona_name: str,
    entry_rel: str = "",
) -> bool:
    """写入回执。抽中且未在冷却期时把回执作为 soft 唤醒项提交收件箱。"""
    shape = reply_shape(pieces)
    if shape == "empty":
        return False
    receipt = raw_receipt_text(pieces)
    plugin = plugin_root.name if plugin_root is not None else ""

    now = time.time()
    last_comment_ts = _last_aside_comment_ts[event.session_id] if event.session_id in _last_aside_comment_ts else 0.0
    in_cooldown = settings.cooldown_seconds > 0 and (now - last_comment_ts) < settings.cooldown_seconds
    all_omitted = shape == "image" and _all_images_omitted(pieces)

    commented = False
    if not in_cooldown and not all_omitted and comment_this_time(shape, settings, roll):
        memory = lookup_advice(plugin, receipt, trigger_id=entry_rel, persona_name=persona_name) if plugin else ""
        if memory:
            _note_session(session, memory, "aside_memory")
            logger.info(t("log.ai.command_aside_memory_hit", session_id=event.session_id, plugin=plugin))
        # 回执唤醒项：seq 指向本次命令在 A 轨最后一条回执出站记录。群聊里
        # 并发出站会交错，用收件人 uid 收窄，避免锚到别人那轮的发送上。
        records = get_history_manager().get_history(event)
        receipt_seq = -1
        expected_uid = str(event.user_id or "")
        for rec in reversed(records):
            if rec.role != "assistant" or rec.link is None:
                continue
            if event.user_type != "direct" and (
                "reply_to_user_id" not in rec.metadata or str(rec.metadata["reply_to_user_id"]) != expected_uid
            ):
                continue
            receipt_seq = rec.link.seq
            break
        if receipt_seq >= 0 and bot is not None:
            from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry, ReceiptContext
            from gsuid_core.ai_core.inbox.scheduler import get_inbox_scheduler

            get_inbox_scheduler().submit(
                bot.bot,
                PendingEntry(
                    item=WakeItem(
                        seq=receipt_seq,
                        user_id=str(event.user_id or ""),
                        kind="receipt",
                        strength="soft",
                        created_at=now,
                    ),
                    event=event,
                    receipt=ReceiptContext(
                        plugin_root=plugin_root,
                        plugin_label=plugin_label,
                        entry_rel=entry_rel,
                        receipt_text=receipt,
                        allow_tools=settings.allow_tools,
                        advice_memory=memory,
                    ),
                    persona_name=persona_name,
                ),
            )
            commented = True

    omitted_count = sum(1 for p in pieces if p.kind == "image" and p.omitted)
    logger.info(
        t(
            "log.ai.command_aside_recorded",
            session_id=event.session_id,
            shape=shape,
            commented="yes" if commented else "no",
            plugin=plugin or "-",
            entry=entry_rel or "-",
            silence="yes" if not commented else "no",
            omitted_images=omitted_count,
        )
    )
    return commented


async def command_with_aside(
    coro: Awaitable[object],
    bot: Bot,
    event: Event,
    trigger: Trigger,
) -> None:
    token = begin_capture()
    command_error: Exception | None = None
    try:
        await coro
    except Exception as exc:
        command_error = exc
        note_outbound(f"命令执行失败：{type(exc).__name__}: {exc}")
    finally:
        pieces = end_capture(token)
    await _settle(bot, event, trigger, pieces)
    if command_error is not None:
        raise command_error


async def _settle(bot: Bot, event: Event, trigger: Trigger, pieces: list[SentPiece]) -> None:
    if reply_shape(pieces) == "empty":
        return
    if not trigger.aside:
        return
    if not bool(ai_config.get_config("enable").data):
        return
    if is_scope_banned(event.session_id):
        return
    if not _list_allows(event):
        return
    persona_name = persona_config_manager.get_persona_for_session(event.session_id)
    if persona_name is None:
        return
    persona_cfg = persona_config_manager.get_config(persona_name)
    raw_skips = (
        persona_cfg.get_config("aside_skip_plugins").data
        if "aside_skip_plugins" in persona_cfg.config
        else ["core_command"]
    )
    skip_plugins = [str(x) for x in raw_skips] if isinstance(raw_skips, list) else ["core_command"]

    anchor = plugin_anchor(trigger.func)
    plugin_root = anchor[0] if anchor is not None else None
    if plugin_root is not None and plugin_root.name in skip_plugins:
        return

    settings = load_aside_settings(persona_name)
    if settings is None:
        return
    if not is_ai_core_ready():
        logger.info(t("log.ai.command_aside_skip", reason="not_ready", session_id=event.session_id))
        return
    plugin_label = ""
    if anchor is not None:
        plugin_label = f"插件 {anchor[0].name}，入口文件 {anchor[1]}"
    session = await get_ai_session(event)
    ran = await apply_aside(
        session,
        bot,
        event,
        pieces,
        settings,
        plugin_root,
        plugin_label,
        random.random(),
        persona_name,
        anchor[1] if anchor is not None else "",
    )
    if ran:
        statistics_manager.record_trigger("aside")


def _list_allows(event: Event) -> bool:
    black = [item for item in ai_config.get_config("black_list").data if item]
    white = [item for item in ai_config.get_config("white_list").data if item]
    user_blocked = event.user_id in black
    group_blocked = event.group_id is not None and event.group_id in black
    if black and (user_blocked or group_blocked):
        return False
    user_ok = event.user_id in white
    group_ok = event.group_id is not None and event.group_id in white
    if white and not (user_ok or group_ok):
        return False
    return True
