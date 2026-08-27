"""OpenAI 兼容视频 file 块：inline base64、/v1/files、未声明 video 才抽帧。"""

from __future__ import annotations

import httpx
import pytest
from openai import APIStatusError
from pydantic_ai.messages import ImageUrl, UploadedFile, BinaryContent
from openai.types.file_purpose import FilePurpose

from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
from gsuid_core.ai_core.multimodal.openai_files import (
    INLINE_VIDEO_MAX_BYTES,
    OpenAIVideoHow,
    OpenAIFilesClient,
    OpenAIChatModelWithVideo,
    video_filename,
    video_delivery_mode,
    upload_openai_video_file,
    openai_chat_inline_video_part,
    video_bytes_to_openai_content,
    openai_chat_uploaded_video_part,
)


class _Created:
    def __init__(self, file_id: str) -> None:
        self.id = file_id


class _Files:
    def __init__(self, status: int, file_id: str = "file-abc") -> None:
        self._status = status
        self._file_id = file_id

    async def create(self, *, file: tuple[str, bytes, str], purpose: FilePurpose) -> _Created:
        req = httpx.Request("POST", "http://example.com/v1/files")
        resp = httpx.Response(self._status, request=req)
        if self._status in (404, 405, 501) or self._status >= 400:
            raise APIStatusError("files endpoint", response=resp, body=None)
        return _Created(self._file_id)


class _Client:
    def __init__(self, status: int, file_id: str = "file-abc") -> None:
        self._files = _Files(status, file_id)

    @property
    def files(self) -> _Files:
        return self._files


def test_video_delivery_mode_matrix() -> None:
    assert video_delivery_mode(supports_video=True, supports_image=True, provider="gemini") == "gemini"
    assert video_delivery_mode(supports_video=True, supports_image=False, provider="openai") == "openai_file"
    assert video_delivery_mode(supports_video=False, supports_image=True, provider="openai") == "frames"
    assert video_delivery_mode(supports_video=False, supports_image=True, provider="gemini") == "frames"
    assert video_delivery_mode(supports_video=True, supports_image=True, provider="anthropic") == "frames"
    assert video_delivery_mode(supports_video=False, supports_image=False, provider="openai") == "unavailable"
    assert video_delivery_mode(supports_video=True, supports_image=False, provider="anthropic") == "unavailable"


def test_video_filename() -> None:
    assert video_filename("video/mp4") == "clip.mp4"
    assert video_filename("video/webm") == "clip.webm"
    assert video_filename("video/quicktime") == "clip.mov"
    assert video_filename("application/octet-stream") == "clip.mp4"


def test_openai_chat_inline_video_part_shape() -> None:
    item = BinaryContent(data=b"abc", media_type="video/mp4")
    part = openai_chat_inline_video_part(item)
    assert part["type"] == "file"
    file_body = part["file"]
    assert "file_data" in file_body
    assert file_body["file_data"].startswith("data:video/mp4;base64,")
    assert "filename" in file_body
    assert file_body["filename"] == "clip.mp4"
    assert "format" in file_body
    assert file_body["format"] == "video/mp4"


def test_openai_chat_uploaded_video_part_shape() -> None:
    item = UploadedFile(
        file_id="file-xyz",
        provider_name="openai",
        media_type="video/mp4",
        vendor_metadata={"filename": "lecture.mp4", "format": "video/mp4"},
    )
    part = openai_chat_uploaded_video_part(item)
    assert part["type"] == "file"
    file_body = part["file"]
    assert "file_id" in file_body
    assert file_body["file_id"] == "file-xyz"
    assert "filename" in file_body
    assert file_body["filename"] == "lecture.mp4"
    assert "format" in file_body
    assert file_body["format"] == "video/mp4"


@pytest.mark.anyio
async def test_inline_small_video_skips_files_api() -> None:
    data = b"tiny-mp4"
    content, how = await video_bytes_to_openai_content(data, "video/mp4", _Client(200))
    assert how == "inline"
    assert isinstance(content, BinaryContent)
    assert content.data == data


