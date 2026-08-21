"""本地图片路径必须编成真正 DataURI，不能把路径当裸 base64 包进去。"""

from __future__ import annotations

import base64
import asyncio
from pathlib import Path

from gsuid_core.ai_core.utils import _normalize_image_url, materialize_image_url

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_normalize_http_and_data_passthrough() -> None:
    http = "https://example.com/a.png"
    assert _normalize_image_url(http) == http
    data = "data:image/png;base64,xx"
    assert _normalize_image_url(data) == data
    assert _normalize_image_url("base64://abc") == "data:image/png;base64,abc"
    assert _normalize_image_url("AAAA") == "data:image/png;base64,AAAA"
    naked = base64.b64encode(_PNG_1X1).decode("ascii")
    assert _normalize_image_url(naked) == f"data:image/png;base64,{naked}"


def test_normalize_local_png_encodes_bytes_not_path(tmp_path: Path) -> None:
    png = tmp_path / "avatar.png"
    png.write_bytes(_PNG_1X1)
    uri = _normalize_image_url(str(png))
    assert uri.startswith("data:image/png;base64,")
    payload = uri.split(",", 1)[1]
    assert payload != str(png)
    assert str(png) not in payload
    assert base64.b64decode(payload) == _PNG_1X1


def test_normalize_local_jpg_mime(tmp_path: Path) -> None:
    jpg = tmp_path / "avatar.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xd9")
    uri = _normalize_image_url(str(jpg))
    assert uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == b"\xff\xd8\xff\xd9"


def test_materialize_local_png(tmp_path: Path) -> None:
    png = tmp_path / "avatar.png"
    png.write_bytes(_PNG_1X1)
    uri = asyncio.run(materialize_image_url(str(png)))
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == _PNG_1X1
