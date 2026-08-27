"""CaptureBot：出站入队，不走 ``super().send`` / ``send_bytes``。

可见台词有两条路径：

- **流式**：``enqueue_text_delta`` 在 pydantic-ai ``TextPartDelta`` 到达时入队，
  供 SSE ``text`` 帧追加。不写 history（CallTools 过闸后再 ``commit_streamed_history``）。
- **整段**：``send`` / ``send_chat_result``。闸门通过后 ``take_unsent_suffix``：
  已全部推过则只记 history；只推了前缀则补后缀；否则整段 ``send``。
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Union, Literal, Optional

from gsuid_core.bot import Bot, _Bot, message_list_to_str
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.message_models import ButtonType
from gsuid_core.ai_core.http_agent.types import AttachmentKind, AttachmentEncoding
from gsuid_core.ai_core.http_agent.config import ATTACHMENT_RUN_MAX, ATTACHMENT_FRAME_MAX


class CaptureItem:
    """队列元素：text / attachment / overflow 标记。"""

    __slots__ = ("kind", "text", "encoding", "mime", "data", "nbytes", "att_kind")

    def __init__(
        self,
        kind: str,
        *,
        text: str = "",
        encoding: AttachmentEncoding = "base64",
        mime: str = "image/png",
        data: str = "",
        nbytes: int = 0,
        att_kind: AttachmentKind = "image",
    ) -> None:
        self.kind = kind
        self.text = text
        self.encoding = encoding
        self.mime = mime
        self.data = data
        self.nbytes = nbytes
        self.att_kind = att_kind


def _is_image_string(s: str) -> bool:
    return s.startswith("base64://") or s.startswith("data:image/") or s.startswith("link://")


def _b64_payload(s: str) -> tuple[str, str, int]:
    if s.startswith("base64://"):
        raw = s[9:]
        return "image/png", raw, (len(raw) * 3) // 4
    if s.startswith("data:image/"):
        header, _, body = s.partition(",")
        mime = "image/png"
        if header.startswith("data:") and ";base64" in header:
            mime_part = header[5:].split(";", 1)[0]
            if mime_part:
                mime = mime_part
        return mime, body, (len(body) * 3) // 4
    if s.startswith("link://"):
        return "image/png", s[7:], len(s)
    return "image/png", s, len(s)


# 流式 delta 合批：过短就等下一块，避免一 token 一帧打爆队列。
_DELTA_FLUSH_CHARS = 8


class CaptureBot(Bot):
    """只覆盖 ``send`` / ``send_option``；主人 DM 走 ``target_send`` 不入队。"""

    def __init__(self, bot: _Bot, ev: Event, queue: asyncio.Queue[CaptureItem]) -> None:
        super().__init__(bot, ev)
        self._cap_queue = queue
        self._out_bytes = 0
        self._overflow = False
        self._streamed_text = ""
        self._delta_buf = ""
        self._hold = ""

    def reset_text_stream(self) -> None:
        """丢掉未对齐的合批缓冲（新 ModelRequest，或本段 leftover 刚入 SSE）。"""
        self._streamed_text = ""
        self._delta_buf = ""
        self._hold = ""

    def has_queued_text(self) -> bool:
        return bool(self._streamed_text)

    def enqueue_text_delta(self, piece: str) -> None:
        """把模型可见文本增量推进 SSE 队列（不写 history）。"""
        if not piece:
            return
        from gsuid_core.ai_core.utils import is_silence_marker, split_protocol_hold

        if is_silence_marker(piece) and not self._hold and not self._delta_buf:
            return
        visible, self._hold = split_protocol_hold(self._hold + piece, force=False)
        if not visible:
            return
        self._delta_buf += visible
        if len(self._delta_buf) >= _DELTA_FLUSH_CHARS:
            self._try_flush_delta()

    def _try_flush_delta(self) -> None:
        if not self._delta_buf:
            return
        item = CaptureItem("text", text=self._delta_buf)
        try:
            self._cap_queue.put_nowait(item)
        except asyncio.QueueFull:
            # 不标 overflow：留给 flush_text_delta 阻塞推完。
            return
        self._streamed_text += self._delta_buf
        self._delta_buf = ""

    async def flush_text_delta(self) -> None:
        """流结束时把合批残余推进队列；队列满则背压等待。"""
        from gsuid_core.ai_core.utils import split_protocol_hold

        vis, _ = split_protocol_hold(self._hold, force=True)
        self._hold = ""
        chunk = self._delta_buf + vis
        self._delta_buf = ""
        if not chunk:
            return
        item = CaptureItem("text", text=chunk)
        try:
            self._cap_queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                await self._cap_queue.put(item)
            except asyncio.CancelledError:
                self._delta_buf = chunk
                raise
        self._streamed_text += chunk

    def consume_streamed_text(self, text: str) -> bool:
        """已入 SSE 的缓冲是否完整覆盖 ``text``。调用前须先 ``flush_text_delta``。"""
        raw = self._streamed_text
        if not raw:
            return False
        if not text:
            if not raw.strip():
                self._streamed_text = ""
                return True
            return False
        stripped = raw.lstrip()
        lead = len(raw) - len(stripped)
        if stripped.startswith(text):
            self._streamed_text = raw[lead + len(text) :].lstrip()
            return True
        return False

    def take_unsent_suffix(self, text: str) -> str | None:
        """闸门通过后：``None`` 已全部在 SSE；否则返回仍需入队的后缀或全文。"""
        raw = self._streamed_text
        # 未入队残余改由返回值负责，避免 cancel 恢复的 buf 再被 flush 重复推
        self._delta_buf = ""
        self._hold = ""
        if not raw:
            return text
        stripped = raw.lstrip()
        if not text:
            return text
        if stripped.startswith(text):
            self._streamed_text = stripped[len(text) :].lstrip()
            return None
        if text.startswith(stripped):
            self._streamed_text = ""
            rest = text[len(stripped) :]
            return rest if rest else None
        self._streamed_text = ""
        return None

    def discard_streamed_preview(self, text: str = "") -> None:
        """闸门拒绝：只丢掉与 text 对齐的预览前缀。空或对不上则保留后续 part。"""
        if not text:
            return
        if self.consume_streamed_text(text):
            self._delta_buf = ""
            self._hold = ""

    async def commit_streamed_history(
        self,
        text: str,
        extra_metadata: Optional[Dict[str, object]] = None,
    ) -> None:
        """delta 已出站：只记 history / observe，不再入 SSE。"""
        if not text:
            return
        await self._record_history(text, extra_metadata)
        await self._observe_outbound(text)

    async def send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        extra_metadata: Optional[Dict[str, object]] = None,
        wait_recall: bool = False,
    ) -> Optional[List[str]]:
        await self._record_history(message, extra_metadata)
        await self._observe_outbound(message)
        await self._enqueue_visible(message)
        return None

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        target_type: Literal["group", "direct", "channel", "sub_channel"],
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = "",
        send_source_group: Optional[str] = None,
        wait_recall: bool = False,
    ) -> Optional[List[str]]:
        # 主人 DM 走 target_send；HTTP 面无适配器，不入 SSE 队列
        return None

    async def receive_resp(
        self,
        reply: Optional[Union[Message, List[Message], List[str], str, bytes]] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        is_mutiply: bool = False,
        is_recive: bool = True,
        timeout: float = 60,
        sep: str = "\n",
        command_tips: str = "请输入以下命令之一:",
        command_start_text: str = "",
        at_sender: bool = False,
    ) -> Optional[Event]:
        # HTTP 面没有 handle_event 回灌，等下去只会心跳到超时
        if reply is not None:
            await self.send(reply)
        raise RuntimeError("HTTP Agent does not support interactive wait")

    async def send_option(
        self,
        reply: Optional[Union[Message, List[Message], List[str], str, bytes]] = None,
        option_list: Optional[ButtonType] = None,
        unsuported_platform: bool = False,
        sep: str = "\n",
        command_tips: str = "请输入以下命令之一:",
        command_start_text: str = "",
        at_sender: bool = False,
    ) -> None:
        if reply is not None:
            await self.send(reply)

    def _put(self, item: CaptureItem) -> None:
        try:
            self._cap_queue.put_nowait(item)
        except asyncio.QueueFull:
            self._overflow = True

    async def _enqueue_visible(self, message: Union[Message, List[Message], str, bytes, List[str]]) -> None:
        from gsuid_core.ai_core.utils import is_silence_marker

        parts: List[Union[Message, str, bytes]]
        if isinstance(message, list):
            parts = list(message)
        else:
            parts = [message]
        for part in parts:
            if isinstance(part, bytes):
                await self._put_attachment("image/png", part, "image")
                continue
            if isinstance(part, str):
                if _is_image_string(part):
                    mime, payload, nbytes = _b64_payload(part)
                    encoding: AttachmentEncoding = "url" if part.startswith("link://") else "base64"
                    await self._put_attachment_encoded(mime, payload, nbytes, encoding, "image")
                elif not is_silence_marker(part):
                    if part:
                        self._put(CaptureItem("text", text=part))
                continue
            if isinstance(part, Message):
                if part.type == "image" and part.data is not None:
                    data = part.data
                    if isinstance(data, bytes):
                        await self._put_attachment("image/png", data, "image")
                    elif isinstance(data, str):
                        mime, payload, nbytes = _b64_payload(data)
                        encoding = "url" if data.startswith("link://") else "base64"
                        await self._put_attachment_encoded(mime, payload, nbytes, encoding, "image")
                elif part.type == "text" and part.data is not None:
                    text = str(part.data)
                    if text and not is_silence_marker(text):
                        self._put(CaptureItem("text", text=text))
                elif part.type in ("file", "record", "video") and part.data is not None:
                    raw = part.data
                    if isinstance(raw, bytes):
                        await self._put_attachment("application/octet-stream", raw, "file")
                    elif isinstance(raw, str):
                        mime, payload, nbytes = _b64_payload(raw)
                        await self._put_attachment_encoded(mime, payload, nbytes, "base64", "file")

    async def _put_attachment(self, mime: str, raw: bytes, kind: AttachmentKind) -> None:
        import base64

        encoded = base64.b64encode(raw).decode("ascii")
        await self._put_attachment_encoded(mime, encoded, len(raw), "base64", kind)

    async def _put_attachment_encoded(
        self,
        mime: str,
        payload: str,
        nbytes: int,
        encoding: AttachmentEncoding,
        kind: AttachmentKind,
    ) -> None:
        use_encoding: AttachmentEncoding = encoding
        data = payload
        if encoding == "base64" and nbytes > ATTACHMENT_FRAME_MAX:
            use_encoding = "omitted"
            data = ""
        if self._out_bytes + nbytes > ATTACHMENT_RUN_MAX:
            use_encoding = "omitted"
            data = ""
            self._overflow = True
        else:
            self._out_bytes += nbytes
        self._put(
            CaptureItem(
                "attachment",
                encoding=use_encoding,
                mime=mime,
                data=data,
                nbytes=nbytes,
                att_kind=kind,
            )
        )

    async def _record_history(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        extra_metadata: Optional[Dict[str, object]],
    ) -> None:
        from gsuid_core.ai_core.utils import is_silence_marker
        from gsuid_core.message_history import get_history_manager
        from gsuid_core.ai_core.outbound import get_outbound_image_label

        content = ""
        metadata: Dict[str, object] = {}
        if isinstance(message, str):
            if message.startswith("base64://"):
                content = get_outbound_image_label() or "[图片]"
                metadata["type"] = "base64_image"
            else:
                content = message
        elif isinstance(message, bytes):
            content = "[图片/文件]"
            metadata["type"] = "bytes"
        elif isinstance(message, list):
            text_parts: List[str] = []
            image_count = 0
            for msg in message:
                if isinstance(msg, Message):
                    if msg.type == "text":
                        text_parts.append(str(msg.data))
                    elif msg.type == "image":
                        image_count += 1
                    else:
                        text_parts.append(f"[{msg.type}]")
                elif isinstance(msg, str):
                    text_parts.append(msg)
            content = " ".join(text_parts)
            if image_count > 0:
                metadata["image_count"] = image_count
                label = get_outbound_image_label()
                if label:
                    content = f"{content} {label}".strip() if content else label
                elif not content:
                    content = "[图片]"
        elif isinstance(message, Message):
            if message.type == "text":
                content = str(message.data)
            elif message.type == "image":
                content = get_outbound_image_label() or "[图片]"
                metadata["type"] = "image"
            else:
                content = f"[{message.type}]"
        if extra_metadata:
            metadata.update(extra_metadata)
        if content and not is_silence_marker(content):
            get_history_manager().add_message(
                event=self.ev,
                role="assistant",
                content=content,
                user_name="AI",
                metadata=metadata,
            )

    async def _observe_outbound(self, message: Union[Message, List[Message], str, bytes, List[str]]) -> None:
        from gsuid_core.ai_core.memory.config import memory_config
        from gsuid_core.ai_core.configs.ai_config import ai_config

        enable_ai = bool(ai_config.get_config("enable").data)
        is_enable_memory = bool(ai_config.get_config("enable_memory").data)
        memory_mode = memory_config.memory_mode
        if not (enable_ai and is_enable_memory and "主动会话" in memory_mode):
            return
        from gsuid_core.ai_core.memory.observer import observe

        if isinstance(message, str):
            body = message
        elif isinstance(message, bytes):
            body = "[图片/文件]"
        elif isinstance(message, Message):
            body = message_list_to_str([message])
        else:
            msgs: List[Message] = []
            for item in message:
                if isinstance(item, Message):
                    msgs.append(item)
                elif isinstance(item, str):
                    msgs.append(Message(type="text", data=item))
            body = message_list_to_str(msgs) if msgs else ""
        if not body.strip():
            return
        try:
            task = asyncio.create_task(
                observe(
                    content=body,
                    speaker_id=f"__assistant_{self.ev.bot_id}__",
                    group_id=self.ev.group_id if self.ev.group_id else None,
                    bot_self_id=self.ev.bot_self_id,
                    observer_blacklist=memory_config.observer_blacklist,
                    message_type="group_msg" if self.ev.group_id else "private_msg",
                    bot_id=self.ev.bot_id,
                )
            )
            self.bot._add_bg_task(task)
        except Exception as e:
            logger.debug(t("log.ai.http_agent_observe_fail", e=e))
