"""同一条命令回执的建议。第二次起直接读这里，不再读源码。"""

from __future__ import annotations

import json
from typing import TypedDict
from threading import Lock

from gsuid_core.data_store import AI_CORE_PATH, get_res_path

_LOCK = Lock()


class RecallItem(TypedDict):
    plugin: str
    receipt: str
    advice: str


def _path():
    return get_res_path(AI_CORE_PATH) / "aside_recall.json"


def normalize_receipt(text: str) -> str:
    return " ".join(text.split())[:800]


def _key(plugin: str, receipt: str) -> str:
    return f"{plugin}\n{normalize_receipt(receipt)}"


def _load() -> dict[str, RecallItem]:
    path = _path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    items_raw = raw["items"] if isinstance(raw, dict) and "items" in raw else None
    items = items_raw if isinstance(items_raw, dict) else {}
    kept: dict[str, RecallItem] = {}
    for key, item in items.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            continue
        plugin = item["plugin"] if "plugin" in item else None
        receipt = item["receipt"] if "receipt" in item else None
        advice = item["advice"] if "advice" in item else None
        if isinstance(plugin, str) and isinstance(receipt, str) and isinstance(advice, str) and advice.strip():
            kept[key] = {"plugin": plugin, "receipt": receipt, "advice": advice}
    return kept


def _save(items: dict[str, RecallItem]) -> None:
    path = _path()
    path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def lookup_advice(plugin: str, receipt: str) -> str:
    if not plugin or not receipt.strip():
        return ""
    with _LOCK:
        item = _load().get(_key(plugin, receipt))
    if item is None:
        return ""
    return item["advice"]


def save_advice(plugin: str, receipt: str, advice: str) -> None:
    text = advice.strip()
    if not plugin or not receipt.strip() or not text:
        return
    with _LOCK:
        items = _load()
        items[_key(plugin, receipt)] = {
            "plugin": plugin,
            "receipt": normalize_receipt(receipt),
            "advice": text[:2000],
        }
        _save(items)