@pytest.mark.anyio
async def test_disallow_inline_uploads_small_video() -> None:
    data = b"tiny-mp4"
    content, how = await video_bytes_to_openai_content(data, "video/mp4", _Client(200, "file-resp"), allow_inline=False)
    assert how == "uploaded"
    assert isinstance(content, UploadedFile)
    assert content.file_id == "file-resp"


@pytest.mark.anyio
async def test_large_video_uploads_file_id() -> None:
    data = b"x" * (INLINE_VIDEO_MAX_BYTES + 1)
    content, how = await video_bytes_to_openai_content(data, "video/mp4", _Client(200, "file-big"))
    assert how == "uploaded"
    assert isinstance(content, UploadedFile)
    assert content.file_id == "file-big"
    assert content.provider_name == "openai"
    assert content.media_type == "video/mp4"


@pytest.mark.anyio
async def test_large_video_files_missing_returns_none() -> None:
    data = b"x" * (INLINE_VIDEO_MAX_BYTES + 1)
    content, how = await video_bytes_to_openai_content(data, "video/mp4", _Client(404))
    assert how == "files_missing"
    assert content is None


@pytest.mark.anyio
async def test_inline_cap_is_twelve_mb() -> None:
    at_cap = b"x" * INLINE_VIDEO_MAX_BYTES
    content, how = await video_bytes_to_openai_content(at_cap, "video/mp4", _Client(200))
    assert how == "inline"
    assert isinstance(content, BinaryContent)


@pytest.mark.anyio
async def test_upload_http_error_raises() -> None:
    with pytest.raises(RuntimeError):
        await upload_openai_video_file(_Client(500), b"x", "video/mp4", "clip.mp4")


@pytest.mark.anyio
async def test_chat_model_maps_video_binary_to_file_part() -> None:
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    model = OpenAIChatModelWithVideo(
        "gemini-2.5-flash",
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(api_key="sk-test", base_url="http://127.0.0.1:9/v1"),
        ),
    )
    item = BinaryContent(data=b"abc", media_type="video/mp4")
    mapped = await model._map_content_item(item)
    assert mapped is not None
    assert mapped["type"] == "file"
    wire = dict(mapped["file"])
    assert "file_data" in wire
    assert str(wire["file_data"]).startswith("data:video/mp4;base64,")
    assert wire["filename"] == "clip.mp4"
    assert wire["format"] == "video/mp4"


@pytest.mark.anyio
async def test_prepare_video_openai_keeps_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.configs import models as models_mod
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    monkeypatch.setattr(models_mod, "get_provider_for_task", lambda _level: "openai")
    model = OpenAIChatModelWithVideo(
        "gemini-2.5-flash",
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(api_key="sk-test", base_url="http://127.0.0.1:9/v1"),
        ),
    )
    agent = GsCoreAIAgent(
        openai_chat_model=model,
        session_id="ut_video_openai",
        create_by="TEST",
        max_history=8,
        max_tokens=64,
        system_prompt="x",
    )
    video = BinaryContent(data=b"abc", media_type="video/mp4")
    out = await agent._prepare_video_content([video], "text,image,video")
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)


@pytest.mark.anyio
async def test_prepare_video_without_video_support_samples_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.configs import models as models_mod
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.multimodal import frame_extract

    async def _fake_frames(
        video_data: bytes,
        video_format: str = "mp4",
        interval_seconds: float = 2.0,
        max_frames: int = 0,
    ) -> list[bytes]:
        return [b"jpeg-one", b"jpeg-two"]

    monkeypatch.setattr(models_mod, "get_provider_for_task", lambda _level: "openai")
    monkeypatch.setattr(frame_extract, "extract_frames_ffmpeg", _fake_frames)
    model = OpenAIChatModelWithVideo(
        "gpt-4o",
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(api_key="sk-test", base_url="http://127.0.0.1:9/v1"),
        ),
    )
    agent = GsCoreAIAgent(
        openai_chat_model=model,
        session_id="ut_video_frames",
        create_by="TEST",
        max_history=8,
        max_tokens=64,
        system_prompt="x",
    )
    video = BinaryContent(data=b"abc", media_type="video/mp4")
    out = await agent._prepare_video_content([video], "text,image")
    images = [item for item in out if isinstance(item, ImageUrl)]
    assert len(images) == 2
    assert images[0].url.startswith("data:image/jpeg;base64,")


