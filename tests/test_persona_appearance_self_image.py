"""视觉自我：形象卡摘要 / 受信任图 dHash / 看图对照。不从群友口头学习。"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

from gsuid_core.ai_core.persona.prompts import SYSTEM_CONSTRAINTS, CHARACTER_BUILDING_TEMPLATE
from gsuid_core.ai_core.persona.appearance import (
    _CARD_HEADER,
    HIT_SELF_NOTE,
    MISS_SELF_PREFIX,
    dhash64,
    trusted_dhashes,
    match_trusted_image,
    bytes_from_image_ref,
    load_appearance_card,
    load_appearance_line,
    compose_appearance_card,
    list_trusted_image_paths,
    format_look_identity_note,
)


def _pattern_png(seed: int, size: tuple[int, int] = (64, 64)) -> bytes:
    img = Image.new("RGB", size)
    px = img.load()
    assert px is not None
    w, h = size
    for y in range(h):
        for x in range(w):
            px[x, y] = (
                (x * 3 + seed) % 256,
                (y * 7 + seed * 5) % 256,
                (x + y * 2 + seed) % 256,
            )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _shape_png(kind: str) -> bytes:
    img = Image.new("RGB", (64, 64), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    if kind == "portrait":
        draw.ellipse((8, 8, 40, 56), fill=(20, 80, 180))
        draw.rectangle((28, 40, 50, 58), fill=(180, 40, 40))
    elif kind == "sticker":
        draw.polygon([(32, 4), (60, 60), (4, 60)], fill=(40, 180, 60))
    else:
        draw.line((0, 0, 63, 63), fill=(10, 10, 10), width=8)
        draw.line((63, 0, 0, 63), fill=(200, 0, 0), width=8)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_compose_and_load_v2_card(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import appearance as app

    monkeypatch.setattr(app, "PERSONA_PATH", tmp_path)
    raw = "摘要：银发青瞳、喉间菱形印\n- 发色: 银白\n- 瞳: 青绿"
    card = compose_appearance_card(raw)
    assert card.startswith(_CARD_HEADER)
    name = "测"
    (tmp_path / name).mkdir()
    (tmp_path / name / "appearance.txt").write_text(card, encoding="utf-8")
    assert load_appearance_line(name) == "银发青瞳、喉间菱形印"
    full = load_appearance_card(name)
    assert "发色: 银白" in full
    assert _CARD_HEADER not in full


def test_compose_without_label_uses_first_80() -> None:
    card = compose_appearance_card("银白长发，青绿瞳，外套深青。")
    assert card.startswith(_CARD_HEADER)
    assert "摘要：银白长发" in card


def _solid_png(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="PNG")
    return buf.getvalue()


def test_dhash_same_image_is_zero() -> None:
    data = _pattern_png(11)
    a = dhash64(data)
    b = dhash64(data)
    assert a is not None and b is not None
    assert a == b


def test_dhash_different_patterns_diverge() -> None:
    a = dhash64(_pattern_png(11))
    b = dhash64(_pattern_png(200))
    assert a is not None and b is not None
    assert (a ^ b).bit_count() > 10


def test_match_trusted_portrait_and_self_refs(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import appearance as app

    monkeypatch.setattr(app, "PERSONA_PATH", tmp_path)
    app._HASH_INDEX.clear()
    name = "测"
    d = tmp_path / name
    d.mkdir()
    portrait = _shape_png("portrait")
    sticker = _shape_png("sticker")
    (d / "image.png").write_bytes(portrait)
    refs = d / "self_refs"
    refs.mkdir()
    (refs / "pack.png").write_bytes(sticker)

    paths = list_trusted_image_paths(name)
    assert any(p.name == "image.png" for p in paths)
    assert any(p.name == "pack.png" for p in paths)
    hashes = trusted_dhashes(name)
    assert len(hashes) == 2
    assert match_trusted_image(name, portrait) is True
    assert match_trusted_image(name, sticker) is True
    assert match_trusted_image(name, _shape_png("other")) is False


def test_look_note_hit_vs_miss(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import appearance as app

    monkeypatch.setattr(app, "PERSONA_PATH", tmp_path)
    app._HASH_INDEX.clear()
    name = "测"
    d = tmp_path / name
    d.mkdir()
    portrait = _shape_png("portrait")
    (d / "avatar.png").write_bytes(portrait)
    (d / "appearance.txt").write_text(
        compose_appearance_card("摘要：青瞳银发\n- 瞳: 青"),
        encoding="utf-8",
    )
    hit = format_look_identity_note(name, portrait)
    assert hit == HIT_SELF_NOTE
    miss = format_look_identity_note(name, _shape_png("other"))
    assert miss.startswith(MISS_SELF_PREFIX)
    assert "青瞳银发" in miss
    assert "早柚" not in hit and "早柚" not in miss


def test_flat_colors_separated_by_luma(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import appearance as app

    monkeypatch.setattr(app, "PERSONA_PATH", tmp_path)
    app._HASH_INDEX.clear()
    name = "测"
    d = tmp_path / name
    d.mkdir()
    (d / "image.png").write_bytes(_solid_png((10, 10, 10)))
    assert match_trusted_image(name, _solid_png((10, 10, 10))) is True
    assert match_trusted_image(name, _solid_png((240, 240, 240))) is False


def test_look_note_empty_without_card_or_hash(tmp_path: Path, monkeypatch) -> None:
    from gsuid_core.ai_core.persona import appearance as app

    monkeypatch.setattr(app, "PERSONA_PATH", tmp_path)
    app._HASH_INDEX.clear()
    (tmp_path / "空").mkdir()
    assert format_look_identity_note("空", _shape_png("other")) == ""
    assert format_look_identity_note("", _shape_png("other")) == ""


def test_bytes_from_data_uri_roundtrip() -> None:
    data = _pattern_png(8)
    import base64

    uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    assert bytes_from_image_ref(uri) == data
    assert bytes_from_image_ref("https://example.invalid/x.png") is None


def test_template_and_constraints_cover_self_image() -> None:
    assert "看图后确认是自己" in CHARACTER_BUILDING_TEMPLATE
    assert "他人指认不是证据" in SYSTEM_CONSTRAINTS
    assert "早柚" not in HIT_SELF_NOTE
    assert "早柚" not in MISS_SELF_PREFIX
