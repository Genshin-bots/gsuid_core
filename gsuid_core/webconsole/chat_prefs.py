"""会话管理页的会话偏好：置顶、群备注。

两样都是**面板自己的状态**，不属于任何适配器，存一份 JSON 就够：

```
{"version": 1, "pins": {"12345:group:67890": 1760000000000}, "notes": {"12345:group:67890": "自家小群"}}
```

key 就是 `Event.session_id`（`{WS_BOT_ID}:{bot_id}:{bot_self_id}:{type}:{id}`），
里面已经带了账号，所以同一个群被两个号看着时各自置顶、各自备注。

拉黑不在这里 —— 那是框架级的全局黑名单（`sp_config` 的 `BlackList`），
由 `session_chat_api` 直接读写，面板只负责显示。

读失败一律从空开始：偏好丢了只是列表顺序不对，不该把「看消息」这件事一起拖垮。
写失败只记 debug 日志，返回值交给调用方决定要不要提示。
"""

from __future__ import annotations

import json
import time
import threading
from typing import Any, Dict, Optional

from boltons.fileutils import atomic_save

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import CHAT_PREFS_PATH

_VERSION = 1
_NOTE_MAX = 64
_READ_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError)

_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None


def clean_note(text: Any) -> str:
    """备注统一在这里过一遍：去控制字符、压空白、限长。

    备注在列表里只当一行标签用，换行 / 制表会把行高顶开，长文本会把列表撑开。
    """
    raw = str(text or "")
    cleaned = "".join(" " if ch < " " or ch == "\x7f" else ch for ch in raw)
    return " ".join(cleaned.split())[:_NOTE_MAX]


def _as_map(value: Any) -> Dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, int] = {}
    for key, at in value.items():
        k = str(key or "").strip()
        if not k:
            continue
        try:
            out[k] = int(at or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _as_notes(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for key, text in value.items():
        k = str(key or "").strip()
        if not k:
            continue
        note = clean_note(text)
        if note:
            out[k] = note
    return out


def _blank() -> Dict[str, Any]:
    return {"version": _VERSION, "pins": {}, "notes": {}}


def _load() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(CHAT_PREFS_PATH, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except _READ_ERRORS:
        _cache = _blank()
        return _cache
    if not isinstance(raw, dict):
        _cache = _blank()
        return _cache
    _cache = {
        "version": _VERSION,
        "pins": _as_map(raw.get("pins")),
        "notes": _as_notes(raw.get("notes")),
    }
    return _cache


def _save() -> bool:
    """整表落盘。atomic_save 是上下文管理器，写失败不留下半个文件。"""
    try:
        with atomic_save(
            str(CHAT_PREFS_PATH),
            text_mode=False,
            file_perms=None,
            overwrite=True,
            overwrite_part=True,
        ) as handle:
            if handle is None:
                return False
            handle.write(json.dumps(_load(), ensure_ascii=False, indent=2).encode("utf-8"))
        return True
    except OSError as e:
        logger.debug(t("log.webconsole.chat_prefs_save_fail", error=e))
        return False


def _toggle(field: str, session_id: str, on: bool) -> bool:
    key = str(session_id or "").strip()
    if not key:
        return False
    state = _load()
    if on:
        state[field][key] = int(time.time() * 1000)
    else:
        state[field].pop(key, None)
    return _save()


def set_pinned(session_id: str, pinned: bool) -> bool:
    with _lock:
        return _toggle("pins", session_id, pinned)


def set_note(session_id: str, note: Any) -> bool:
    """设置 / 清除备注。传空串或纯空白就是清除。"""
    key = str(session_id or "").strip()
    if not key:
        return False
    with _lock:
        state = _load()
        text = clean_note(note)
        if text:
            state["notes"][key] = text
        else:
            state["notes"].pop(key, None)
        return _save()


def snapshot() -> Dict[str, Dict[str, Any]]:
    """一次性取出两张表，列表接口按 session_id 直接查。"""
    with _lock:
        state = _load()
        return {
            "pins": dict(state["pins"]),
            "notes": dict(state["notes"]),
        }