def _openai_agent(session_id: str) -> GsCoreAIAgent:
    from openai import AsyncOpenAI
    from pydantic_ai.providers.openai import OpenAIProvider

    model = OpenAIChatModelWithVideo(
        "gemini-2.5-flash",
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(api_key="sk-test", base_url="http://127.0.0.1:9/v1"),
        ),
    )
    return GsCoreAIAgent(
        openai_chat_model=model,
        session_id=session_id,
        create_by="TEST",
        max_history=8,
        max_tokens=64,
        system_prompt="x",
    )


async def _files_missing(
    data: bytes,
    mime_type: str,
    client: OpenAIFilesClient | None,
    *,
    allow_inline: bool = True,
) -> tuple[None, OpenAIVideoHow]:
    _ = allow_inline
    return None, "files_missing"


@pytest.mark.anyio
async def test_files_missing_falls_back_to_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.configs import models as models_mod
    from gsuid_core.ai_core.multimodal import openai_files as openai_files_mod, frame_extract

    async def _fake_frames(
        video_data: bytes,
        video_format: str = "mp4",
        interval_seconds: float = 2.0,
        max_frames: int = 0,
    ) -> list[bytes]:
        return [b"jpeg-one"]

    monkeypatch.setattr(models_mod, "get_provider_for_task", lambda _level: "gemini")
    monkeypatch.setattr(openai_files_mod, "video_bytes_to_openai_content", _files_missing)
    monkeypatch.setattr(frame_extract, "extract_frames_ffmpeg", _fake_frames)
    agent = _openai_agent("ut_video_files_missing_frames")
    video = BinaryContent(data=b"x" * (INLINE_VIDEO_MAX_BYTES + 1), media_type="video/mp4")
    out = await agent._prepare_video_content([video], "text,image,video")
    images = [item for item in out if isinstance(item, ImageUrl)]
    assert len(images) == 1


@pytest.mark.anyio
async def test_files_missing_without_image_is_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.multimodal import openai_files as openai_files_mod

    monkeypatch.setattr(openai_files_mod, "video_bytes_to_openai_content", _files_missing)
    agent = _openai_agent("ut_video_files_missing_text")
    video = BinaryContent(data=b"x" * (INLINE_VIDEO_MAX_BYTES + 1), media_type="video/mp4")
    out = await agent._prepare_video_content([video], "text,video")
    assert len(out) == 1
    assert isinstance(out[0], str)
    assert "无法读取" in out[0]


@pytest.mark.anyio
async def test_prepare_video_responses_disables_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.configs import models as models_mod
    from gsuid_core.ai_core.multimodal import openai_files as openai_files_mod

    seen: dict[str, bool] = {}

    async def _capture(
        data: bytes,
        mime_type: str,
        client: OpenAIFilesClient | None,
        *,
        allow_inline: bool = True,
    ) -> tuple[BinaryContent, OpenAIVideoHow]:
        seen["allow_inline"] = allow_inline
        return BinaryContent(data=data, media_type=mime_type), "inline"

    monkeypatch.setattr(models_mod, "get_provider_for_task", lambda _level: "openai")
    monkeypatch.setattr(openai_files_mod, "video_bytes_to_openai_content", _capture)
    model = OpenAIResponsesModel(
        "gpt-4o",
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(api_key="sk-test", base_url="http://127.0.0.1:9/v1"),
        ),
    )
    agent = GsCoreAIAgent(
        openai_chat_model=model,
        session_id="ut_video_responses_inline",
        create_by="TEST",
        max_history=8,
        max_tokens=64,
        system_prompt="x",
    )
    video = BinaryContent(data=b"abc", media_type="video/mp4")
    await agent._prepare_video_content([video], "text,image,video")
    assert seen["allow_inline"] is False


