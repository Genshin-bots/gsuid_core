"""网页控制台插件选择器偏好：最近使用插件列表与最后编辑插件。"""

from __future__ import annotations

import json
import threading
from typing import TypedDict
from dataclasses import dataclass

from boltons.fileutils import atomic_save

from gsuid_core.sv import SL
from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.data_store import PLUGIN_PREFS_PATH

_VERSION = 1
MAX_RECENT_PLUGINS = 16
_NAME_MAX_LEN = 80
_READ_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError)


class PluginPrefsPayload(TypedDict):
    recent_plugins: list[str]
    last_edited_plugin: str


@dataclass
class _PluginPrefsState:
    recent_plugins: list[str]
    last_edited_plugin: str


_lock = threading.Lock()
_cache: _PluginPrefsState | None = None


def _clean_plugin_name(raw_name: str) -> str:
    stripped = "".join(ch for ch in raw_name.strip() if ch >= " " and ch != "\x7f")[:_NAME_MAX_LEN]
    if not stripped:
        return ""
    lower = stripped.lower()
    for loaded_name in SL.plugins:
        if loaded_name.lower() == lower:
            return loaded_name
    return stripped


def _parse_recent_list(raw_items: list[str]) -> list[str]:
    seen_lower: set[str] = set()
    cleaned_list: list[str] = []
    for item in raw_items:
        name = _clean_plugin_name(item)
        if not name:
            continue
        lower = name.lower()
        if lower in seen_lower:
            continue
        seen_lower.add(lower)
        cleaned_list.append(name)
        if len(cleaned_list) >= MAX_RECENT_PLUGINS:
            break
    return cleaned_list


def _load_locked() -> _PluginPrefsState:
    global _cache
    if _cache is not None:
        return _cache

    try:
        raw_text = PLUGIN_PREFS_PATH.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
    except _READ_ERRORS:
        _cache = _PluginPrefsState(recent_plugins=[], last_edited_plugin="")
        return _cache

    if not isinstance(parsed, dict):
        _cache = _PluginPrefsState(recent_plugins=[], last_edited_plugin="")
        return _cache

    raw_recent = parsed["recent_plugins"] if "recent_plugins" in parsed else []
    str_items = [x for x in raw_recent if isinstance(x, str)] if isinstance(raw_recent, list) else []
    raw_last = parsed["last_edited_plugin"] if "last_edited_plugin" in parsed else ""
    last_edited = _clean_plugin_name(raw_last) if isinstance(raw_last, str) else ""

    _cache = _PluginPrefsState(
        recent_plugins=_parse_recent_list(str_items),
        last_edited_plugin=last_edited,
    )
    return _cache


def _save_locked(state: _PluginPrefsState) -> bool:
    doc = {
        "version": _VERSION,
        "recent_plugins": state.recent_plugins,
        "last_edited_plugin": state.last_edited_plugin,
    }
    try:
        PLUGIN_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with atomic_save(
            str(PLUGIN_PREFS_PATH),
            text_mode=False,
            file_perms=None,
            overwrite=True,
            overwrite_part=True,
        ) as handle:
            if handle is None:
                return False
            handle.write(json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
        return True
    except OSError as error:
        logger.debug(t("log.webconsole.plugin_prefs_save_fail", error=error))
        return False


def _snapshot(state: _PluginPrefsState) -> PluginPrefsPayload:
    return {
        "recent_plugins": list(state.recent_plugins),
        "last_edited_plugin": state.last_edited_plugin,
    }


def get_plugin_prefs_sync() -> PluginPrefsPayload:
    with _lock:
        return _snapshot(_load_locked())


def record_recent_plugin_sync(plugin_name: str, *, edited: bool = False) -> PluginPrefsPayload:
    cleaned = _clean_plugin_name(plugin_name)
    with _lock:
        state = _load_locked()
        if not cleaned:
            return _snapshot(state)
        lower = cleaned.lower()
        rest = [item for item in state.recent_plugins if item.lower() != lower]
        state.recent_plugins = [cleaned, *rest][:MAX_RECENT_PLUGINS]
        if edited:
            state.last_edited_plugin = cleaned
        _save_locked(state)
        return _snapshot(state)


@to_thread
def get_plugin_prefs() -> PluginPrefsPayload:
    return get_plugin_prefs_sync()


@to_thread
def record_recent_plugin(plugin_name: str, edited: bool = False) -> PluginPrefsPayload:
    return record_recent_plugin_sync(plugin_name, edited=edited)
