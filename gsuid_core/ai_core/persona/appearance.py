"""立绘 → 形象卡文本（appearance.txt）。慢变，进 system 角色卡区。"""

from __future__ import annotations

from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import PERSONA_PATH

_APPEARANCE_PROMPT = "描述这个角色外观：发色/发型/服装/标志性特征，一两句话，不要评价画风。"


def appearance_path(persona_name: str) -> Path:
    return PERSONA_PATH / persona_name / "appearance.txt"


def load_appearance_line(persona_name: str) -> str:
    path = appearance_path(persona_name)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip().replace("\n", " ")
    if not text:
        return ""
    return text[:100]


def _portrait_file(persona_dir: Path) -> Path | None:
    for name in ("avatar.png", "image.png", "avatar.jpg", "image.jpg"):
        p = persona_dir / name
        if p.is_file():
            return p
    return None


async def refresh_appearance_card(persona_name: str) -> str:
    """立绘 mtime 变化才重跑 understand_image；失败静默。"""
    persona_dir = PERSONA_PATH / persona_name
    portrait = _portrait_file(persona_dir)
    if portrait is None:
        return ""
    out = appearance_path(persona_name)
    if out.is_file() and out.stat().st_mtime >= portrait.stat().st_mtime:
        return load_appearance_line(persona_name)
    try:
        from gsuid_core.ai_core.image_understand.understand import understand_image

        desc = await understand_image(str(portrait), prompt=_APPEARANCE_PROMPT)
    except Exception as e:
        logger.debug(t("log.persona.appearance_understand_fail", p0=persona_name, e=e))
        return load_appearance_line(persona_name)
    line = (desc or "").strip().replace("\n", " ")[:100]
    if not line:
        return ""
    out.write_text(line, encoding="utf-8")
    return line


async def refresh_all_appearance_cards() -> None:
    if not PERSONA_PATH.exists():
        return
    for persona_dir in PERSONA_PATH.iterdir():
        if persona_dir.is_dir():
            await refresh_appearance_card(persona_dir.name)