@pytest.mark.anyio
async def test_prepare_video_uses_model_not_task_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.configs import models as models_mod

    monkeypatch.setattr(models_mod, "get_provider_for_task", lambda _level: "gemini")
    agent = _openai_agent("ut_video_routed_provider")
    video = BinaryContent(data=b"abc", media_type="video/mp4")
    out = await agent._prepare_video_content([video], "text,image,video")
    assert len(out) == 1
    assert isinstance(out[0], BinaryContent)


@pytest.mark.anyio
async def test_prepare_user_message_drops_image_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.configs import models as models_mod

    class _Data:
        def __init__(self, data: list[str]) -> None:
            self.data = data

    class _Cfg:
        def get_config(self, key: str) -> _Data:
            if key == "model_support":
                return _Data(["text", "video"])
            return _Data([])

    monkeypatch.setattr(models_mod, "get_model_config_for_task", lambda _level: _Cfg())
    agent = _openai_agent("ut_video_drop_image")
    image = BinaryContent(data=b"\x89PNG\r\n\x1a\n", media_type="image/png")
    video = BinaryContent(data=b"abc", media_type="video/mp4")
    out = await agent._prepare_user_message(["看这个", image, video])
    assert isinstance(out, list)
    assert any(isinstance(item, BinaryContent) and str(item.media_type).startswith("video/") for item in out)
    assert not any(isinstance(item, BinaryContent) and str(item.media_type).startswith("image/") for item in out)


