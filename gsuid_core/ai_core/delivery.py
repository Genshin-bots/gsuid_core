"""出站发送协调（拆条、@ 与表情包解析、回复计划分组分发；渲染/制品交付见 delivery_present）。"""

from __future__ import annotations

import re
import json
import asyncio
import contextvars
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Tuple,
    Mapping,
    Callable,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from PIL import Image

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.ai_core.agent_errors import strip_framework_user_leaks
from gsuid_core.ai_core.protocol_text import (
    is_silence_marker,
    _strip_tool_call_artifacts,
    _strip_special_control_tokens,
    remainder_after_protocol_tags,
)
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.delivery_present import (
    _send_report_images,
    _strip_persona_markdown,
    _normalize_html_linebreaks,
    _try_render_markdown_image,
    _split_speech_and_artifacts,
    _should_render_markdown_image,
    _try_present_via_render_agent,
    _resolve_and_deliver_leaked_handles,
)
from gsuid_core.ai_core.angle_bracket_guard import MEME_TAG_PATTERN

if TYPE_CHECKING:
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.reply_plan import ReplyGroup

# 表情没有字数，按 7 个字计，间隔是 1 秒。
MEME_PAUSE_CHARS = 7
_ERROR_OUTPUT_RE = re.compile(r"执行出错[:：]|status_code:\s*\d{3}|Traceback|rate_limit|APIError|TimeoutError")
_INBOX_MAX_TARGETS: int = 3
_INBOX_MAX_BUBBLES: int = 3


def bubble_pause(char_count: int) -> float:
    """两条出站消息的间隔。字数 / 7 秒，最短 0.5，最长 3。"""
    return min(max(char_count / 7, 0.5), 3.0)


async def pause_before_meme() -> None:
    """表情包发出前等待，和文字气泡用同一条间隔。"""
    await asyncio.sleep(bubble_pause(MEME_PAUSE_CHARS))


# 病理级硬顶；主人格长文控长走 FileOS，勿在此用 4k 砍刀污染落盘真身
_TOOL_RESULT_SAFETY_MAX_CHARS = 2_000_000


@runtime_checkable
class _ModelDumpJson(Protocol):
    def model_dump_json(self) -> str: ...


@runtime_checkable
class _JsonDumpable(Protocol):
    def json(self) -> str: ...


async def handle_tool_result(
    bot: Optional[Bot],
    result: object,
    max_length: int = _TOOL_RESULT_SAFETY_MAX_CHARS,
) -> str:
    """序列化工具返回；Message 会尝试 send。默认仅病理级长度硬顶。"""
    if isinstance(result, Message):
        a = "生成内容成功!"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关消息！"
        else:
            a += ", 由于没有Bot对象, 未发送相关消息！"
        return a
    elif isinstance(result, str):
        res_str = result
    elif isinstance(result, dict):
        res_str = json.dumps(result, ensure_ascii=False)
    elif isinstance(result, Image.Image):
        img_bytes = await convert_img(result)
        a = f"生成了图片资源, 资源ID: {RM.register(img_bytes)}"
        if bot is not None:
            await bot.send(img_bytes)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, bytes):
        a = f"生成了某项资源, 资源ID: {RM.register(result)}"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, list):
        res_str = json.dumps(result, ensure_ascii=False)
    elif isinstance(result, _ModelDumpJson):
        res_str = result.model_dump_json()
    elif isinstance(result, _JsonDumpable):
        res_str = result.json()
    else:
        res_str = str(result)

    # 【读窗口】分页体永不砍，避免 offset 续读丢页
    if len(res_str) > max_length:
        if "【读窗口】" in res_str[:800] or "…[分页 " in res_str or "\n[分页 " in res_str:
            return res_str
        return res_str[:max_length] + f"\n...[系统截断: 省略后 {len(res_str) - max_length} 字符]"
    return res_str


_MENTION_LABEL_RE = re.compile(r"([^\s()（）\n]{1,16})\(用户ID:([^)]+)\)")


