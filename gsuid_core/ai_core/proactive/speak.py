"""主动发言共享人格表达层：统一 Heartbeat 与 Autonomy 的发声提示词与口吻约束。"""

from __future__ import annotations

import time
import uuid
import asyncio
from typing import Literal
from dataclasses import field, dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    is_silence_marker,
    is_agent_error_text,
    strip_framework_user_leaks,
)
from gsuid_core.ai_core.base_prompts import join_framework_constraints
from gsuid_core.ai_core.persona.persona import Persona
from gsuid_core.ai_core.persona.resource import load_persona
from gsuid_core.ai_core.persona.settings import get_master_title
from gsuid_core.ai_core.configs.task_kinds import TASK_HEARTBEAT_OUTPUT

AUTONOMY_SPEAK_TIMEOUT_SECONDS = 60.0

AUTONOMY_SPEAK_USER_TEMPLATE = (
    "{context_section}{addressing_section}"
    "[你心里刚琢磨好的一点念头（内部草稿，禁止照抄复读）]\n"
    "{draft}\n\n"
    "---\n\n"
    "把上面心里琢磨的那点念头，用你自己的角色口吻自然说出来。\n"
    "直接输出最终要发的话，不要任何前缀、引号或解释。\n\n"
    "[主动发言角色约束]\n"
    "- {length_rule}\n"
    "- 必须完全保持角色口吻：用你自己的语气词和说话节奏，禁止客服腔、汇报腔、播报腔。\n"
    "- 禁止套话开场与过程旁白：严禁写「刚才看到群里在聊…」「刚才看到你在问…」「我去翻了翻资料/查了下/看了一眼…」"
    "这类时间套话或查资料旁白；像真人随口接话那样直接说出想法或关心。\n"
    "- 禁止复读数据与词条：不要把草稿里的多条数值、百分比、参数、词条、专业分析或分点攻略搬出来念；"
    "心里知道就行，嘴上只说一句直觉感受、态度或最有趣的一个细节。\n"
    "- 禁止使用表格、编号列表、加粗标题等结构化格式。\n"
    "- 如果结合最近聊天觉得此刻开口突兀、话题已冷或多余，只输出 `<SILENCE>`。{meme_rule}\n"
)

ComposeStatus = Literal["ok", "silence", "error"]


@dataclass(frozen=True, slots=True)
class ComposeOutcome:
    """自主行动发声轮结果，区分正常成稿、主动沉默与执行失败。"""

    status: ComposeStatus
    text: str = ""
    log_files: list[str] = field(default_factory=list)


def strip_message_quotes(text: str) -> str:
    """剥离模型输出首尾多余的成对引号。"""
    cleaned = text.strip()
    for quote in ('"""', "'''", '"', "'"):
        if cleaned.startswith(quote) and cleaned.endswith(quote) and len(cleaned) > len(quote) * 2:
            return cleaned[len(quote) : -len(quote)].strip()
    if cleaned.startswith("“") and cleaned.endswith("”") and len(cleaned) > 2:
        if cleaned.count("“") == 1 and cleaned.count("”") == 1:
            return cleaned[1:-1].strip()
    return cleaned


def build_proactive_speak_system_prompt(
    persona_name: str,
    persona_content: str,
    *,
    include_meme_protocol: bool | None = None,
) -> str:
    """组装主动发言统一系统提示：入戏钉 + 完整人设 + 表情协议 + 框架底线。"""
    from gsuid_core.ai_core.prompt_group.store import render_prompt

    role = render_prompt(persona_name, "role_playing_start").strip()
    body = f"{role}\n{persona_content}" if role else persona_content
    if include_meme_protocol is None:
        from gsuid_core.ai_core.persona.config import persona_allows_meme_reply

        use_meme = persona_allows_meme_reply(persona_name)
    else:
        use_meme = include_meme_protocol
    if use_meme:
        from gsuid_core.ai_core.meme.vocab import SCENE_WORDS, EMOTION_WORDS, join_words
        from gsuid_core.ai_core.persona.meme_style import resolve_meme_frequency_line

        meme_block = render_prompt(
            persona_name,
            "meme_protocol",
            emotion_words=join_words(EMOTION_WORDS),
            scene_words=join_words(SCENE_WORDS),
            meme_frequency=resolve_meme_frequency_line(persona_name),
        ).strip()
        if meme_block:
            body = f"{body}\n\n{meme_block}"
    return join_framework_constraints(persona_name, body)


