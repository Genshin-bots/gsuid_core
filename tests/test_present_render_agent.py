"""过长结构化正文先交给 render_agent 出图，没交出图片才退回 markdown 卡片。"""

from __future__ import annotations

import base64
import asyncio
from typing import List, Union, Mapping, Optional

import pytest

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core import utils as ai_utils
from gsuid_core.segment import Message
from gsuid_core.ai_core.outbound import ImageClaim
from gsuid_core.ai_core.configs.ai_config import ai_config

_HANDLE = "res_abc123def456"
_FACT = "对乙酰氨基酚"
_LONG = (
    "# 家里的小药箱\n\n"
    "这个我来慢慢想。家里那点药不用堆成小仓库。\n\n"
    "## 退烧止痛\n\n"
    f"{_FACT}、布洛芬，两种别叠着吃，剂量照说明书来。\n\n"
    "## 肠胃\n\n"
    "口服补液盐、蒙脱石散、益生菌。拉肚子时补液比止泻更要紧。\n\n"
    "真碰上高烧不退、喘不上气，就直接去医院。\n"
)


class _Box:
    def __init__(self, data: bool | int) -> None:
        self.data = data


class _Art:
    mime = "image/png"
    payload_path = "render.png"


class _Sent:
    def __init__(self) -> None:
        self.images: List[bytes] = []
        self.texts: List[str] = []


def _install(
    monkeypatch: pytest.MonkeyPatch,
    sent: _Sent,
    tasks: List[str],
    *,
    agent_reply: str,
    allow: bool,
    flag: bool,
) -> None:
    real_get = ai_config.get_config

    def _get(name: str) -> object:
        if name == "render_long_markdown_as_image":
            return _Box(flag)
        if name == "markdown_image_min_chars":
            return _Box(40)
        return real_get(name)

    async def _run(
        profile_id: str,
        task: str,
        ev: Event | None,
        bot: Bot | None = None,
        session_id_suffix: str = "",
    ) -> str:
        assert profile_id == "render_agent"
        tasks.append(task)
        return agent_reply

    async def _md(**_kwargs: object) -> bytes:
        return b"MD_PNG"

    async def _claim(ev: Event | None, image_id: str, *, session_id: str) -> ImageClaim:
        return ImageClaim(occupied=False, refuse=None)

    async def _get_art(res_id: str) -> _Art | None:
        if res_id == _HANDLE:
            return _Art()
        return None

    async def _send(
        self: Bot,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        extra_metadata: Optional[Mapping[str, object]] = None,
        wait_recall: bool = False,
    ) -> None:
        if isinstance(message, str):
            sent.texts.append(message)
            return
        segs = message if isinstance(message, list) else [message]
        for seg in segs:
            if not isinstance(seg, Message):
                continue
            if seg.type == "image" and isinstance(seg.data, str) and seg.data.startswith("base64://"):
                sent.images.append(base64.b64decode(seg.data.removeprefix("base64://")))
            elif seg.type == "text" and isinstance(seg.data, str):
                sent.texts.append(seg.data)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_config, "get_config", _get)
    monkeypatch.setattr(ai_utils.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("gsuid_core.ai_core.output_firewall.is_enabled", lambda: False)
    monkeypatch.setattr(ai_utils, "_persona_allows_render_agent", lambda _ev: allow)
    monkeypatch.setattr(ai_utils, "_read_image_artifact_bytes", lambda _path: b"AGENT_PNG")
    monkeypatch.setattr("gsuid_core.ai_core.capability_agents.runner.run_capability_agent", _run)
    monkeypatch.setattr("gsuid_core.utils.html_render.render_md_to_bytes", _md)
    monkeypatch.setattr("gsuid_core.ai_core.outbound.try_claim_image_delivery", _claim)
    monkeypatch.setattr("gsuid_core.ai_core.planning.models.AIAgentArtifact.get_by_id", _get_art)
    monkeypatch.setattr(Bot, "send", _send)


def _deliver(text: str) -> None:
    asyncio.run(
        ai_utils.send_chat_result(
            Bot.__new__(Bot),
            text,
            ev=Event(user_id="u1", raw_text="家里备点什么药"),
            ooc_check=False,
        )
    )


def test_long_markdown_sends_render_agent_image(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _Sent()
    tasks: List[str] = []
    _install(
        monkeypatch,
        sent,
        tasks,
        agent_reply=f"已出图，句柄 `{_HANDLE}`",
        allow=True,
        flag=True,
    )
    _deliver(_LONG)
    assert tasks and _FACT in tasks[0]
    assert sent.images == [b"AGENT_PNG"]
    assert not sent.texts


def test_report_block_sends_render_agent_image(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _Sent()
    tasks: List[str] = []
    _install(
        monkeypatch,
        sent,
        tasks,
        agent_reply=f"句柄 {_HANDLE}",
        allow=True,
        flag=False,
    )
    _deliver(f'<report title="小药箱">{_FACT}，别和布洛芬叠着吃。</report>')
    assert tasks and _FACT in tasks[0]
    assert sent.images == [b"AGENT_PNG"]


def test_missing_image_handle_falls_back_to_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _Sent()
    tasks: List[str] = []
    _install(
        monkeypatch,
        sent,
        tasks,
        agent_reply="画好了，但是没给句柄。",
        allow=True,
        flag=True,
    )
    _deliver(_LONG)
    assert tasks
    assert sent.images == [b"MD_PNG"]


def test_disabled_render_agent_uses_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _Sent()
    tasks: List[str] = []
    _install(
        monkeypatch,
        sent,
        tasks,
        agent_reply=f"句柄 {_HANDLE}",
        allow=False,
        flag=True,
    )
    _deliver(_LONG)
    assert tasks == []
    assert sent.images == [b"MD_PNG"]


def test_nested_present_render_does_not_reenter_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _Sent()
    tasks: List[str] = []
    _install(
        monkeypatch,
        sent,
        tasks,
        agent_reply=f"句柄 {_HANDLE}",
        allow=True,
        flag=True,
    )

    async def _go() -> None:
        token = ai_utils._PRESENT_RENDER_DEPTH.set(1)
        try:
            await ai_utils.send_chat_result(
                Bot.__new__(Bot),
                _LONG,
                ev=Event(user_id="u1", raw_text="家里备点什么药"),
                ooc_check=False,
            )
        finally:
            ai_utils._PRESENT_RENDER_DEPTH.reset(token)

    asyncio.run(_go())
    assert tasks == []
    assert sent.images == [b"MD_PNG"]
