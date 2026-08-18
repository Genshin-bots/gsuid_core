"""GenshinUID 体力邮箱提醒：邮箱解析与邮件通道优先。"""

from __future__ import annotations

import sys
import asyncio
from types import ModuleType
from pathlib import Path

import pytest

# ruff: noqa: E402
_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "gsuid_core" / "plugins" / "GenshinUID"
if not (_PLUGIN_ROOT / "GenshinUID").is_dir():
    pytest.skip("GenshinUID 未安装（独立插件，不随框架仓库）", allow_module_level=True)
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from GenshinUID.genshinuid_resin.special_remind import (
    extract_email,
    default_qq_email,
    compose_resin_mail,
    is_email_remind_text,
    send_resin_special_mail,
)


def test_extract_email() -> None:
    assert extract_email("邮箱提醒 you@example.com") == "you@example.com"
    assert extract_email("特别提醒 you@example.com") == "you@example.com"
    assert extract_email("you@example.com") == "you@example.com"
    assert extract_email("邮箱提醒") is None
    assert extract_email("not-an-email") is None


def test_default_qq_email() -> None:
    assert default_qq_email("123456789") == "123456789@qq.com"


def test_is_email_remind_text() -> None:
    assert is_email_remind_text("邮箱提醒 you@example.com") is True
    assert is_email_remind_text("特别提醒 you@example.com") is True
    assert is_email_remind_text("体力") is False


def test_compose_resin_mail_mentions_uid_and_value() -> None:
    subject, body = compose_resin_mail("100740568", 195, "140")
    assert "100740568" in subject
    assert "邮箱提醒" in subject
    assert "195" in body
    assert "140" in body


def _install_fake_mail(send_fn: object) -> None:
    pkg = ModuleType("gscore_mail")
    api = ModuleType("gscore_mail.api")
    setattr(api, "send", send_fn)
    sys.modules["gscore_mail"] = pkg
    sys.modules["gscore_mail.api"] = api


def _drop_fake_mail() -> None:
    sys.modules.pop("gscore_mail.api", None)
    sys.modules.pop("gscore_mail", None)


def test_send_resin_special_mail_uses_plugin_api() -> None:
    captured: list[object] = []

    async def fake_send(**kwargs: object) -> dict[str, object]:
        captured.append(kwargs)
        return {"ok": True, "backend": "smtp", "message": "ok", "raw": ""}

    _install_fake_mail(fake_send)
    try:

        async def _run() -> None:
            ok = await send_resin_special_mail("a@b.c", "1", 160, "140")
            assert ok is True

        asyncio.run(_run())
    finally:
        _drop_fake_mail()
    assert captured
    first = captured[0]
    assert isinstance(first, dict)
    assert first["to"] == "a@b.c"
    assert "邮箱提醒" in str(first["subject"])


def test_send_resin_special_mail_missing_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    from GenshinUID.genshinuid_resin import special_remind as sr

    _drop_fake_mail()
    monkeypatch.setattr(sr, "import_api", lambda package: None)

    async def _run() -> None:
        assert await send_resin_special_mail("a@b.c", "1", 160, "140") is False

    asyncio.run(_run())


def test_send_resin_special_mail_fail_result() -> None:
    async def fake_send(**kwargs: object) -> dict[str, object]:
        return {"ok": False, "backend": "smtp", "message": "down", "raw": ""}

    _install_fake_mail(fake_send)
    try:

        async def _run() -> None:
            assert await send_resin_special_mail("a@b.c", "1", 160, "140") is False

        asyncio.run(_run())
    finally:
        _drop_fake_mail()