@pytest.mark.anyio
async def test_prepare_user_message_no_image_unread_video(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.configs import models as models_mod

    class _Data:
        def __init__(self, data: list[str]) -> None:
            self.data = data

    class _Cfg:
        def get_config(self, key: str) -> _Data:
            if key == "model_support":
                return _Data(["text"])
            return _Data([])

    monkeypatch.setattr(models_mod, "get_model_config_for_task", lambda _level: _Cfg())
    agent = _openai_agent("ut_video_unread")
    video = BinaryContent(data=b"abc", media_type="video/mp4")
    out = await agent._prepare_user_message(["看这个", video])
    assert isinstance(out, str)
    assert "无法读取" in out


@pytest.mark.anyio
async def test_msg_process_registers_video() -> None:
    from gsuid_core.models import Message, MessageReceive
    from gsuid_core.handler import msg_process
    from gsuid_core.utils.resource_manager import RM

    msg = MessageReceive(
        bot_id="onebot",
        bot_self_id="1",
        user_type="direct",
        user_id="u1",
        content=[Message(type="video", data="base64://AAAA")],
    )
    ev = await msg_process(msg)
    assert ev.video_id is not None
    assert ev.video_id.startswith("vid_")
    assert ev.video_id_list == [ev.video_id]
    data = await RM.get(ev.video_id)
    assert isinstance(data, (bytes, bytearray))


class _SupportData:
    def __init__(self, data: list[str]) -> None:
        self.data = data


class _SupportCfg:
    def __init__(self, support: list[str]) -> None:
        self._support = support

    def get_config(self, key: str) -> _SupportData:
        if key == "model_support":
            return _SupportData(self._support)
        return _SupportData([])


@pytest.mark.anyio
async def test_prepare_content_payload_lists_video_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.utils import prepare_content_payload
    from gsuid_core.ai_core.configs import models as models_mod

    async def _none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("gsuid_core.ai_core.outbound.resolve_quote", _none)
    monkeypatch.setattr("gsuid_core.ai_core.outbound.ownership_hint", _none)
    monkeypatch.setattr(models_mod, "get_model_config_for_task", lambda _level: _SupportCfg(["text", "image", "video"]))
    ev = Event(
        bot_id="onebot",
        bot_self_id="1",
        user_type="direct",
        user_id="u1",
        text="看这个",
        video_id_list=["vid_deadbeef"],
    )
    payload, _flags = await prepare_content_payload(ev)
    assert payload
    text = payload[0]
    assert isinstance(text, str)
    assert "vid_deadbeef" in text
    assert "read_video" in text


@pytest.mark.anyio
async def test_prepare_content_payload_hides_read_video_without_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.utils import prepare_content_payload
    from gsuid_core.ai_core.configs import models as models_mod

    async def _none(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("gsuid_core.ai_core.outbound.resolve_quote", _none)
    monkeypatch.setattr("gsuid_core.ai_core.outbound.ownership_hint", _none)
    monkeypatch.setattr(models_mod, "get_model_config_for_task", lambda _level: _SupportCfg(["text", "image"]))
    ev = Event(
        bot_id="onebot",
        bot_self_id="1",
        user_type="direct",
        user_id="u1",
        text="看这个",
        video_id_list=["vid_deadbeef"],
    )
    payload, _flags = await prepare_content_payload(ev)
    text = payload[0]
    assert isinstance(text, str)
    assert "read_video" not in text
    assert "无法查看" in text


@pytest.mark.anyio
async def test_load_video_for_agent_injects_binary() -> None:
    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.ai_core.buildin_tools.video_reader import load_video_for_agent

    agent = _openai_agent("ut_read_video")
    vid = RM.register_video(b"\x00\x00\x00\x18ftypisom" + b"abc")
    note, media = await load_video_for_agent(vid, agent)
    assert media is not None
    assert any(isinstance(item, BinaryContent) for item in media)
    assert "视频" in note


@pytest.mark.anyio
async def test_load_video_rejects_unrecognized_bytes() -> None:
    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.ai_core.buildin_tools.video_reader import load_video_for_agent

    agent = _openai_agent("ut_read_video_bad")
    vid = RM.register_video(b"xxxx")
    note, media = await load_video_for_agent(vid, agent)
    assert media is None
    assert "不是可识别的视频" in note


def test_sniff_video_mime_ftyp() -> None:
    from gsuid_core.ai_core.buildin_tools.video_reader import sniff_video_mime

    buf = b"\x00\x00\x00\x18ftypisom"
    assert sniff_video_mime(buf) == "video/mp4"
    assert sniff_video_mime(b"xxxx") is None
    assert sniff_video_mime(b"") is None


def test_context_has_video_requires_model_support() -> None:
    from collections.abc import Sequence

    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools.visibility import (
        MODEL_DECLARES_VIDEO_KEY,
        context_has_video,
    )

    class _Ctx:
        deps: ToolContext | None
        messages: Sequence[object]

        def __init__(self, extra: dict[str, bool]) -> None:
            self.deps = ToolContext(
                ev=Event(bot_id="onebot", bot_self_id="1", user_type="direct", user_id="u1", video_id="vid_1"),
                extra=dict(extra),
            )
            self.messages = ()

    assert context_has_video(_Ctx({MODEL_DECLARES_VIDEO_KEY: True})) is True
    assert context_has_video(_Ctx({MODEL_DECLARES_VIDEO_KEY: False})) is False
    assert context_has_video(_Ctx({})) is True


def test_unread_attachment_counts_video_only_when_readable() -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.agent_run.state import RunOnceState
    from gsuid_core.ai_core.agent_run.settle import _has_unread_attachment

    st = RunOnceState(
        user_message="hi",
        bot=None,
        ev=Event(
            bot_id="onebot",
            bot_self_id="1",
            user_type="direct",
            user_id="u1",
            video_id_list=["vid_1"],
        ),
        rag_context=None,
        tools=[],
        return_mode="return",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
    )
    assert _has_unread_attachment(st, video_readable=True) is True
    assert _has_unread_attachment(st, video_readable=False) is False