def build_autonomy_speak_user_prompt(
    *,
    draft: str,
    is_group: bool,
    recent_context: str,
    master_title: str,
    target_is_master: bool,
    target_name: str = "",
    target_zone_label: str = "",
    allow_meme: bool = False,
    persona_name: str | None = None,
    context_header: str = "",
) -> str:
    """构造自主行动发声轮的纯净 user 提示，支持提示词组 ``autonomy_speak`` 覆盖。"""
    from gsuid_core.ai_core.prompt_group.store import render_prompt

    context_section = ""
    trimmed_ctx = recent_context.strip()
    if trimmed_ctx:
        if context_header.strip():
            context_section = f"{context_header.strip()}\n{trimmed_ctx}\n\n"
        elif trimmed_ctx.startswith("["):
            context_section = f"{trimmed_ctx}\n\n"
        else:
            default_header = "[群里最近发生的事]" if is_group else "[最近的私聊上下文]"
            context_section = f"{default_header}\n{trimmed_ctx}\n\n"

    addressing_lines: list[str] = []
    if target_is_master:
        who = f"{target_name}（你的{master_title}）" if target_name else f"你的{master_title}"
        addressing_lines.append(f"当前说话对象是 {who}，可自然称呼「{master_title}」。")
    elif target_name:
        zone_note = f"（关系：{target_zone_label}）" if target_zone_label else ""
        addressing_lines.append(f"当前说话对象是 {target_name}{zone_note}。")
        if master_title:
            addressing_lines.append(f"对方不是主人，严禁称呼「{master_title}」。")
    elif master_title:
        addressing_lines.append(
            f"称呼须与聊天记录里的发言人对齐：只有回应主人本人发的消息时才称「{master_title}」，认不准发言人就不用称呼。"
        )
    addressing_section = "[对象与称呼]\n" + "\n".join(addressing_lines) + "\n\n" if addressing_lines else ""

    length_rule = (
        "主动发言必须简短：≤50字，最好 15–35 字（1–2 句）。你是群友随口插一句，不是写分析报告或攻略。"
        if is_group
        else (
            "私聊消息必须简短自然：≤60字（1–2 句），留一个自然开放的话头，"
            "禁止「不用回我」「只是提醒一下」「说完我就走了」等单向通知收尾。"
        )
    )
    meme_rule = "\n- 气氛合适时可按表情协议单独附一行 `<meme:情绪>`，不要每条硬加。" if allow_meme else ""
    return render_prompt(
        persona_name,
        "autonomy_speak",
        context_section=context_section,
        addressing_section=addressing_section,
        draft=draft.strip(),
        length_rule=length_rule,
        meme_rule=meme_rule,
    )


