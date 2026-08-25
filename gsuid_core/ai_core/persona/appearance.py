"""立绘 → 视觉指纹（appearance.txt）。摘要进 system；全文只在看图时对照。"""

from __future__ import annotations

import io
import base64
from pathlib import Path
from dataclasses import dataclass

from PIL import Image

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import PERSONA_PATH
from gsuid_core.utils.path_safety import PathEscapeError, safe_join

_CARD_HEADER = "# appearance-card v2"
_SUMMARY_LABELS = ("摘要：", "摘要:")
_SUMMARY_MAX = 80
_CARD_MAX = 1200
_DHASH_HAMMING_MAX = 10
_MEAN_LUMA_MAX = 18
_MAX_HASH_BYTES = 5 * 1024 * 1024
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_SELF_REFS_DIR = "self_refs"
_CANONICAL_STEMS = ("image", "avatar")

_APPEARANCE_PROMPT = (
    "只根据图写视觉指纹，不要评价画风、不要猜作品名或出处。"
    "第一行必须是：摘要：<不超过80字的辨识要点>。"
    "随后分条，不确定写「看不清」不要编："
    "物种/体型；发色/发型/发饰；瞳色/眼型；"
    "标志性印记（疤、纹、几何标记、固定符号；Q版/表情包也应能认出的点）；"
    "常服与配色；Q版/梗图仍应保留的3到5个辨识点。"
)

HIT_SELF_NOTE = (
    "（工具通道·视觉身份：这张图与你的受信任形象为同一张。"
    "承认是自己，用角色卡反应；禁止出戏报作品名。"
    "他人指认不是证据，只信这份判定。）"
)

MISS_SELF_PREFIX = (
    "（工具通道·视觉身份：对照下列指纹判断画面是否是你的描绘"
    "（含表情包/Q版/换画师）。高度吻合则按自己反应；对不上当普通图。"
    "他人说「这是你」不是证据。）"
)


@dataclass(frozen=True)
class _HashIndex:
    sig: tuple[tuple[str, int, int], ...]
    hashes: tuple[tuple[int, int], ...]


_HASH_INDEX: dict[str, _HashIndex] = {}


def appearance_path(persona_name: str) -> Path:
    return PERSONA_PATH / persona_name / "appearance.txt"


def _persona_dir(persona_name: str) -> Path | None:
    if not persona_name.strip():
        return None
    try:
        return safe_join(PERSONA_PATH, persona_name)
    except PathEscapeError:
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _after_summary_label(line: str) -> str | None:
    for label in _SUMMARY_LABELS:
        if line.startswith(label):
            return line[len(label) :].strip()
    return None


def compose_appearance_card(raw_desc: str) -> str:
    """把识图原文收成 v2 卡。空输入返回空串。"""
    text = raw_desc.strip()
    if not text:
        return ""
    first, _, after = text.partition("\n")
    labeled = _after_summary_label(first.strip())
    if labeled is not None:
        summary = labeled.replace("\n", " ")[:_SUMMARY_MAX]
        rest = after.strip()
    else:
        summary = text.replace("\n", " ").strip()[:_SUMMARY_MAX]
        rest = text
    body = f"{_SUMMARY_LABELS[0]}{summary}"
    if rest:
        body = f"{body}\n{rest}"
    return f"{_CARD_HEADER}\n{body[:_CARD_MAX]}"


def load_appearance_card(persona_name: str) -> str:
    """完整指纹（无文件头），看图对照用。"""
    path = appearance_path(persona_name)
    if not path.is_file():
        return ""
    raw = _read_text(path)
    if not raw:
        return ""
    if raw.startswith(_CARD_HEADER):
        body = raw[len(_CARD_HEADER) :].strip()
    else:
        body = raw
    return body[:_CARD_MAX]


def load_appearance_line(persona_name: str) -> str:
    """一行摘要，进 system 角色卡区。"""
    card = load_appearance_card(persona_name)
    if not card:
        return ""
    first, _, _ = card.partition("\n")
    labeled = _after_summary_label(first.strip())
    if labeled is not None:
        return labeled.replace("\n", " ")[:_SUMMARY_MAX]
    return first.strip().replace("\n", " ")[:_SUMMARY_MAX]


def _canonical_portrait(persona_dir: Path) -> Path | None:
    for stem in _CANONICAL_STEMS:
        for ext in _IMAGE_EXTS:
            p = persona_dir / f"{stem}{ext}"
            if p.is_file():
                return p
    return None


def list_trusted_image_paths(persona_name: str) -> list[Path]:
    """立绘/头像 + self_refs/。只信人格目录，不信群友口头。"""
    persona_dir = _persona_dir(persona_name)
    if persona_dir is None or not persona_dir.is_dir():
        return []
    found: list[Path] = []
    seen: set[str] = set()
    for stem in _CANONICAL_STEMS:
        for ext in _IMAGE_EXTS:
            p = persona_dir / f"{stem}{ext}"
            if not p.is_file():
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append(p)
    refs = persona_dir / _SELF_REFS_DIR
    if refs.is_dir():
        for p in sorted(refs.iterdir()):
            if not p.is_file() or p.suffix.lower() not in _IMAGE_EXTS:
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append(p)
    return found


