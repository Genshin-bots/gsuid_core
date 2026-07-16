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
from gsuid_core.ai_core import output_firewall
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


async def _resolve_kanban_artifact(res_id: str) -> Optional[Union[bytes, str]]:
    """尝试把一个 ``res_xxx`` 句柄解析成可发送的图片数据。

    走 ``AIAgentArtifact.get_by_id``——找到 artifact 后：
    - 优先读 ``payload_path``（落盘 ≥4KB 大工件）→ 返回文件 bytes
    - 否则读 ``payload_inline``（≤4KB inline 文本）→ 多为代码 / 文本，无法当图片发，
      返回 None 让上层退回 RM 链路

    找不到 artifact / 读文件失败时返回 None；不抛异常，避免上层 try-except 兜底。
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
    if art.payload_path:
        p = Path(art.payload_path)
        if p.exists():
            return p.read_bytes()
        logger.debug(
            t("🧠 [BuildinTools] Kanban artifact {res_id} 落盘路径不存在: {p0}", res_id=res_id, p0=art.payload_path)
        )
        return None
    if art.payload_inline:
        # inline payload 通常是 ≤4KB 文本（代码 / JSON 摘要），不是图片字节
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
        logger.warning(t("🧠 [BuildinTools] send_message_by_ai: Bot对象为空，无法发送消息"))
        return "发送失败：Bot对象不可用"

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

    # 出戏防火墙（§D.4）：同轮首次命中 return 警告让模型重写重发；重写后仍命中则放行
    if text and output_firewall.is_enabled():
        _ev_text = tool_ctx.ev.raw_text if tool_ctx.ev is not None and tool_ctx.ev.raw_text else ""
        warning = output_firewall.gate_warn_once(tool_ctx.extra, text, user_text=_ev_text)
        if warning is not None:
            return warning

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
                            "🧠 [BuildinTools] Kanban artifact 解析失败，回退尝试 RM.get('{image_id}')",
                            image_id=image_id,
                        )
                    )
                    try:
                        img_data = await RM.get(image_id)
                        media_parts.append(MessageSegment.image(img_data))
                    except ValueError as e:
                        logger.warning(
                            t("🧠 [BuildinTools] RM.get({image_id}) 抛出 ValueError: {e}", image_id=image_id, e=e)
                        )
                        if "找不到资源" in str(e):
                            return (
                                f"❌ 找不到资源ID: {image_id}（既不在 Kanban artifact 表，"
                                f"也不在 RM 临时资源池）。可能 ID 错了 / artifact 已过期 / "
                                f"代理执行未实际登记 artifact——请确认。"
                            )
                        return f"❌ 资源ID: {image_id} 数据转换失败: {e}"
                elif isinstance(kanban_payload, bytes):
                    # 文件类 artifact：转 RM 自动注册一次（便于后续重复发送），然后直接发 bytes
                    new_rm_id = RM.register(kanban_payload)
                    logger.info(
                        t(
                            "🧠 [BuildinTools] send_message_by_ai: Kanban artifact"
                            " {image_id} → 自动注册成 RM 资源 {new_rm_id}",
                            image_id=image_id,
                            new_rm_id=new_rm_id,
                        )
                    )
                    media_parts.append(MessageSegment.image(kanban_payload))
                else:
                    # inline 文本 artifact：不是图片，提示主人格用 text 参数发
                    return (
                        f"❌ 资源ID: {image_id} 是 Kanban inline 文本 artifact（非图片字节），"
                        f"请用 artifact_get({image_id}) 取原文后用 text 参数发送。"
                    )
            else:
                try:
                    logger.debug(t("🧠 [BuildinTools] 调用 RM.get('{image_id}')", image_id=image_id))
                    img_data = await RM.get(image_id)
                    logger.debug(t("🧠 [BuildinTools] RM.get 成功, img_data type={p0}", p0=type(img_data)))
                    media_parts.append(MessageSegment.image(img_data))
                except ValueError as e:
                    logger.warning(
                        t("🧠 [BuildinTools] RM.get({image_id}) 抛出 ValueError: {e}", image_id=image_id, e=e)
                    )
                    # 区分"资源不存在"和"资源转换失败"
                    if "找不到资源" in str(e):
                        return f"❌ 找不到资源ID: {image_id}，可能已过期或ID不正确。"
                    else:
                        return f"❌ 资源ID: {image_id} 数据转换失败: {e}"

        if video_id:
            try:
                logger.debug(t("🧠 [BuildinTools] 调用 RM.get('{video_id}')", video_id=video_id))
                video_data = await RM.get(video_id)
                logger.debug(t("🧠 [BuildinTools] RM.get 成功, video_data type={p0}", p0=type(video_data)))
                media_parts.append(MessageSegment.video(video_data))
            except ValueError as e:
                logger.warning(t("🧠 [BuildinTools] RM.get({video_id}) 抛出 ValueError: {e}", video_id=video_id, e=e))
                if "找不到资源" in str(e):
                    return f"❌ 找不到资源ID: {video_id}，可能已过期或ID不正确。"
                else:
                    return f"❌ 资源ID: {video_id} 数据转换失败: {e}"

        if audio_id:
            try:
                logger.debug(t("🧠 [BuildinTools] 调用 RM.get('{audio_id}')", audio_id=audio_id))
                audio_data = await RM.get(audio_id)
                logger.debug(t("🧠 [BuildinTools] RM.get 成功, audio_data type={p0}", p0=type(audio_data)))
                media_parts.append(MessageSegment.record(audio_data))
            except ValueError as e:
                logger.warning(t("🧠 [BuildinTools] RM.get({audio_id}) 抛出 ValueError: {e}", audio_id=audio_id, e=e))
                if "找不到资源" in str(e):
                    return f"❌ 找不到资源ID: {audio_id}，可能已过期或ID不正确。"
                else:
                    return f"❌ 资源ID: {audio_id} 数据转换失败: {e}"

        # 文本走统一 send_chat_result（剥 markdown / 长文转图 / 拆条 / @解析），别裸 bot.send
        # 把 **加粗** 刷进群；ooc_check=False：入口已 gate_warn_once 过，这里只做归一化。
        if text:
            from gsuid_core.ai_core.utils import send_chat_result

            # run 级发送去重（与 gs_agent 主循环共用 extra 里的同一集合）：干净历史重试 /
            # 模型重复调用不再把同一段话发两遍，媒体不受影响（评审修复 F14）
            _sent_registry = tool_ctx.extra["run_sent_texts"] if "run_sent_texts" in tool_ctx.extra else None
            if isinstance(_sent_registry, set) and text.strip() in _sent_registry:
                logger.info(t("🧠 [BuildinTools] 相同文本本轮已发送过，跳过重复发送（run 级去重）"))
                text = ""
            else:
                await send_chat_result(bot, text, ev=ev, ooc_check=False)
                if isinstance(_sent_registry, set):
                    _sent_registry.add(text.strip())
        if media_parts:
            await bot.send(media_parts if len(media_parts) > 1 else media_parts[0])

        # 计数放在真正发出之后：媒体解析报错的早退不占额度
        if throttle_key is not None:
            _PER_TURN_SEND_MESSAGE_COUNT[throttle_key] = _PER_TURN_SEND_MESSAGE_COUNT.get(throttle_key, 0) + 1

        content_desc = []
        if text:
            content_desc.append("文本")
        if image_id:
            content_desc.append(f"图片({image_id})")
        if video_id:
            content_desc.append(f"视频({video_id})")
        if audio_id:
            content_desc.append(f"音频({audio_id})")
        logger.info(t("🧠 [BuildinTools] 发送 {p0} 给用户 {target_id}", p0="+".join(content_desc), target_id=target_id))

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
        logger.exception(t("🧠 [BuildinTools] send_message_by_ai 发送消息失败: {e}", e=e))
        return f"发送失败：{str(e)}"
