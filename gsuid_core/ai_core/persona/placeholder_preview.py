"""已激活人格在发送时填进提示词标签的值。控制台只读展示，不在提示词组页写入。"""

from __future__ import annotations

from typing import TypedDict
from datetime import datetime

from pytz import timezone

from gsuid_core.config import core_config
from gsuid_core.ai_core.persona.config import persona_config_manager
from gsuid_core.ai_core.persona.resource import list_available_personas
from gsuid_core.ai_core.persona.settings import get_master_title
from gsuid_core.ai_core.configs.ai_config import persona_config
from gsuid_core.ai_core.persona.appearance import load_appearance_line
from gsuid_core.ai_core.persona.chat_style import stored_int, speech_shape_spec, resolve_chat_style
from gsuid_core.ai_core.prompt_group.store import render_prompt, group_name_for_persona
from gsuid_core.ai_core.agent_node.registry import (
    format_capability_roster,
    persona_allows_capability_agent,
)
from gsuid_core.utils.plugins_config.models import GsListStrConfig
from gsuid_core.ai_core.prompt_group.catalog import RENDER_CLAUSE_WHEN_ALLOWED

_SHANGHAI = timezone("Asia/Shanghai")


class PersonaPlaceholderPreview(TypedDict):
    name: str
    prompt_group: str
    values: dict[str, str]
    soft_follows: bool


class PlaceholderPreviewData(TypedDict):
    personas: list[PersonaPlaceholderPreview]
    shared: dict[str, str]


def enabled_persona_names() -> list[str]:
    """人设配置里打开开关的名字，按保存顺序。"""
    item = persona_config.get_config("enable_persona")
    if not isinstance(item, GsListStrConfig) or not isinstance(item.data, list):
        return []
    names: list[str] = []
    for raw in item.data:
        if isinstance(raw, str):
            name = raw.strip()
            if name and name not in names:
                names.append(name)
    return names


def _masters_text() -> str:
    raw = core_config.get_config("masters")
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                parts.append(text)
        elif isinstance(item, int) and not isinstance(item, bool):
            parts.append(str(item))
    return ", ".join(parts)


def _date_text() -> str:
    return datetime.now(_SHANGHAI).strftime("%Y年%m月%d日")


def preview_persona(name: str) -> PersonaPlaceholderPreview:
    """这一人格发送时，各标签会被换成的文字。"""
    style = resolve_chat_style(name)
    shape = speech_shape_spec()
    cfg = persona_config_manager.get_config(name)
    stored_soft = stored_int(cfg, "speech_len_soft", shape["soft_default"])
    clause = RENDER_CLAUSE_WHEN_ALLOWED if persona_allows_capability_agent(name, "render_agent") else ""
    values: dict[str, str] = {
        "soft": str(style.soft),
        "segment_hint": style.segment_hint,
        "master_title": get_master_title(name),
        "char_name": name,
        "render_clause": clause,
        "appearance": load_appearance_line(name),
        "answer_contract": render_prompt(name, "answer_contract"),
        "lines": format_capability_roster(name),
    }
    return {
        "name": name,
        "prompt_group": group_name_for_persona(name),
        "values": values,
        "soft_follows": stored_soft == shape["soft_default"],
    }


def active_placeholder_preview() -> PlaceholderPreviewData:
    """只含仍存在、且开关打开的人格。"""
    available = set(list_available_personas())
    personas = [preview_persona(name) for name in enabled_persona_names() if name in available]
    return {
        "personas": personas,
        "shared": {"masters": _masters_text(), "date": _date_text()},
    }