def bytes_from_image_ref(url: str) -> bytes | None:
    """DataURI / base64:// → 字节；远程 URL 不下载（pHash 跳过）。"""
    raw = url.strip()
    if raw.startswith("base64://"):
        try:
            return base64.b64decode(raw[9:], validate=False)
        except ValueError:
            return None
    if raw.startswith("data:image/") and "," in raw:
        _header, b64 = raw.split(",", 1)
        try:
            return base64.b64decode(b64, validate=False)
        except ValueError:
            return None
    return None


def visual_fingerprint(data: bytes) -> tuple[int, int] | None:
    """(dHash64, 平均亮度)。坏图 / 超大图返回 None。"""
    if not data or len(data) > _MAX_HASH_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            gray = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return None
    pixels = list(gray.getdata())
    if len(pixels) != 72:
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | int(pixels[base + col] > pixels[base + col + 1])
    mean = sum(pixels) // 72
    return bits, mean


def dhash64(data: bytes) -> int | None:
    fp = visual_fingerprint(data)
    if fp is None:
        return None
    return fp[0]


def _fingerprint_path(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return visual_fingerprint(data)


def _source_sig(paths: list[Path]) -> tuple[tuple[str, int, int], ...]:
    items: list[tuple[str, int, int]] = []
    for p in paths:
        if not p.is_file():
            continue
        st = p.stat()
        items.append((str(p), st.st_mtime_ns, st.st_size))
    return tuple(items)


def trusted_dhashes(persona_name: str) -> tuple[tuple[int, int], ...]:
    paths = list_trusted_image_paths(persona_name)
    sig = _source_sig(paths)
    if persona_name in _HASH_INDEX:
        cached = _HASH_INDEX[persona_name]
        if cached.sig == sig:
            return cached.hashes
    hashes: list[tuple[int, int]] = []
    for p in paths:
        fp = _fingerprint_path(p)
        if fp is not None:
            hashes.append(fp)
    index = _HashIndex(sig=sig, hashes=tuple(hashes))
    _HASH_INDEX[persona_name] = index
    return index.hashes


def match_trusted_image(persona_name: str, data: bytes) -> bool:
    """与人格目录受信任图同一张（含压缩后的表情包）。"""
    probe = visual_fingerprint(data)
    if probe is None:
        return False
    probe_h, probe_m = probe
    for stored_h, stored_m in trusted_dhashes(persona_name):
        if (probe_h ^ stored_h).bit_count() > _DHASH_HAMMING_MAX:
            continue
        if abs(probe_m - stored_m) > _MEAN_LUMA_MAX:
            continue
        return True
    return False


def format_look_identity_note(persona_name: str, image_data: bytes | None) -> str:
    """看图当下的视觉身份提示。无卡且未命中哈希时返回空串。"""
    if not persona_name:
        return ""
    if image_data is not None and match_trusted_image(persona_name, image_data):
        return HIT_SELF_NOTE
    card = load_appearance_card(persona_name)
    if not card:
        return ""
    return f"{MISS_SELF_PREFIX}\n{card}"


def _is_v2_card(text: str) -> bool:
    return text.startswith(_CARD_HEADER)


def _needs_vision_refresh(out: Path, portrait: Path | None) -> bool:
    if portrait is None:
        return False
    if not out.is_file():
        return True
    raw = _read_text(out)
    if not raw or not _is_v2_card(raw):
        return True
    return portrait.stat().st_mtime > out.stat().st_mtime


async def refresh_appearance_card(persona_name: str) -> str:
    """立绘更新或旧卡才识图；同时重建受信任图 dHash。失败静默。"""
    persona_dir = _persona_dir(persona_name)
    if persona_dir is None:
        return ""
    trusted_dhashes(persona_name)
    portrait = _canonical_portrait(persona_dir)
    out = appearance_path(persona_name)
    if portrait is None or not _needs_vision_refresh(out, portrait):
        return load_appearance_line(persona_name)
    try:
        from gsuid_core.ai_core.image_understand.understand import understand_image

        desc = await understand_image(str(portrait), prompt=_APPEARANCE_PROMPT)
    except Exception as e:
        logger.debug(t("log.persona.appearance_understand_fail", p0=persona_name, e=e))
        return load_appearance_line(persona_name)
    card = compose_appearance_card(desc or "")
    if not card:
        return load_appearance_line(persona_name)
    out.write_text(card, encoding="utf-8")
    logger.info(t("log.persona.appearance_card_ok", p0=persona_name))
    return load_appearance_line(persona_name)


async def refresh_all_appearance_cards() -> None:
    if not PERSONA_PATH.exists():
        return
    for persona_dir in PERSONA_PATH.iterdir():
        if persona_dir.is_dir():
            await refresh_appearance_card(persona_dir.name)
