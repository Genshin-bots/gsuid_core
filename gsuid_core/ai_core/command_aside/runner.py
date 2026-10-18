"""命令协程返回后：先把回执写入当前会话，再按概率点评。"""

from __future__ import annotations

import re
import random
from typing import Literal, Protocol, Sequence
from pathlib import Path
from collections.abc import Awaitable

from pydantic_ai.tools import Tool
from pydantic_ai.messages import UserContent

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.trigger import Trigger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.persona.config import ASIDE_MODE, normalize_ai_modes, persona_config_manager
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.prompt_group.store import render_prompt

from .policy import bind_tool_policy, restore_tool_policy
from .recall import save_advice, lookup_advice
from .capture import SentPiece, end_capture, reply_shape, begin_capture, note_outbound
from .receipt import receipt_parts
from .plugin_read import plugin_anchor, make_plugin_source_tool
from .source_lookup import command_stems, find_receipt_excerpts

ReplyShape = Literal["image", "text", "mixed"]


class AsideSettings:
    def __init__(
        self,
        image_probability: float,
        text_probability: float,
        mixed_probability: float,
        allow_tools: bool,
    ) -> None:
        self.image_probability = image_probability
        self.text_probability = text_probability
        self.mixed_probability = mixed_probability
        self.allow_tools = allow_tools

    def probability(self, shape: ReplyShape) -> float:
        if shape == "image":
            return self.image_probability
        if shape == "text":
            return self.text_probability
        return self.mixed_probability


class AsideSession(Protocol):
    async def locked_append_then_run(
        self,
        user_content: str | Sequence[UserContent],
        assistant_text: str,
        followup: str | Sequence[UserContent] | None,
        bot: Bot | None,
        ev: Event | None,
        *,
        is_framework_injection: bool,
        budget_gate: bool,
        stats_chat_type: str,
    ) -> str | None: ...


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
    return AsideSettings(
        image_probability=float(config.get_config("aside_image_probability").data),
        text_probability=float(config.get_config("aside_text_probability").data),
        mixed_probability=float(config.get_config("aside_mixed_probability").data),
        allow_tools=bool(config.get_config("aside_allow_tools").data),
    )


def raw_receipt_text(pieces: list[SentPiece]) -> str:
    return "\n".join(piece.text.strip() for piece in pieces if piece.kind == "text" and piece.text.strip())


def receipt_commands(receipt: str, excerpts: list[tuple[str, str]] | None = None) -> str:
    """回执方括号里的命令，以及摘录里紧接着的另一步。都嵌进同一句。"""
    found: list[str] = []
    for item in re.findall(r"\[([^\[\]\n]{2,80})\]", receipt):
        if item not in found:
            found.append(item)
    extras = _excerpt_steps(excerpts or [], found)
    if not found and not extras:
        return ""
    lines: list[str] = []
    if found:
        lines.append(f"回执里的命令原文：{'、'.join(found[:3])}。原样嵌进你这一句，前缀不要改、不要漏。")
    if extras:
        lines.append(f"摘录里紧接着还有：{'、'.join(extras)}。也嵌进同一句。")
    lines.append("不要另起一段写用法。")
    return "\n".join(lines)


def _excerpt_steps(excerpts: list[tuple[str, str]], known: list[str]) -> list[str]:
    stems = command_stems("[" + "][".join(known) + "]") if known else []
    extras: list[str] = []
    for _, excerpt in excerpts:
        for raw in re.findall(r"[【\[]([^】\]\n]{2,30})[】\]]", excerpt):
            command = raw.strip()
            if any(ch in command for ch in "[]【】"):
                continue
            if command in {"或者", "或", "以及"} or len(command) < 2:
                continue
            if not any("\u4e00" <= ch <= "\u9fff" for ch in command):
                continue
            if any(stem in command or command in stem for stem in stems):
                continue
            if any(command in item or item in command for item in known):
                continue
            if command not in extras:
                extras.append(command)
    return extras[:3]


def _comment_line(
    persona_name: str,
    *,
    plugin_label: str,
    source_section: str,
    memory: str,
    allow_tools: bool,
    mount_reader: bool,
    command_spell: str,
) -> str:
    if memory:
        tool_clause = "已有记忆。禁止再读源码。记忆里出现过的命令原文都留在这一句里，句子用你自己的人设写。"
        memory_section = f"已有记忆：{memory}"
    elif allow_tools and mount_reader:
        tool_clause = (
            "还没有这条回执的记忆。上面的源码摘录就是刚读到的。"
            "用你自己的口吻说下一句，对方要能照做。需要再看别的文件时用 read_command_plugin_file。"
            "有摘录或能读源码时，禁止只输出 <SILENCE>。"
        )
        memory_section = ""
    elif allow_tools:
        tool_clause = "本轮可以使用工具。回执像失败或用法说明时，先查清，再用你自己的口吻说下一句。"
        memory_section = ""
    else:
        tool_clause = "本轮不能使用工具。不要假装查过数据或读过源码。"
        memory_section = ""
    where = f"这条命令来自{plugin_label}。" if plugin_label else ""
    body = render_prompt(
        persona_name,
        "aside_comment",
        plugin_label=where,
        source_section=source_section,
        memory_section=memory_section,
        tool_clause=tool_clause,
    ).strip()
    if command_spell:
        body = f"{body}\n{command_spell}"
    if body.startswith("（系统："):
        return body if body.endswith("）") else f"{body}）"
    return f"（系统：{body}）"


