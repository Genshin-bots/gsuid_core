"""
消息发送工具模块

提供主动向用户发送消息的能力，支持文本消息和图片消息。

资源 ID 解析：``image_id`` 支持三种来源：

1. ``img_xxxxxxxx``——RM（``ResourceManager``）注册的临时图片，``RM.get`` 直读。
2. ``res_xxxxxxxx``——Kanban ``AIAgentArtifact`` 句柄；本工具会读 artifact 的
   ``payload_path`` / ``payload_inline``，把数据 ``RM.register`` 自动转一次成
   RM 资源再发，让主人格 / 转译代理可以直接把能力代理产物发给主人，无需关心
   两套存储的区分（详见 ``AI_AGENT_ARCHITECTURE.md`` §3.6）。
3. ``http://`` / ``https://`` / ``base64://``——直接走 ``MessageSegment.image``。
"""

import asyncio
from typing import TYPE_CHECKING, Dict, List, Tuple, Union, Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.resource_manager import RM

if TYPE_CHECKING:
    pass


# 单轮节流：弱模型常把 send_message_by_ai 当回复通道一轮连发好几条刷屏。与 scheduler.py
# add_once_task 同构，key=(session_id, turn_id)，超限直接拒发、提示改用正文输出。
PER_TURN_SEND_MESSAGE_LIMIT = 2
_PER_TURN_SEND_MESSAGE_COUNT: Dict[Tuple[str, str], int] = {}


def _get_send_throttle_key(ctx: RunContext[ToolContext]) -> Optional[Tuple[str, str]]:
    """构造 (session_id, turn_id) 节流键；缺 ev / turn_id 时跳过节流（返回 None）。"""
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return None
    turn_id = tool_ctx.extra.get("turn_id") if tool_ctx.extra else None
    if not turn_id:
        return None
    return (str(ev.session_id), str(turn_id))


def clear_turn_send_throttle(session_id: str, turn_id: str) -> None:
    """回合结束时清理本轮的 send_message_by_ai 计数（由 gs_agent finally 调用）。"""
    _PER_TURN_SEND_MESSAGE_COUNT.pop((str(session_id), str(turn_id)), None)


def _looks_like_image_bytes(data: bytes) -> bool:
    """按文件头魔数判断是否为常见图片字节（mime 缺失时用）。"""
    if not data:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    if data[:2] == b"BM":
        return True
    return False


def _is_textish_mime(mime: str) -> bool:
    """mime 是否应按文本解码（非图片落盘）。"""
    if not mime or mime.startswith("text/"):
        return True
    if mime in ("application/json", "application/xml", "application/javascript"):
        return True
    return mime.endswith("+json") or mime.endswith("+xml")


async def _resolve_kanban_artifact(res_id: str) -> Optional[Union[bytes, str]]:
    """解析 ``res_xxx``。bytes=仅图片；str=文本/非图片；None=不存在。

    2026-08-04：text/markdown 落盘被当图片塞多模态 → MiniMax unknown format 整轮 400。
    """
    if not res_id.startswith("res_"):
        return None
    try:
        from gsuid_core.ai_core.planning.models import AIAgentArtifact
    except ImportError:
        return None
    art = await AIAgentArtifact.get_by_id(res_id)
    if art is None:
        return None

    mime = (art.mime or "").lower().strip()
    is_image_mime = mime.startswith("image/")

    if art.payload_path:
        p = Path(art.payload_path)
        if not await asyncio.to_thread(p.exists):
            logger.debug(t("log.ai.buildintools_kanban_artifact_res", res_id=res_id, p0=art.payload_path))
            return None
        data = await asyncio.to_thread(p.read_bytes)
        # 以魔数为准：只有真图返回 bytes（mime 标 image/* 内容却是 md 时也拒）
        if _looks_like_image_bytes(data):
            return data
        # 非图：textish / 无 mime → 文本；否则标记串（供上层 str 分支拒绝当图）
        if _is_textish_mime(mime) or is_image_mime:
            return data.decode("utf-8", errors="replace")
        return f"[binary non-image artifact mime={mime or 'unknown'} size={len(data)} path={art.payload_path}]"

    if art.payload_inline:
        # inline 存不了真图，一律当文本
        return art.payload_inline
    return None


