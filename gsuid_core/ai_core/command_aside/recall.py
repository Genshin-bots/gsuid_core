"""同一条命令回执的建议。第二次起直接读这里，不再读源码。"""

from __future__ import annotations

import re
import json
import time
from typing import TypedDict
from threading import Lock

from gsuid_core.data_store import AI_CORE_PATH, get_res_path

_LOCK = Lock()
_MAX_RECALL_ITEMS = 200
_RECALL_TTL_SECONDS = 7 * 86400.0
_LEGACY_PLACEHOLDER = "图片过大，未放入会话"


class RecallItem(TypedDict):
    plugin: str
    trigger_id: str
    receipt: str
    advice: str
    updated_at: float


def _path():
    return get_res_path(AI_CORE_PATH) / "aside_recall.json"


def skeleton_receipt(text: str) -> str:
    # 去除数字、ID、URL、变量并提取稳定骨架，避免同插件下参数污染
    s = text.strip()
    s = re.sub(r"https?://\S+", "{URL}", s)
    s = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?", "{IP}", s)
    s = re.sub(r"【[^】\n]{3,60}】", "{VAR}", s)
    s = re.sub(r"\b\d{2,}\b", "{NUM}", s)
    return " ".join(s.split())[:600]


def normalize_receipt(text: str) -> str:
    return skeleton_receipt(text)


def _key(plugin: str, trigger_id: str, receipt: str) -> str:
    return f"{plugin}\n{trigger_id}\n{skeleton_receipt(receipt)}"


def _legacy_whitespace_key(plugin: str, receipt: str) -> str:
    # 旧文件的键是空白折叠，不是骨架
    return f"{plugin}\n{' '.join(receipt.split())[:800]}"


def _is_polluted(receipt: str, advice: str) -> bool:
    return _LEGACY_PLACEHOLDER in receipt or _LEGACY_PLACEHOLDER in advice


def _stamp(item: object, legacy_mtime: float) -> float | None:
    if not isinstance(item, dict) or "updated_at" not in item:
        return legacy_mtime
    raw = item["updated_at"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


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
    now = time.time()
    legacy_mtime = path.stat().st_mtime
    for key, item in items.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            continue
        plugin = item["plugin"] if "plugin" in item else None
        receipt = item["receipt"] if "receipt" in item else None
        advice = item["advice"] if "advice" in item else None
        trigger_id = item["trigger_id"] if "trigger_id" in item else ""
        updated_at = _stamp(item, legacy_mtime)
        if not (isinstance(plugin, str) and isinstance(receipt, str) and isinstance(advice, str) and advice.strip()):
            continue
        if not isinstance(trigger_id, str) or updated_at is None:
            continue
        if _is_polluted(receipt, advice):
            continue
        if now - updated_at > _RECALL_TTL_SECONDS:
            continue
        kept[key] = {
            "plugin": plugin,
            "trigger_id": trigger_id,
            "receipt": receipt,
            "advice": advice,
            "updated_at": updated_at,
        }
    return kept


def _save(items: dict[str, RecallItem]) -> None:
    path = _path()
    # 限制总容量，按更新时间保留最新的条目
    sorted_items = sorted(
        items.items(),
        key=lambda pair: pair[1]["updated_at"],
        reverse=True,
    )[:_MAX_RECALL_ITEMS]
    data = {"items": dict(sorted_items)}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_item(items: dict[str, RecallItem], plugin: str, receipt: str, trigger_id: str) -> RecallItem | None:
    candidates = [
        _key(plugin, trigger_id, receipt),
        _key(plugin, "", receipt),
        _legacy_whitespace_key(plugin, receipt),
        f"{plugin}\n{skeleton_receipt(receipt)}",
    ]
    for key in candidates:
        if key in items:
            return items[key]
    return None


def lookup_advice(plugin: str, receipt: str, trigger_id: str = "") -> str:
    if not plugin or not receipt.strip():
        return ""
    with _LOCK:
        item = _find_item(_load(), plugin, receipt, trigger_id)
    if item is None:
        return ""
    return item["advice"]


def save_advice(plugin: str, receipt: str, advice: str, trigger_id: str = "") -> None:
    text = advice.strip()
    if not plugin or not receipt.strip() or not text:
        return
    if _is_polluted(receipt, text):
        return
    with _LOCK:
        items = _load()
        k = _key(plugin, trigger_id, receipt)
        items[k] = {
            "plugin": plugin,
            "trigger_id": trigger_id,
            "receipt": skeleton_receipt(receipt),
            "advice": text[:2000],
            "updated_at": time.time(),
        }
        _save(items)