def _note_session(session: AsideSession, content: str, source: str) -> None:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    if isinstance(session, GsCoreAIAgent):
        session._session_logger.log_system_injection(content, source=source)


async def apply_aside(
    session: AsideSession,
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
    """写入回执。抽中时再跑一轮点评。返回是否发起了点评。"""
    shape = reply_shape(pieces)
    if shape == "empty":
        return False
    user_content, assistant_text = receipt_parts(event.raw_text or event.text, pieces, plugin_label)
    receipt = raw_receipt_text(pieces)
    plugin = plugin_root.name if plugin_root is not None else ""
    followup: str | None = None
    extra: tuple[Tool[ToolContext], ...] = ()
    memory = ""
    excerpts: list[tuple[str, str]] = []
    if comment_this_time(shape, settings, roll):
        memory = lookup_advice(plugin, receipt) if plugin else ""
        mount_reader = settings.allow_tools and plugin_root is not None and not memory
        if memory:
            _note_session(session, memory, "aside_memory")
            logger.info(t("log.ai.command_aside_memory_hit", session_id=event.session_id, plugin=plugin))
        elif mount_reader and plugin_root is not None:
            excerpts = find_receipt_excerpts(plugin_root, receipt, entry_rel)
            if excerpts:
                listed = "、".join(rel for rel, _ in excerpts)
                _note_session(session, listed, "aside_source")
                logger.info(
                    t(
                        "log.ai.command_aside_source_read",
                        session_id=event.session_id,
                        plugin=plugin,
                        files=listed,
                    )
                )
        source_section = ""
        if excerpts:
            blocks = [f"文件 {rel}：\n{excerpt}" for rel, excerpt in excerpts]
            source_section = "刚读到的源码：\n" + "\n\n".join(blocks)
            if len(source_section) > 6000:
                source_section = source_section[:6000] + "\n…已截断"
        followup = _comment_line(
            persona_name,
            plugin_label=plugin_label,
            source_section=source_section,
            memory=memory,
            allow_tools=settings.allow_tools,
            mount_reader=mount_reader,
            command_spell=receipt_commands(receipt, excerpts),
        )
        if mount_reader and plugin_root is not None:
            extra = (make_plugin_source_tool(plugin_root),)
    tokens = bind_tool_policy(settings.allow_tools and not memory, extra) if followup is not None else None
    try:
        spoken = await session.locked_append_then_run(
            user_content,
            assistant_text,
            followup,
            bot,
            event,
            is_framework_injection=False,
            budget_gate=True,
            stats_chat_type=ASIDE_MODE,
        )
    finally:
        if tokens is not None:
            restore_tool_policy(tokens)
    ran = spoken is not None
    advice = ""
    if ran and spoken and spoken.strip():
        from gsuid_core.ai_core.utils import is_silence_marker

        if not is_silence_marker(spoken):
            advice = spoken.strip()
    # 先落盘再发送。发送会让出事件循环，下一位不能赶在写入前又去读源码。
    if advice and not memory and plugin:
        save_advice(plugin, receipt, advice)
    if advice and bot is not None:
        await bot.send(advice)
    logger.info(
        t(
            "log.ai.command_aside_recorded",
            session_id=event.session_id,
            shape=shape,
            commented="yes" if ran else "no",
        )
    )
    return ran


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
    if not bool(ai_config.get_config("enable").data):
        return
    from gsuid_core.buildin_plugins.core_command.core_ai_control.state import is_scope_banned

    if is_scope_banned(event.session_id):
        return
    if not _list_allows(event):
        return
    persona_name = persona_config_manager.get_persona_for_session(event.session_id)
    if persona_name is None:
        return
    settings = load_aside_settings(persona_name)
    if settings is None:
        return
    from gsuid_core.ai_core.startup import is_ai_core_ready

    if not is_ai_core_ready():
        logger.info(t("log.ai.command_aside_skip", reason="not_ready", session_id=event.session_id))
        return
    from gsuid_core.ai_core.ai_router import get_ai_session
    from gsuid_core.ai_core.statistics import statistics_manager

    anchor = plugin_anchor(trigger.func)
    plugin_root = anchor[0] if anchor is not None else None
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