def mention_names_in_text(text: str) -> Dict[str, str]:
    """从「名(用户ID:id)」抽出可解析的提及。"""
    names: Dict[str, str] = {}
    for match in _MENTION_LABEL_RE.finditer(text or ""):
        name = match.group(1).strip()
        uid = match.group(2).strip()
        if name and uid and name != uid:
            names[name] = uid
    return names


def mention_names_from_event(ev: Event | None) -> Dict[str, str]:
    if ev is None:
        return {}
    sender = ev.sender
    nick = ""
    if isinstance(sender, dict) and "nickname" in sender and isinstance(sender["nickname"], str):
        nick = sender["nickname"].strip()
    uid = str(ev.user_id).strip() if ev.user_id else ""
    if nick and uid and nick != uid:
        return {nick: uid}
    return {}


def rewrite_nickname_mentions(
    text: str,
    names: Dict[str, str],
    *,
    already_at: str | None = None,
) -> str:
    """<@昵称> 收成 <@用户ID>。已经在提醒的人去掉重复；对不上的括号删掉。裸 @ 保留。"""
    from gsuid_core.ai_core.outbound import sub_mentions, is_mention_id, normalize_mention_token

    def _repl(match: re.Match[str]) -> str:
        token = normalize_mention_token(match.group(1))
        if not token:
            return ""
        uid = names[token] if token in names else ""
        if uid:
            if already_at and uid == already_at:
                return ""
            return f"<@{uid}>"
        if is_mention_id(token):
            return f"<@{token}>"
        return ""

    return sub_mentions(text, _repl)


def _persona_max_bubbles(ev: Event | None) -> int:
    """单轮主通道气泡上限。按当前人格的 ``chat_style`` 派生，缺人格回落默认档。"""
    from gsuid_core.ai_core.persona.settings import persona_name_from_event
    from gsuid_core.ai_core.persona.chat_style import resolve_chat_style

    return resolve_chat_style(persona_name_from_event(ev)).bubbles


def _outbound_auditor(ev: Event | None) -> Optional[Callable[[str, int, int], None]]:
    """主通道出站审计回调。拿不到 session 时返回 None（评估 / 子代理等无归属场景）。

    session 在发送前解析一次，不在气泡循环里反复查注册表。
    """
    if ev is None or not ev.session_id:
        return None
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    sess = get_ai_session_registry().get_ai_session(ev.session_id)
    if sess is None or sess._session_logger is None:
        return None

    group_id = str(ev.group_id) if ev.group_id else ""
    target_user = str(ev.user_id) if ev.user_id else ""
    session_logger = sess._session_logger

    def _log(text: str, index: int, total: int) -> None:
        session_logger.log_outbound_audit(
            group_id=group_id,
            text=text,
            image_id="",
            topic="",
            target_user=target_user,
            bubble_index=index,
            bubble_total=total,
        )

    return _log


def pack_outbound_bubbles(blocks: Sequence[str], budget: int) -> list[str]:
    """按气泡预算无损打包分段文本；超出预算的尾部合并进最后一个气泡。"""
    cleaned = [b.strip() for b in blocks if b.strip()]
    if not cleaned or budget <= 0:
        return []
    if len(cleaned) <= budget:
        return cleaned
    head = cleaned[: budget - 1]
    tail = "\n\n".join(cleaned[budget - 1 :])
    return [*head, tail]


