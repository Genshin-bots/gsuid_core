"""表情预览图：长边不超过 160，并且是 JPEG。"""

import io

from PIL import Image

from gsuid_core.ai_core.meme.thumb import THUMB_LONG_SIDE, cached_thumb, remember_thumb, make_thumb_jpeg


def test_thumb_shrinks_long_side_and_is_jpeg() -> None:
    source = Image.new("RGB", (800, 200), (10, 20, 30))
    buf = io.BytesIO()
    source.save(buf, format="PNG")
    jpeg = make_thumb_jpeg(buf.getvalue())
    assert jpeg[:2] == b"\xff\xd8"
    with Image.open(io.BytesIO(jpeg)) as image:
        assert max(image.size) == THUMB_LONG_SIDE
        assert image.size == (160, 40)


def test_thumb_cache_keeps_bytes() -> None:
    remember_thumb("abc", b"jpeg")
    assert cached_thumb("abc") == b"jpeg"
    assert cached_thumb("missing") is None
