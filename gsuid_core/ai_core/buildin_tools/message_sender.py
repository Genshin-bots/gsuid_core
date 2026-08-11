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

from typing import TYPE_CHECKING, Dict, List, Tuple, Union, Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Message
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
        if not p.exists():
            logger.debug(t("log.ai.buildintools_kanban_artifact_res", res_id=res_id, p0=art.payload_path))
            return None
        data = p.read_bytes()
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


@ai_tools(category="self")
async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    text: str = "",
    image_id: str = "",
    video_id: str = "",
    audio_id: str = "",
    user_id: Optional[str] = None,
) -> str:
    """
    主动发送消息给用户

    支持发送文本消息、图片消息、视频消息、音频消息，或组合发送。
    AI 可以任意传入 text 和/或 image_id 和/或 video_id 和/或 audio_id，系统会按顺序发送。

    **重要**：当其他工具返回 `[视频消息]`/`[图片消息]`/`[语音消息]` 等标记时，
    表示该媒体已由框架自动发送给用户，**无需再调用本工具重复发送**。
    本工具仅用于主动补充发送文字说明或追加媒体时使用。

    **资源 ID 必须来自上下文**：image_id / video_id / audio_id 只能填本轮对话中
    实际出现过的 ID（如 `img_xxxxxxxx`），**禁止自行构造或猜测**——凭空编造的 ID
    必然发送失败（§13 生产实录：编造 32 位 hex ID 被拒）。没有可用资源就只发 text。

    Args:
        ctx: 工具执行上下文（包含bot和ev对象）
        text: 文本内容，可选
        image_id: 图片资源ID，可选，格式通常为"res_xxxxxx"或"img_xxxxx"
        video_id: 视频资源ID，可选，格式通常为"video_xxxxxx"或"img_xxxxxx"
        audio_id: 音频资源ID，可选，格式通常为"aud_xxxxxxxx"
        user_id: 可选，目标用户ID，默认为事件关联的用户

    Returns:
        发送结果描述字符串

    Example:
        >>> await send_message_by_ai(ctx, text="你好！这是一条主动消息。")
        >>> await send_message_by_ai(ctx, text="提醒你...", user_id="123456")
        >>> await send_message_by_ai(ctx, image_id="res_abc123")
        >>> await send_message_by_ai(ctx, text="这是你要的视频！", video_id="img_abc123")
        >>> await send_message_by_ai(ctx, audio_id="aud_abc12345")
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

    if not text and not image_id and not video_id and not audio_id:
        return "发送失败：text、image_id、video_id 和 audio_id 至少提供一个"

    # 单轮节流：超过 PER_TURN_SEND_MESSAGE_LIMIT 直接拒发，把模型推回"正文输出"这条正道
    throttle_key = _get_send_throttle_key(ctx)
    if throttle_key is not None and _PER_TURN_SEND_MESSAGE_COUNT.get(throttle_key, 0) >= PER_TURN_SEND_MESSAGE_LIMIT:
        return (
            f"⚠️ 本轮你已用 send_message_by_ai 主动发过 {PER_TURN_SEND_MESSAGE_LIMIT} 条了。"
            "它**不是常规回复通道**——接下来想对用户说的话，**直接作为你的回复正文输出**即可"
            "（框架会自动发出，并自动处理换行分条 / 长文转图）。本轮请勿再调用本工具。"
        )

    # 统一输出闸门（尖括号 + OOC …）：打回则 return feedback，放行继续发
    if text:
        from gsuid_core.ai_core.output_gate import tool_gate_feedback

        _ev_text = tool_ctx.ev.raw_text if tool_ctx.ev is not None and tool_ctx.ev.raw_text else ""
        _gate_fb = tool_gate_feedback(text, tool_ctx.extra, user_text=_ev_text)
        if _gate_fb is not None:
            return _gate_fb

    # 目标用户（§E.3）：默认当前对话者；Event 保证 user_id 存在，不用 getattr 兜底
    ev = tool_ctx.ev
    target_id = user_id or (str(ev.user_id) if ev is not None else "")

    try:
        media_parts: List[Message] = []
        if image_id:
            # 资源ID（如 img_xxxxxxxx 走 RM，res_xxxxxxxx 走 Kanban artifact 后转 RM）
            if image_id.startswith("http") or image_id.startswith("base64://"):
                media_parts.append(MessageSegment.image(image_id))
            elif image_id.startswith("res_"):
                # Kanban artifact 句柄：从 AIAgentArtifact 解析 → 转 RM → 发送
                # 这一段是 §3.6 "主人格透明发送能力代理产物"的实现基础——主人格
                # 不需要知道 RM / artifact 是两套存储，只要拿到 res_xxx 句柄直接发。
                kanban_payload = await _resolve_kanban_artifact(image_id)
                if kanban_payload is None:
                    # 兜底：仍可能是用户上传时被框架登记成 RM 但前缀写成 res_ 的情况
                    logger.debug(
                        t(
                            "log.ai.buildintools_kanban_artifact_parsing_fail",
                            image_id=image_id,
                        )
                    )
                    try:
                        img_data = await RM.get(image_id)
                        media_parts.append(MessageSegment.image(img_data))
                    except ValueError as e:
                        logger.warning(t("log.ai.buildintools_rm_get_image_id", image_id=image_id, e=e))
                        if "找不到资源" in str(e):
                            # 交付校验（方案九）：句柄失效给出可执行出路——重委派渲染，
                            # 而不是死胡同文案让模型卡在原地或谎报已发。
                            return (
                                f"❌ 资源ID: {image_id} 无法解析（artifact 不存在或已过期，"
                                f"可能是渲染子任务未真正出图）。请重新 "
                                f'create_subagent(agent_profile="render_agent", task=原事实包) '
                                f"再委派一次出图；勿再发送该 ID，勿向用户声称已发图。"
                            )
                        return f"❌ 资源ID: {image_id} 数据转换失败: {e}"
                elif isinstance(kanban_payload, bytes):
                    # 文件类 artifact：转 RM 自动注册一次（便于后续重复发送），然后直接发 bytes
                    new_rm_id = RM.register(kanban_payload)
                    logger.info(
                        t(
                            "log.ai.buildintools_kanban_artifact_image",
                            image_id=image_id,
                            new_rm_id=new_rm_id,
                        )
                    )
                    media_parts.append(MessageSegment.image(kanban_payload))
                else:
                    # 文本 / 非图片 artifact（含落盘 markdown）：不能当 image 发
                    return (
                        f"❌ 资源ID: {image_id} 是文本类 Kanban artifact（非图片字节），"
                        f"请用 artifact_get('{image_id}') 取原文后："
                        f"短文用 text 参数发送，长文/多数据用 render_html_to_image 出图。"
                    )
            else:
                try:
                    logger.debug(t("log.ai.buildintools_calling_rm_get_2", image_id=image_id))
                    img_data = await RM.get(image_id)
                    logger.debug(t("log.ai.buildintools_rm_get_succeeded_ok_2", p0=type(img_data)))
                    media_parts.append(MessageSegment.image(img_data))
                except ValueError as e:
                    logger.warning(t("log.ai.buildintools_rm_get_image_id", image_id=image_id, e=e))
                    # 区分"资源不存在"和"资源转换失败"
                    if "找不到资源" in str(e):
                        return f"❌ 找不到资源ID: {image_id}，可能已过期或ID不正确。"
                    else:
                        return f"❌ 资源ID: {image_id} 数据转换失败: {e}"

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

        # 文本走统一 send_chat_result（剥 markdown / 长文转图 / 拆条 / @解析），别裸 bot.send
        # ooc_check=False：入口已 tool_gate_feedback（pre_send_gate）过，此处只做呈现归一化。
        _at_raw = tool_ctx.extra["at_user_id"] if "at_user_id" in tool_ctx.extra else None
        _at_uid = str(_at_raw) if isinstance(_at_raw, str) and _at_raw else None
        if text:
            from gsuid_core.ai_core.utils import send_chat_result

            # run 级发送去重（与 gs_agent 主循环共用 extra 里的同一集合）：干净历史重试 /
            # 模型重复调用不再把同一段话发两遍，媒体不受影响（评审修复 F14）
            _sent_registry = tool_ctx.extra["run_sent_texts"] if "run_sent_texts" in tool_ctx.extra else None
            if isinstance(_sent_registry, set) and text.strip() in _sent_registry:
                logger.info(t("log.ai.buildintools_skipping_duplicate_run_skip"))
                text = ""
            else:
                await send_chat_result(bot, text, ev=ev, ooc_check=False, at_user_id=_at_uid)
                if isinstance(_sent_registry, set):
                    _sent_registry.add(text.strip())
                _at_uid = None  # 文本已 @，媒体不再重复
        if media_parts:
            _out = list(media_parts)
            if _at_uid:
                _out = [MessageSegment.at(_at_uid), *_out]
            await bot.send(_out if len(_out) > 1 else _out[0])

        # 计数放在真正发出之后：媒体解析报错的早退不占额度
        if throttle_key is not None:
            _PER_TURN_SEND_MESSAGE_COUNT[throttle_key] = _PER_TURN_SEND_MESSAGE_COUNT.get(throttle_key, 0) + 1

        # 交付终局信号：**台词**已随工具发出（media-only 不算，留一句收尾额度）。
        # loop 据此把本 run 置为 delivered 终局态——交付后对用户只许 <SILENCE>，
        # 杜绝「任务已完成…」状态汇报 OOC（结构信号，非文本关键词判定）。
        if text:
            tool_ctx.extra["delivered_with_speech"] = True

        content_desc = []
        if text:
            content_desc.append("文本")
        if image_id:
            content_desc.append(f"图片({image_id})")
        if video_id:
            content_desc.append(f"视频({video_id})")
        if audio_id:
            content_desc.append(f"音频({audio_id})")
        logger.info(t("log.ai.buildintools_user_target_id", p0="+".join(content_desc), target_id=target_id))

        # §8.1：工具本质上仍然是"框架在 LLM run 外注入到用户会话"的主动输出
        # ——若拿得到调用方所在的主 session，把发出去的文本同步追加进该
        # session 的 pydantic_ai 历史，避免后续轮主人格"对自己刚发的话失忆"。
        # 仅同步文本（图 / 音 / 视频在 pydantic_ai 历史里没有合适的语义形态）。
        if text and tool_ctx.parent_session_id:
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            parent_session = get_ai_session_registry().get_ai_session(tool_ctx.parent_session_id)
            if parent_session is not None:
                parent_session.append_proactive_assistant_turn(
                    content=text,
                    source="tool",
                    trigger_reason="send_message_by_ai",
                )
        return f"消息已发送给用户 {target_id}"

    except Exception as e:
        logger.exception(t("log.ai.buildintools_event", e=e))
        return f"发送失败：{str(e)}"


@ai_tools(category="self")
async def set_session_reply_mute(
    ctx: RunContext[ToolContext],
    duration_minutes: int = 60,
    reason: str = "",
) -> str:
    """暂停本会话自动应答一段时间（框架静默，非角色扮演）。

    静默期内非主人消息不会进入主 Agent；主人硬触发会自动解除静默。
    用于用户明确要求「别回消息 / 休息 N 小时」等场景。

    Args:
        ctx: 工具上下文
        duration_minutes: 静默分钟数，1～240
        reason: 可选备注（仅日志）
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None or not ev.session_id:
        return "设置失败：无会话信息"
    mins = max(1, min(int(duration_minutes), 240))
    from gsuid_core.ai_core.session_mute import set_session_mute

    until = set_session_mute(ev.session_id, float(mins * 60))
    logger.info(
        t(
            "log.ai.session_mute_set_session",
            session=ev.session_id,
            minutes=mins,
            reason=(reason or "")[:80],
            until=until,
        )
    )
    return f"✅ 已设置本会话静默 {mins} 分钟（框架层，到期自动恢复）。"


@ai_tools(category="self")
async def clear_session_reply_mute(ctx: RunContext[ToolContext]) -> str:
    """立即解除本会话的框架静默窗口。"""
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None or not ev.session_id:
        return "解除失败：无会话信息"
    from gsuid_core.ai_core.session_mute import clear_session_mute

    if clear_session_mute(ev.session_id):
        return "✅ 已解除本会话静默。"
    return "ℹ️ 当前本会话未处于静默。"