async def send_chat_result(
    bot: Bot,
    text: str,
    ev: Event | None = None,
    extra_metadata: Optional[Mapping[str, object]] = None,
    ooc_check: bool = True,
    at_user_id: str | None = None,
    mention_names: Optional[Dict[str, str]] = None,
    _reply_prefix: Optional[Sequence[Message]] = None,
    _plan_group: bool = False,
    _reply_target: Optional[Tuple[int, str]] = None,
) -> None:
    """
    解析并发送聊天结果，支持：
    - 按换行分割多条消息
    - <@用户ID> 或 <@昵称> → MessageSegment.at。裸 @ 不解析
    - at_user_id：框架强制前置 @（任务交付回灌等，不依赖模型自觉）
    - <meme: 情绪> 标记（可带反引号；尖括号和冒号旁边的空格也认）→ 触发表情包发送（需传入 ev）
    - extra_metadata：透传到 ``Bot.send`` 的 ``extra_metadata``，最终落到
      ``message_history`` 记录上（如主动消息的 ``proactive=True / source / reason``）
    - ooc_check：出戏防火墙开关。gs_agent 自判产物已走过系统提醒，传 False 放行
    """
    if not text:
        return

    meta: dict[str, object] = dict(extra_metadata) if extra_metadata else {}
    if "speech_channel" not in meta:
        meta["speech_channel"] = "persona"
    extra_metadata = meta

    _trimmed = text.strip()
    if is_silence_marker(_trimmed):
        logger.debug(i18n_t("log.ai.send_chat_result_special_trimmed_skip", _trimmed=repr(_trimmed)))
        return

    # 必须在 remainder_after_protocol_tags 剥 <reply #n> 之前解析。
    _turn = None
    if meta["speech_channel"] == "persona" and not _plan_group:
        from gsuid_core.ai_core.inbox.turn import current_turn

        _turn = current_turn()
    if _turn is not None:
        from gsuid_core.ai_core.reply_plan import parse_reply_plan

        _valid = {e.item.seq for e in _turn.entries}
        _plan = parse_reply_plan(
            _trimmed,
            valid_seqs=frozenset(_valid),
            default_seqs=(_turn.primary.item.seq,),
            max_targets=_INBOX_MAX_TARGETS,
        )
        if _plan.invalid_refs:
            logger.info(i18n_t("log.inbox.reply_ref_invalid", seqs=",".join(str(s) for s in _plan.invalid_refs)))
        if _plan.silence:
            return
        if _plan.groups:
            await _send_reply_plan_groups(
                bot,
                _plan.groups,
                turn=_turn,
                ev=ev,
                meta=meta,
                ooc_check=ooc_check,
            )
            return

    _speech = remainder_after_protocol_tags(_trimmed).strip()
    if not _speech:
        return
    _speech = strip_framework_user_leaks(_speech)
    if not _speech:
        return
    text = _speech

    if _ERROR_OUTPUT_RE.search(text):
        logger.warning(i18n_t("log.ai.send_chat_result_intercepted_fail", text=text[:100]))
        text = "脑子转不动了，等下再说。"

    text = _strip_tool_call_artifacts(text)
    text = _strip_special_control_tokens(text)
    text = await _resolve_and_deliver_leaked_handles(text, bot, extra_metadata, ev=ev)
    text, report_blocks = _split_speech_and_artifacts(text)

    from gsuid_core.ai_core.angle_bracket_guard import (
        find_illegal_angle_tags,
        sanitize_illegal_angle_tags,
    )

    _ab_leaks = find_illegal_angle_tags(text)
    if _ab_leaks:
        logger.warning(
            i18n_t(
                "log.ai.send_chat_result_sanitize_residual_tags",
                tags=_ab_leaks[:4],
                preview=repr(text[:80]),
            )
        )
        text = sanitize_illegal_angle_tags(text)
        if not text.strip() and not report_blocks:
            return

    text = _normalize_html_linebreaks(text)
    from gsuid_core.ai_core.outbound import rewrite_cq_at, strip_mention_tags

    text = rewrite_cq_at(text)
    _names = mention_names_from_event(ev)
    if mention_names:
        _names.update(mention_names)
    text = rewrite_nickname_mentions(text, _names, already_at=at_user_id)

    logger.trace(i18n_t("log.ai.meme_text_raw_output", text=repr(text)))

    meme_tags: list[str] = MEME_TAG_PATTERN.findall(text)
    md_source: str = MEME_TAG_PATTERN.sub("", text).strip()

    clean_text: str = _strip_persona_markdown(md_source)
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    clean_text = re.sub(r"^[，。！？\s]+|[，。！？\s]+$", "", clean_text)

    _ooc_replaced = False
    if (clean_text or report_blocks) and ooc_check:
        from gsuid_core.ai_core.control.outbound_detectors import (
            inspect_outbound_ooc,
            is_ooc_firewall_enabled,
        )

        if is_ooc_firewall_enabled():
            _user_text = ev.raw_text if ev is not None and ev.raw_text else ""
            _hit = inspect_outbound_ooc(clean_text, user_text=_user_text) if clean_text else None
            if _hit is not None:
                logger.warning(
                    i18n_t(
                        "log.ai.firewall_result_hit_ooc_red",
                        p0=_hit.category,
                        p1=_hit.matched,
                    )
                )
                clean_text = ""
                _ooc_replaced = True
            if report_blocks:
                _kept_blocks: List[Tuple[str, str]] = []
                for _r_title, _r_body in report_blocks:
                    _r_hit = inspect_outbound_ooc(_r_body, user_text=_user_text)
                    if _r_hit is None:
                        _kept_blocks.append((_r_title, _r_body))
                    else:
                        logger.warning(
                            i18n_t(
                                "log.ai.firewall_report_artifact_block",
                                p0=_r_hit.category,
                                p1=_r_hit.matched,
                            )
                        )
                report_blocks = _kept_blocks

    async def _send_trailing_artifacts() -> None:
        if report_blocks:
            await _send_report_images(report_blocks, bot, extra_metadata, ev)
        if meme_tags and ev is not None:
            await _send_meme_from_tag(meme_tags[0].strip(), bot, ev)

    logger.trace(i18n_t("log.ai.meme_parsed_tags_cleaned", meme_tags=meme_tags, clean_text=repr(clean_text)))

    if not clean_text:
        await _send_trailing_artifacts()
        return

    if not _ooc_replaced and _should_render_markdown_image(md_source):
        if await _try_present_via_render_agent(md_source, bot, ev, extra_metadata):
            await _send_trailing_artifacts()
            return
        if await _try_render_markdown_image(md_source, bot, extra_metadata):
            await _send_trailing_artifacts()
            return

    _PERSONA_MAX_BUBBLES = _persona_max_bubbles(ev)
    raw_blocks = [b for b in re.split(r"\n\s*\n", clean_text) if b.strip()]
    _plan_turn = None
    if meta["speech_channel"] == "persona":
        from gsuid_core.ai_core.inbox.turn import current_turn

        _plan_turn = current_turn()

    _budget = _PERSONA_MAX_BUBBLES
    if _plan_turn is not None:
        _turn_rem = max(0, _INBOX_MAX_BUBBLES - _plan_turn.bubbles_sent)
        _budget = min(_PERSONA_MAX_BUBBLES, _turn_rem)

    if _budget <= 0:
        if raw_blocks:
            logger.info(i18n_t("log.inbox.bubble_capped", count=len(raw_blocks)))
        await _send_trailing_artifacts()
        return

    if len(raw_blocks) > _budget:
        logger.debug(i18n_t("log.ai.persona_bubbles_clamped", p0=_budget))
    blocks = pack_outbound_bubbles(raw_blocks, _budget)
    _force_at = (at_user_id or "").strip()
    _at_done = False
    _audit = _outbound_auditor(ev)
    _total = len(blocks)
    _prefix = list(_reply_prefix) if _reply_prefix else []
    _deferred_reply = _reply_target

    for _idx, block in enumerate(blocks, 1):
        if not block.strip():
            continue

        segments = _parse_at_segments(block)
        if _prefix:
            segments = [*_prefix, *segments]
            _prefix = []
        _has_force_in_block = any(s.type == "at" and str(s.data or "") == _force_at for s in segments)
        if _force_at and not _at_done:
            if not _has_force_in_block:
                segments = [MessageSegment.at(_force_at), *segments]
            _at_done = True
        elif _at_done:
            kept = [s for s in segments if s.type != "at"]
            if not kept:
                continue
            segments = kept
        segments = _ensure_at_trailing_space(segments)

        if _plan_turn is not None:
            _plan_turn.bubbles_sent += 1

        plain_text = strip_mention_tags(block)

        await asyncio.sleep(bubble_pause(len(plain_text)))

        if _deferred_reply is not None and ev is not None:
            _t_seq, _t_mid = _deferred_reply
            _deferred_reply = None
            from gsuid_core.message_history import get_history_manager

            _cur_latest = -1
            for _rec in reversed(get_history_manager().get_history(ev)):
                if _rec.role == "user" and _rec.link is not None:
                    _cur_latest = _rec.link.seq
                    break
            if _cur_latest != -1 and _cur_latest != _t_seq and not any(s.type == "reply" for s in segments):
                segments = [Message("reply", _t_mid), *segments]

        await bot.send(segments, extra_metadata=extra_metadata)
        if _audit is not None:
            _audit(plain_text.strip(), _idx, _total)

    await _send_trailing_artifacts()