async def filter_outbound_media_safety(
    ev: Optional[Event],
    media_parts: List[Message],
    success_img_ids: List[str],
) -> Tuple[List[Message], List[str]]:
    """出站图片安全检查插槽（默认放行，留待安全插件/风控挂接）。"""
    return media_parts, success_img_ids


def _normalize_image_ids(val: Optional[Union[List[str], str]]) -> List[str]:
    """统一规整图片ID参数，兼容字符串、列表及JSON数组字符串。"""
    if val is None:
        return []
    if isinstance(val, str):
        v = val.strip()
        if not v:
            return []
        if v.startswith("[") and v.endswith("]"):
            try:
                import json

                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if item and str(item).strip()]
            except Exception:
                pass
        return [v]
    if isinstance(val, (list, tuple, set)):
        result: List[str] = []
        for item in val:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
        return result
    return []


def _note_send_block(tool_ctx: ToolContext, reason: str, content: str) -> None:
    """主动发送被闸拦住时，把原文和原因码写入当前会话日志。"""
    from gsuid_core.ai_core.agent_run.support import log_output_block_on_session

    sid = tool_ctx.parent_session_id or ""
    if not sid and tool_ctx.ev is not None and tool_ctx.ev.session_id:
        sid = str(tool_ctx.ev.session_id)
    log_output_block_on_session(sid, reason, content)


