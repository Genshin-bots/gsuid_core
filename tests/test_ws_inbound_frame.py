"""一帧坏的上报不能拆掉 WebSocket。"""

from __future__ import annotations

from typing import List

import pytest
from msgspec import json as msgjson

from gsuid_core.models import Message, MessageReceive
from gsuid_core.handler import decode_inbound_frame


def _capture_warning(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    warnings: List[str] = []

    def _warning(event: object, *_args: object, **_kwargs: object) -> None:
        warnings.append(str(event))

    monkeypatch.setattr("gsuid_core.handler.logger.warning", _warning)
    return warnings


@pytest.mark.parametrize("data", [b"", b'{"bot_id":"yunzai"'])
def test_undecodable_frame_warns_once_and_returns_none(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    warnings = _capture_warning(monkeypatch)

    assert decode_inbound_frame(data, "yunzai") is None

    assert len(warnings) == 1
    text = warnings[0]
    assert "yunzai" in text
    assert "Input data was truncated" in text
    assert str(len(data)) in text
    assert "\n" not in text


def test_wrong_type_frame_warns_once_and_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings = _capture_warning(monkeypatch)

    assert decode_inbound_frame(b"[]", "yunzai") is None

    assert len(warnings) == 1
    text = warnings[0]
    assert "yunzai" in text
    assert "Expected" in text
    assert "2" in text
    assert "\n" not in text


def test_valid_frame_decodes_without_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings = _capture_warning(monkeypatch)
    raw = msgjson.encode(
        MessageReceive(
            bot_id="yunzai",
            bot_self_id="1",
            user_id="2",
            content=[Message(type="text", data="hi")],
        )
    )

    decoded = decode_inbound_frame(raw, "yunzai")

    assert decoded is not None
    assert decoded.bot_id == "yunzai"
    assert decoded.content[0].data == "hi"
    assert warnings == []