async def _send_reply_plan_groups(
    bot: Bot,
    groups: Sequence["ReplyGroup"],
    *,
    turn: "InboxTurn",
    ev: Event | None,
    meta: dict[str, object],
    ooc_check: bool,
) -> None:
    """回复计划分发（§1.6）：每组一次出站，绑定 OutboundBinding 与目标收件人。

    首气泡形态：单目标且发送时刻仍为 A 轨最新 user 记录 → 纯文本；非最新（含打字停顿
    期间有新消息插队）且有 msg_id → 前置 ``Message("reply", msg_id)``；多目标 → 依次
    前置 ``MessageSegment.at(uid)``。
    """
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.message_history.link import OutboundBinding, bind_outbound, reset_outbound
    from gsuid_core.ai_core.evidence.store import get_evidence_store

    pending = frozenset(e.item.seq for e in turn.entries)

    _store = get_evidence_store(turn.session_id)
    _personal_cred = any(
        (o := _store.find(obs_id)) is not None and o.credential_owner is not None and o.credential_owner != "shared"
        for obs_id in turn.observations
    )
    latest_user_seq = -1
    if ev is not None:
        for rec in reversed(get_history_manager().get_history(ev)):
            if rec.role == "user" and rec.link is not None:
                latest_user_seq = rec.link.seq
                break

    for group in groups:
        target_seqs = [s for s in group.seqs]
        if not target_seqs:
            target_seqs = [turn.primary.item.seq]

        targets: list[tuple[str, str]] = []
        for seq in target_seqs:
            uid, uname = "", ""
            entry = turn.entry_for(seq)
            if entry is not None:
                uid = str(entry.event.user_id or "")
                if "nickname" in entry.event.sender:
                    nick = entry.event.sender["nickname"]
                    if isinstance(nick, str):
                        uname = nick.strip()
            elif ev is not None:
                rec = get_history_manager().find_by_seq(ev, seq)
                if rec is not None:
                    uid = str(rec.user_id)
                    uname = rec.user_name or ""
            if uid and all(u != uid for u, _ in targets):
                targets.append((uid, uname))
        if not targets:
            targets.append((str(ev.user_id if ev is not None else ""), ""))

        prefix: list[Message] = []
        reply_target: tuple[int, str] | None = None
        if len(targets) > 1:
            prefix = [MessageSegment.at(uid) for uid, _ in targets]
        else:
            msg_id = ""
            entry = turn.entry_for(target_seqs[0])
            if entry is not None and entry.event.msg_id:
                msg_id = str(entry.event.msg_id)
            elif ev is not None:
                rec = get_history_manager().find_by_seq(ev, target_seqs[0])
                if rec is not None and "msg_id" in rec.metadata:
                    msg_id = str(rec.metadata["msg_id"])
            if target_seqs[0] != latest_user_seq and msg_id:
                prefix = [Message("reply", msg_id)]
            elif msg_id:
                reply_target = (target_seqs[0], msg_id)

        gmeta = dict(meta)
        gmeta["reply_to_user_id"] = targets[0][0]
        if targets[0][1]:
            gmeta["reply_to_user_name"] = targets[0][1]

        binding = OutboundBinding(
            seen_seq=turn.head_seq,
            in_reply_to_seqs=tuple(target_seqs),
            evidence=tuple(turn.observations),
            personal_credential=_personal_cred,
        )
        token = bind_outbound(binding)
        try:
            await send_chat_result(
                bot,
                group.text,
                ev=ev,
                extra_metadata=gmeta,
                ooc_check=ooc_check,
                _reply_prefix=prefix,
                _plan_group=True,
                _reply_target=reply_target,
            )
        finally:
            reset_outbound(token)
        turn.answered |= {s for s in target_seqs if s in pending}


