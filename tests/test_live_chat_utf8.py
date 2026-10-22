"""实时聊天的 JSON 必须用 UTF-8 读写。中文 Windows 上旧文件可能是 GBK。"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path

from gsuid_core.webconsole.live_chat_api import _read_json, _atomic_write_json


def test_write_and_read_chinese_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    payload = {"activeId": "direct:1", "conversations": [{"name": "与主人私聊"}]}

    async def _run() -> None:
        await _atomic_write_json(path, payload)
        raw = path.read_bytes()
        raw.decode("utf-8")
        assert "与主人私聊".encode("utf-8") in raw
        loaded = await _read_json(path)
        assert isinstance(loaded, dict)
        conversations = loaded["conversations"]
        assert isinstance(conversations, list)
        first = conversations[0]
        assert isinstance(first, dict)
        assert first["name"] == "与主人私聊"

    asyncio.run(_run())


def test_gbk_file_is_read_and_rewritten_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    body = {"name": "与主人私聊", "lastPreview": "早柚不知道"}
    path.write_bytes(json.dumps(body, ensure_ascii=False).encode("gbk"))

    async def _run() -> None:
        loaded = await _read_json(path)
        assert isinstance(loaded, dict)
        assert loaded["name"] == "与主人私聊"
        assert loaded["lastPreview"] == "早柚不知道"
        path.read_bytes().decode("utf-8")

    asyncio.run(_run())
