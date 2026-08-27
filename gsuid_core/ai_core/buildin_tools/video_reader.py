"""惰性视频读取：入站只透传 ``vid_xxxxxxxx``，Agent 按需调用本工具取回。

仅当前模型声明 video 时才挂本工具。投喂走 ``_prepare_video_content``
（Gemini Files / OpenAI file；Files 缺失时抽帧）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.messages import UserContent, BinaryContent

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import VIDEO_MAX_MB, fetch_video_bytes
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.buildin_tools.visibility import context_has_video

if TYPE_CHECKING:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

_UNREADABLE = "当前模型无法读取该视频内容。用户发送了视频，但本模型不能查看。"


def sniff_video_mime(data: bytes) -> str | None:
    """按文件头推断视频 MIME；识别不出返回 None（禁止默认 video/mp4）。"""
    if not data or len(data) < 3:
        return None
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if data[8:12] == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    if data[:3] == b"FLV":
        return "video/x-flv"
    return None


async def resolve_video_bytes(video_id: str) -> tuple[bytes | None, str | None]:
    """视频 ID / URL → 字节。失败时第二项是给 Agent 的中文说明。"""
    raw = video_id.strip()
    if not raw:
        return None, "❌ video_id 不能为空"
    if raw.startswith("base64://"):
        raw = "data:video/mp4;base64," + raw[9:]
    if raw.startswith(("http://", "https://", "data:")):
        try:
            data, _mime = await fetch_video_bytes(raw)
        except RuntimeError as e:
            return None, f"❌ 视频读取失败: {e}"
        return data, None
    try:
        data = await RM.get(raw)
    except ValueError as e:
        if "找不到资源" in str(e):
            return None, f"❌ 找不到视频资源: {raw}（可能已过期或 ID 不正确）"
        return None, f"❌ 视频资源 {raw} 读取失败: {e}"
    limit = VIDEO_MAX_MB * 1024 * 1024
    if len(data) > limit:
        return None, f"❌ 视频体积超过上限 {VIDEO_MAX_MB}MB"
    return data, None


def _agent_for_session(parent_session_id: str | None) -> GsCoreAIAgent | None:
    if not parent_session_id:
        return None
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    return get_ai_session_registry().get_ai_session(parent_session_id)


async def load_video_for_agent(
    video_id: str,
    agent: GsCoreAIAgent,
) -> tuple[str, list[UserContent] | None]:
    """取回视频并按当前 Agent 模型转换成可注入内容。"""
    data, err = await resolve_video_bytes(video_id)
    if err:
        return err, None
    if data is None:
        return f"❌ 找不到视频资源: {video_id}", None
    mime = sniff_video_mime(data)
    if mime is None:
        return (
            f"❌ `{video_id}` 的内容不是可识别的视频（文件头无法识别），"
            "read_video 拒绝注入多模态，以免服务商 400 打死本轮。",
            None,
        )
    video = BinaryContent(data=data, media_type=mime)
    out = await agent._prepare_video_content([video], ["text", "image", "video"])
    texts: list[str] = []
    media: list[UserContent] = []
    for item in out:
        if isinstance(item, str):
            texts.append(item)
        else:
            media.append(item)
    if not media:
        note = "\n".join(texts).strip() if texts else _UNREADABLE
        return note, None
    shown = f"🎬 视频[{video_id}]已直接呈现给你，请直接查看后作答。"
    if texts:
        shown = "\n".join(texts) + "\n" + shown
    return shown, media


@ai_tools(category="buildin", visible_when=context_has_video, timeout=320.0)
async def read_video(
    ctx: RunContext[ToolContext],
    video_id: str,
) -> str | ToolReturn:
    """
    查看（读取）一条视频的内容

    用户发来的视频不会直接进入你的视野，只会以「视频ID」(形如 ``vid_xxxxxxxx``)
    的形式出现在消息里。需要看清视频里有什么时，调用本工具把它读出来。

    本工具仅在当前模型声明支持 video 时才会出现。能原生吃视频时把视频塞回会话；
    ``/v1/files`` 不可用且能看图时抽帧。不要对用户编造视频内容。

    Args:
        ctx: 工具执行上下文
        video_id: 视频资源ID。支持消息里的 ``vid_xxxxxxxx``，以及
            ``http(s)://`` / ``base64://`` / ``data:video/`` 直链。

    Returns:
        能投喂时返回 ``ToolReturn``（视频或抽帧图片注入会话）；否则返回中文说明。
        资源不存在 / 已过期 / 超体积 / 非视频字节时返回错误说明。

    Example:
        >>> await read_video(ctx, "vid_1a2b3c4d")
    """
    sess = _agent_for_session(ctx.deps.parent_session_id)
    if sess is None:
        return "❌ 当前会话不可用，无法读取视频。"
    note, media = await load_video_for_agent(video_id, sess)
    if media is None:
        logger.info(t("log.ai.buildintools_read_video_unread", video_id=video_id))
        return note
    logger.info(t("log.ai.buildintools_read_video_direct", video_id=video_id))
    return ToolReturn(return_value=note, content=media)