@ai_tools(category="self", code_callable=False)
async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    text: str = "",
    image_ids: Optional[Union[List[str], str]] = None,
    video_id: str = "",
    audio_id: str = "",
    user_id: Optional[str] = None,
    image_id: Optional[Union[List[str], str]] = None,
) -> str:
    """
    主动发送消息给用户

    支持发送文本消息、图片消息（单张或多张组合）、视频消息、音频消息。
    AI 可以传入 text 和/或 image_ids 和/或 video_id 和/或 audio_id，系统会组合发送。

    **重要**：当其他工具返回 `[视频消息]`/`[图片消息]`/`[语音消息]` 等标记时，
    表示该媒体已由框架自动发送给用户，**无需再调用本工具重复发送**。
    本工具仅用于主动补充发送文字说明或追加媒体时使用。

    **资源 ID 必须来自上下文**：image_ids / video_id / audio_id 只能填本轮对话中
    实际出现过的 ID（如 `img_xxxxxxxx`、`res_xxxxxxxx`）或图片 URL，**禁止自行构造或猜测**。
    没有可用资源就只发 text。

    Args:
        ctx: 工具执行上下文（包含bot和ev对象）
        text: 文本内容，可选
        image_ids: 图片资源ID或URL列表，可选。支持单个ID（如"img_123"）或列表（如["img_1", "img_2"]）
        video_id: 视频资源ID，可选，格式通常为"video_xxxxxx"或"img_xxxxxx"
        audio_id: 音频资源ID，可选，格式通常为"aud_xxxxxxxx"
        user_id: 可选，目标用户ID，默认为事件关联的用户
        image_id: 可选，兼容旧参数（单个或多个图片资源ID，等同于 image_ids）

    Returns:
        发送结果描述字符串
    """
    tool_ctx: ToolContext = ctx.deps
    bot: Optional[Bot] = tool_ctx.bot

    if bot is None:
        logger.warning(t("log.ai.buildintools_bot_object_empty"))
        return "发送失败：Bot对象不可用"

    if not tool_ctx.allow_user_outbound:
        return (
            "发送失败：当前为能力代理/子 Agent，禁止对用户会话直发。"
            "请把结论与产物登记为 artifact 后返回主人格，由主人格出站。"
        )

    target_image_ids: list[str] = _normalize_image_ids(image_ids)
    for img in _normalize_image_ids(image_id):
        if img not in target_image_ids:
            target_image_ids.append(img)

    if not text and not target_image_ids and not video_id and not audio_id:
        return "发送失败：text、image_ids、video_id 和 audio_id 至少提供一个"

    # 单次调用图片上限截断至 9 张，避免单条消息超限
    if len(target_image_ids) > 9:
        target_image_ids = target_image_ids[:9]

    # 单轮节流：超过 PER_TURN_SEND_MESSAGE_LIMIT 直接拒发，把模型推回正文输出
    throttle_key = _get_send_throttle_key(ctx)
    if throttle_key is not None and _PER_TURN_SEND_MESSAGE_COUNT.get(throttle_key, 0) >= PER_TURN_SEND_MESSAGE_LIMIT:
        return (
            f"⚠️ 本轮你已用 send_message_by_ai 主动发过 {PER_TURN_SEND_MESSAGE_LIMIT} 条了。"
            "它**不是常规回复通道**——接下来想对用户说的话，**直接作为你的回复正文输出**即可"
            "（框架会自动发出，并自动处理换行分条 / 长文转图）。本轮请勿再调用本工具。"
        )

    extra = tool_ctx.extra
    pol = extra["speech_policy"] if "speech_policy" in extra and isinstance(extra["speech_policy"], str) else ""
    has_st = extra["has_status_tool"] is True if "has_status_tool" in extra else False
    has_media = bool(target_image_ids or video_id or audio_id)
    if pol == "status_ok" and not has_st and not has_media:
        return "⚠️ 用户在追问进度：先 list_my_kanban_tasks / artifact_get_recent 查状态，再发。禁止空口报完成。"

    # 统一输出闸门（尖括号 + OOC …）：打回则 return feedback，放行继续发
    if text:
        from gsuid_core.ai_core.output_gate import tool_gate_feedback
        from gsuid_core.ai_core.agent_run.speech_policy import should_block_user_visible_text

        _ev_text = tool_ctx.ev.raw_text if tool_ctx.ev is not None and tool_ctx.ev.raw_text else ""
        if text:
            from gsuid_core.ai_core.angle_bracket_guard import (
                has_illegal_angle_tags,
                sanitize_illegal_angle_tags,
            )

            if has_illegal_angle_tags(text):
                cleaned = sanitize_illegal_angle_tags(text)
                if cleaned:
                    text = cleaned
        if text:
            _gate_fb = tool_gate_feedback(text, tool_ctx.extra, user_text=_ev_text)
            if _gate_fb is not None:
                _note_send_block(tool_ctx, "output_gate_tool", text)
                if has_media:
                    text = ""
                else:
                    return _gate_fb
        if text:
            _blk, _why = should_block_user_visible_text(
                pol or "free",
                text,
                pending_async=False,
                image_sent=has_media,
                has_status_tool=has_st,
                tool_calls_so_far=["send_message_by_ai"],
            )
            if _blk:
                _note_send_block(tool_ctx, _why, text)
                if has_media:
                    # 图仍发；被拦的台词不出站（与 TextPart 同一套 should_block）
                    text = ""
                else:
                    return "⚠️ 台词被话术闸拦住。改掉编排或进度汇报后再发，或只发媒体、不附这句台词。"

    # 目标用户（§E.3）：默认当前对话者；Event 保证 user_id 存在，不用 getattr 兜底
    ev = tool_ctx.ev
    _wake = extra["delivery_wake"] is True if "delivery_wake" in extra else False
    if _wake and ev is not None and ev.user_id:
        target_id = str(ev.user_id)
        extra["at_user_id"] = target_id
    else:
        target_id = user_id or (str(ev.user_id) if ev is not None else "")
    if text:
        from gsuid_core.ai_core.persona.settings import persona_name_from_event
        from gsuid_core.ai_core.agent_run.speech_policy import title_mentioned, non_master_title

        _ban = non_master_title(target_id, persona_name_from_event(ev))
        if title_mentioned(_ban, text):
            _note_send_block(tool_ctx, "master_title", text)
            return f"⚠️ 接收人不是主人，台词不能出现「{_ban}」。改写后再发，不要用这个称呼。"
    session_id = str(ev.session_id) if ev is not None else (tool_ctx.parent_session_id or "")

    for img_id in target_image_ids:
        if img_id.startswith("dlg_"):
            return (
                "❌ 这是委派句柄（dlg_），不是图片句柄。"
                "请用交付帧里的 res_ 或 artifact_get_recent 取图后再发；"
                "没有 res_ 说明出图未成功，需重新委派 render_agent。"
            )

    occupied_res_ids: list[str] = []
    failed_notes: list[str] = []
    valid_image_ids: list[str] = []

    for img_id in target_image_ids:
        if img_id.startswith("res_"):
            from gsuid_core.ai_core.outbound import (
                lookup_res_owner,
                res_owner_refusal,
                speaker_may_take_res,
                try_claim_image_delivery,
            )

            looked = await lookup_res_owner(img_id)
            speaker = str(ev.user_id) if ev is not None and ev.user_id else ""
            if not speaker_may_take_res(looked, speaker):
                logger.warning(
                    t(
                        "log.ai.delivery_owner_mismatch",
                        res_id=img_id,
                        owner=looked.owner_user_id,
                        target=speaker,
                    )
                )
                failed_notes.append(res_owner_refusal(looked, speaker))
                continue

            claim = await try_claim_image_delivery(ev, img_id, session_id=session_id)
            if claim.refuse is not None:
                failed_notes.append(claim.refuse)
                continue
            if claim.occupied:
                occupied_res_ids.append(img_id)
        valid_image_ids.append(img_id)

    async def _abort_send(msg: str) -> str:
        if occupied_res_ids:
            from gsuid_core.ai_core.outbound import release_image_delivery

            for o_id in occupied_res_ids:
                await release_image_delivery(ev, o_id)
        return msg

    sent = False
    try:
        media_parts: List[Message] = []
        success_img_ids: list[str] = []

        for img_id in valid_image_ids:
            if img_id.startswith("http") or img_id.startswith("base64://"):
                media_parts.append(MessageSegment.image(img_id))
                success_img_ids.append(img_id)
            elif img_id.startswith("res_"):
                kanban_payload = await _resolve_kanban_artifact(img_id)
                if kanban_payload is None:
                    logger.debug(
                        t(
                            "log.ai.buildintools_kanban_artifact_parsing_fail",
                            image_id=img_id,
                        )
                    )
                    try:
                        img_data = await RM.get(img_id)
                        media_parts.append(MessageSegment.image(img_data))
                        success_img_ids.append(img_id)
                    except ValueError as e:
                        logger.warning(t("log.ai.buildintools_rm_get_image_id", image_id=img_id, e=e))
                        if img_id in occupied_res_ids:
                            from gsuid_core.ai_core.outbound import release_image_delivery

                            await release_image_delivery(ev, img_id)
                            occupied_res_ids.remove(img_id)
                        if "找不到资源" in str(e):
                            failed_notes.append(
                                f"❌ 资源ID: {img_id} 无法解析（artifact 不存在或已过期，"
                                f"可能是渲染子任务未真正出图）。请重新 "
                                f'create_subagent(agent_profile="render_agent", task=原事实包) '
                                f"再委派一次出图；勿再发送该 ID，勿向用户声称已发图。"
                            )
                        else:
                            failed_notes.append(f"❌ 资源ID: {img_id} 数据转换失败: {e}")
                elif isinstance(kanban_payload, bytes):
                    new_rm_id = RM.register(kanban_payload)
                    logger.info(
                        t(
                            "log.ai.buildintools_kanban_artifact_image",
                            image_id=img_id,
                            new_rm_id=new_rm_id,
                        )
                    )
                    media_parts.append(MessageSegment.image(kanban_payload))
                    success_img_ids.append(img_id)
                else:
                    if img_id in occupied_res_ids:
                        from gsuid_core.ai_core.outbound import release_image_delivery

                        await release_image_delivery(ev, img_id)
                        occupied_res_ids.remove(img_id)
                    failed_notes.append(
                        f"❌ 资源ID: {img_id} 是文本类 Kanban artifact（非图片字节），"
                        f"请用 artifact_get('{img_id}') 取原文后："
                        f"短文用 text 参数发送，长文/多数据用 render_html_to_image 出图。"
                    )
            else:
                try:
                    logger.debug(t("log.ai.buildintools_calling_rm_get_2", image_id=img_id))
                    img_data = await RM.get(img_id)
                    logger.debug(t("log.ai.buildintools_rm_get_succeeded_ok_2", p0=type(img_data)))
                    media_parts.append(MessageSegment.image(img_data))
                    success_img_ids.append(img_id)
                except ValueError as e:
                    logger.warning(t("log.ai.buildintools_rm_get_image_id", image_id=img_id, e=e))
                    if "找不到资源" in str(e):
                        failed_notes.append(f"❌ 找不到资源ID: {img_id}，可能已过期或ID不正确。")
                    else:
                        failed_notes.append(f"❌ 资源ID: {img_id} 数据转换失败: {e}")

        if video_id:
            try:
                logger.debug(t("log.ai.buildintools_calling_rm_get_3", video_id=video_id))
                video_data = await RM.get(video_id)
                logger.debug(t("log.ai.buildintools_rm_get_succeeded_ok_3", p0=type(video_data)))
                media_parts.append(MessageSegment.video(video_data))
            except ValueError as e:
                logger.warning(t("log.ai.buildintools_rm_get_video_id", video_id=video_id, e=e))
                if "找不到资源" in str(e):
                    return f"❌ 找不到资源ID: {video_id}，可能已过期或ID不正确。"
                else:
                    return f"❌ 资源ID: {video_id} 数据转换失败: {e}"

        if audio_id:
            try:
                logger.debug(t("log.ai.buildintools_calling_rm_get", audio_id=audio_id))
                audio_data = await RM.get(audio_id)
                logger.debug(t("log.ai.buildintools_rm_get_succeeded_ok", p0=type(audio_data)))
                media_parts.append(MessageSegment.record(audio_data))
            except ValueError as e:
                logger.warning(t("log.ai.buildintools_rm_get_audio_id", audio_id=audio_id, e=e))
                if "找不到资源" in str(e):
                    return f"❌ 找不到资源ID: {audio_id}，可能已过期或ID不正确。"
                else:
                    return f"❌ 资源ID: {audio_id} 数据转换失败: {e}"

        if not success_img_ids and target_image_ids and not text and not video_id and not audio_id:
            return await _abort_send(";\n".join(failed_notes))

        # 检查出站安全插槽
        media_parts, success_img_ids = await filter_outbound_media_safety(ev, media_parts, success_img_ids)

        _at_raw = tool_ctx.extra["at_user_id"] if "at_user_id" in tool_ctx.extra else None
        _at_uid = str(_at_raw) if isinstance(_at_raw, str) and _at_raw else None
        from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

        _meta = turn_reply_metadata(ev)
        if _at_uid:
            _meta["reply_to_user_id"] = _at_uid
            if ev is None or _at_uid != str(ev.user_id):
                if "reply_to_user_name" in _meta:
                    del _meta["reply_to_user_name"]
        if text:
            from gsuid_core.ai_core.utils import send_chat_result

            _sent_registry = tool_ctx.extra["run_sent_texts"] if "run_sent_texts" in tool_ctx.extra else None
            if isinstance(_sent_registry, set) and text.strip() in _sent_registry:
                logger.info(t("log.ai.buildintools_skipping_duplicate_run_skip"))
                text = ""
            else:
                _mention_raw = tool_ctx.extra["mention_names"] if "mention_names" in tool_ctx.extra else None
                _mentions: dict[str, str] = {}
                if isinstance(_mention_raw, dict):
                    for _mk, _mv in _mention_raw.items():
                        if isinstance(_mk, str) and isinstance(_mv, str) and _mk and _mv:
                            _mentions[_mk] = _mv
                await send_chat_result(
                    bot,
                    text,
                    ev=ev,
                    ooc_check=False,
                    at_user_id=_at_uid,
                    mention_names=_mentions,
                    extra_metadata=_meta,
                )
                if isinstance(_sent_registry, set):
                    _sent_registry.add(text.strip())
                _at_uid = None

        if media_parts:
            from gsuid_core.ai_core.outbound import (
                topic_from_extra,
                set_outbound_image_label,
                reset_outbound_image_label,
                format_outbound_image_placeholder,
                ensure_outbound_topic_from_artifact,
            )

            for _iid in success_img_ids:
                await ensure_outbound_topic_from_artifact(tool_ctx.extra, _iid)
            _rep_id = success_img_ids[0] if success_img_ids else ""
            _label = format_outbound_image_placeholder(topic_from_extra(tool_ctx.extra), _rep_id)
            _tok = set_outbound_image_label(_label)
            _out = list(media_parts)
            if _at_uid:
                _out = [MessageSegment.at(_at_uid), *_out]
            try:
                await bot.send(_out if len(_out) > 1 else _out[0], extra_metadata=_meta)
                sent = True
            finally:
                reset_outbound_image_label(_tok)
        elif text:
            sent = True

        if sent:
            from gsuid_core.ai_core.history_format import note_thread_handle

            for hid in (*success_img_ids, video_id, audio_id):
                if hid:
                    note_thread_handle(tool_ctx, hid)
            if success_img_ids:
                extra["image_sent_this_run"] = True

        if throttle_key is not None:
            _PER_TURN_SEND_MESSAGE_COUNT[throttle_key] = _PER_TURN_SEND_MESSAGE_COUNT.get(throttle_key, 0) + 1

        content_desc = []
        if text:
            content_desc.append("文本")
        if success_img_ids:
            content_desc.append(f"图片({','.join(success_img_ids)})")
        if video_id:
            content_desc.append(f"视频({video_id})")
        if audio_id:
            content_desc.append(f"音频({audio_id})")
        logger.info(t("log.ai.buildintools_user_target_id", p0="+".join(content_desc), target_id=target_id))

        if text and tool_ctx.parent_session_id:
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            parent_session = get_ai_session_registry().get_ai_session(tool_ctx.parent_session_id)
            if parent_session is not None:
                parent_session.append_proactive_assistant_turn(
                    content=text,
                    source="tool",
                    trigger_reason="send_message_by_ai",
                )
        from gsuid_core.ai_core.outbound import record_outbound, topic_from_extra, write_decision_memo

        _topic = topic_from_extra(tool_ctx.extra)
        _tname = ""
        if ev is not None and ev.sender:
            raw_nick = ev.sender["nickname"] if "nickname" in ev.sender else None
            raw_card = ev.sender["card"] if "card" in ev.sender else None
            if isinstance(raw_nick, str) and raw_nick:
                _tname = raw_nick
            elif isinstance(raw_card, str) and raw_card:
                _tname = raw_card

        _bot_self = str(ev.bot_self_id) if ev is not None and ev.bot_self_id else ""
        if success_img_ids:
            for idx, _iid in enumerate(success_img_ids):
                await record_outbound(
                    ev=ev,
                    session_id=session_id,
                    text=text if idx == 0 else "",
                    image_id=_iid,
                    topic=_topic,
                    target_user=target_id,
                    target_name=_tname,
                )
                if tool_ctx.parent_session_id:
                    from gsuid_core.ai_core.session_registry import get_ai_session_registry

                    _sess = get_ai_session_registry().get_ai_session(tool_ctx.parent_session_id)
                    if _sess is not None and _sess._session_logger is not None:
                        _sess._session_logger.log_outbound_audit(
                            group_id=str(ev.group_id) if ev is not None and ev.group_id else "",
                            text=text if idx == 0 else "",
                            image_id=_iid,
                            topic=_topic,
                            target_user=target_id,
                        )
                await write_decision_memo(
                    bot_self_id=_bot_self,
                    text=f"已发 {(_topic or '图')[:12]} {_iid}".strip(),
                    ref=f"decision:send:{session_id}:{_iid}"[:160],
                    handle=_iid,
                    owner_user_id=target_id,
                )
        else:
            await record_outbound(
                ev=ev,
                session_id=session_id,
                text=text,
                image_id="",
                topic=_topic,
                target_user=target_id,
                target_name=_tname,
            )
            if tool_ctx.parent_session_id:
                from gsuid_core.ai_core.session_registry import get_ai_session_registry

                _sess = get_ai_session_registry().get_ai_session(tool_ctx.parent_session_id)
                if _sess is not None and _sess._session_logger is not None:
                    _sess._session_logger.log_outbound_audit(
                        group_id=str(ev.group_id) if ev is not None and ev.group_id else "",
                        text=text,
                        image_id="",
                        topic=_topic,
                        target_user=target_id,
                    )
            await write_decision_memo(
                bot_self_id=_bot_self,
                text=f"已发 {(_topic or '图')[:12]} {text[:20]}".strip(),
                ref=f"decision:send:{session_id}:t"[:160],
                handle="",
                owner_user_id=target_id,
            )

        res_msg = f"消息已发送给用户 {target_id}"
        if failed_notes:
            res_msg += f"（部分图片未发送：{'; '.join(failed_notes)}）"
        return res_msg

    except Exception as e:
        logger.exception(t("log.ai.buildintools_event", e=e))
        if sent:
            return f"发送失败：{str(e)}"
        return await _abort_send(f"发送失败：{str(e)}")