def _split_meme_tag(tag: str) -> tuple[str, str]:
    """第一个词是情绪，其余是场景。空格、中文逗号、英文逗号都能拆。"""
    text = tag.strip().replace("，", " ").replace(",", " ")
    mood, _, scene = text.partition(" ")
    return mood.strip(), scene.strip()


async def _send_meme_from_tag(tag: str, bot: Bot, ev: Event) -> None:
    """解析 meme 标记并发送对应表情包"""
    from gsuid_core.ai_core.outbound import (
        format_meme_placeholder,
        set_outbound_image_label,
        reset_outbound_image_label,
    )
    from gsuid_core.ai_core.persona.config import persona_allows_meme_reply
    from gsuid_core.ai_core.buildin_tools.meme_tools import _get_persona_for_event

    persona = _get_persona_for_event(ev)
    if not persona_allows_meme_reply(persona):
        return

    mood, scene = _split_meme_tag(tag)
    label_token: contextvars.Token | None = None
    try:
        from gsuid_core.ai_core.meme.library import _read_file, get_memes_base_path
        from gsuid_core.ai_core.meme.selector import pick
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        record, reason = await pick(
            mood=mood,
            scene=scene,
            persona=persona,
            session_id=ev.session_id,
        )
        if record is None:
            logger.debug(i18n_t("log.ai.meme_tag_skip", reason=reason, mood=mood, scene=scene))
            return

        file_path = get_memes_base_path() / record.file_path
        if not file_path.exists():
            logger.debug(i18n_t("log.ai.meme_file_exist_path", file_path=file_path))
            return

        image_data = await _read_file(file_path)
        img_b64 = await convert_img(image_data)
        from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

        label_token = set_outbound_image_label(format_meme_placeholder(mood))
        await pause_before_meme()
        await bot.send(MessageSegment.image(img_b64), extra_metadata=turn_reply_metadata(ev))
        await AiMemeRecord.record_usage(record.meme_id, ev.group_id or "")
        logger.info(i18n_t("log.ai.meme_tag_triggered_mood", p0=record.meme_id, mood=mood))
    except Exception as e:
        logger.debug(i18n_t("log.ai.meme_tag_fail", e=e))
    finally:
        if label_token is not None:
            reset_outbound_image_label(label_token)