async def run_proactive_speak_agent(
    *,
    persona_name: str,
    event: Event,
    system_prompt: str,
    user_prompt: str,
    create_by: str,
    session_id: str,
    timeout_seconds: float = AUTONOMY_SPEAK_TIMEOUT_SECONDS,
) -> ComposeOutcome:
    """运行主动发言子 Agent（Heartbeat_Output / Autonomy_Output 共用收口）。"""
    from gsuid_core.ai_core.gs_agent import create_agent

    output_agent = create_agent(
        system_prompt,
        create_by=create_by,
        task_kind=TASK_HEARTBEAT_OUTPUT,
        persona_name=persona_name,
        session_id=session_id,
        is_subagent=True,
    )
    output_agent.bind_budget_scope(event)
    output_logger = output_agent._session_logger
    generator_log_files: list[str] = []
    result: str = ""
    timed_out = False
    try:
        result = await asyncio.wait_for(
            output_agent.run(user_message=user_prompt, return_mode="return", budget_gate=True),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        if output_logger is not None:
            if timed_out:
                output_logger.log_error("timeout", "proactive speak timeout")
                output_logger.log_run_end()
            generator_log_files.append(str(output_logger._file_path))
            output_logger.close()

    if timed_out:
        if create_by == "Heartbeat_Output":
            logger.warning(t("log.ai.heartbeat_fail_generation_stage", e="timeout"))
        else:
            logger.warning(t("log.ai.autonomy_speak_fail", persona_name=persona_name, e="timeout"))
        return ComposeOutcome(status="error", log_files=list(generator_log_files))

    stripped = result.strip()
    if not stripped or stripped == NO_RESULT_TEXT:
        return ComposeOutcome(status="error", log_files=list(generator_log_files))
    if is_agent_error_text(stripped):
        if create_by == "Heartbeat_Output":
            logger.warning(t("log.ai.heartbeat_fail_generation_stage", e=stripped[:120]))
        else:
            logger.warning(t("log.ai.autonomy_speak_fail", persona_name=persona_name, e=stripped[:120]))
        return ComposeOutcome(status="error", log_files=list(generator_log_files))
    if is_silence_marker(stripped):
        return ComposeOutcome(status="silence", log_files=list(generator_log_files))

    message = strip_framework_user_leaks(strip_message_quotes(stripped)).strip()
    if not message or is_silence_marker(message):
        return ComposeOutcome(status="silence", log_files=list(generator_log_files))
    return ComposeOutcome(status="ok", text=message, log_files=list(generator_log_files))


async def compose_autonomy_message(
    *,
    persona_name: str,
    event: Event,
    draft: str,
    recent_context: str,
    target_is_master: bool,
    target_name: str = "",
    target_zone_label: str = "",
    context_header: str = "",
) -> ComposeOutcome:
    """通过与心跳同源的纯净人格发声轮，把自主行动草稿化为自然台词。"""
    cleaned_draft = draft.strip()
    if not cleaned_draft or not persona_name:
        return ComposeOutcome(status="error")
    if persona_name != "智能助手" and not Persona(persona_name).exists():
        logger.warning(t("log.ai.autonomy_speak_fail", persona_name=persona_name, e="persona profile missing"))
        return ComposeOutcome(status="error")
    persona_content = await load_persona(persona_name)
    if not persona_content:
        logger.warning(t("log.ai.autonomy_speak_fail", persona_name=persona_name, e="persona profile empty"))
        return ComposeOutcome(status="error")

    from gsuid_core.ai_core.persona.config import persona_allows_meme_reply

    allow_meme = persona_allows_meme_reply(persona_name)
    speak_system_prompt = build_proactive_speak_system_prompt(
        persona_name,
        persona_content,
        include_meme_protocol=allow_meme,
    )
    user_prompt = build_autonomy_speak_user_prompt(
        draft=cleaned_draft,
        is_group=bool(event.group_id),
        recent_context=recent_context,
        master_title=get_master_title(persona_name),
        target_is_master=target_is_master,
        target_name=target_name,
        target_zone_label=target_zone_label,
        allow_meme=allow_meme,
        persona_name=persona_name,
        context_header=context_header,
    )
    target_for_sid = event.group_id or event.user_id or "unknown"
    output_session_id = f"autonomy_output_{persona_name}_{target_for_sid}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    outcome = await run_proactive_speak_agent(
        persona_name=persona_name,
        event=event,
        system_prompt=speak_system_prompt,
        user_prompt=user_prompt,
        create_by="Autonomy_Output",
        session_id=output_session_id,
    )
    if outcome.status == "ok":
        logger.info(t("log.ai.autonomy_speak_done", persona_name=persona_name, message=repr(outcome.text)))
    return outcome
