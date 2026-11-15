"""备份 zip 内的 ``gscore_backup_manifest.json``：结构定义与解析。

清单放 zip 根，``ZIP_STORED`` 且最后写入，前端可不解压直接读。
"""

import json
from typing import TypedDict

from gsuid_core.utils.backup.hints import BackupHintKind

BACKUP_MANIFEST_NAME = "gscore_backup_manifest.json"
MANIFEST_FORMAT = "gscore-backup"
MANIFEST_FORMAT_VERSION = 1


class BackupManifestHint(TypedDict):
    path: str
    plugin: str
    kind: BackupHintKind


class BackupManifest(TypedDict):
    format: str
    format_version: int
    created_at: str
    core_version: str
    platform: str
    file_id: str
    roots: list[str]
    hints: list[BackupManifestHint]
    file_count: int
    total_bytes: int


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def parse_manifest(raw: bytes | str) -> BackupManifest | None:
    """校验结构，不合法返回 ``None``（按无清单旧版备份处理）。"""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fmt = _as_str(data["format"]) if "format" in data else ""
    ver = _as_int(data["format_version"]) if "format_version" in data else 0
    if fmt != MANIFEST_FORMAT or ver != MANIFEST_FORMAT_VERSION:
        return None
    raw_roots = data["roots"] if "roots" in data else []
    roots = [r for r in raw_roots if isinstance(r, str)] if isinstance(raw_roots, list) else []
    hints: list[BackupManifestHint] = []
    raw_hints = data["hints"] if "hints" in data else []
    if isinstance(raw_hints, list):
        for h in raw_hints:
            if not isinstance(h, dict):
                continue
            hkind = _as_str(h["kind"]) if "kind" in h else ""
            if hkind not in ("user_data", "config", "regenerable"):
                continue
            hints.append(
                {
                    "path": _as_str(h["path"]) if "path" in h else "",
                    "plugin": _as_str(h["plugin"]) if "plugin" in h else "",
                    "kind": hkind,
                }
            )
    return {
        "format": fmt,
        "format_version": ver,
        "created_at": _as_str(data["created_at"]) if "created_at" in data else "",
        "core_version": _as_str(data["core_version"]) if "core_version" in data else "",
        "platform": _as_str(data["platform"]) if "platform" in data else "",
        "file_id": _as_str(data["file_id"]) if "file_id" in data else "",
        "roots": roots,
        "hints": hints,
        "file_count": _as_int(data["file_count"]) if "file_count" in data else 0,
        "total_bytes": _as_int(data["total_bytes"]) if "total_bytes" in data else 0,
    }