def _ensure_at_trailing_space(segments: list[Message]) -> list[Message]:
    """确保 at 消息段后紧跟的 text 消息段以空格开头，防止各平台粘连。"""
    result = list(segments)
    for i in range(len(result) - 1):
        if result[i].type == "at" and result[i + 1].type == "text":
            txt = str(result[i + 1].data or "")
            if txt and not txt.startswith((" ", "\t", "\n")):
                result[i + 1] = MessageSegment.text(" " + txt)
    return result


def _parse_at_segments(text: str) -> list[Message]:
    """把 <@用户ID> 解析成 at 段。认转义尖括号和多余空格。裸 @ 留在文本里。"""
    from gsuid_core.ai_core.outbound import (
        is_mention_id,
        iter_mentions,
        rewrite_cq_at,
        normalize_mention_token,
    )

    text = rewrite_cq_at(text)
    segments: list[Message] = []
    last_end = 0

    for match in iter_mentions(text):
        token = normalize_mention_token(match.group(1))
        if not is_mention_id(token):
            continue
        before = text[last_end : match.start()]
        if before:
            segments.append(MessageSegment.text(before))

        segments.append(MessageSegment.at(token))

        last_end = match.end()

    tail = text[last_end:]
    if tail:
        segments.append(MessageSegment.text(tail))

    if not segments:
        return [MessageSegment.text(text)]

    return _ensure_at_trailing_space(segments)
