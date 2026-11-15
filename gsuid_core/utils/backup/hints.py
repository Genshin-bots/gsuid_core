"""备份建议路径注册表：插件登记哪些目录是用户数据 / 配置 / 可重新下载的资源。

`rel` 一律是相对 ``gs_data_path`` 的 posix 路径；登记不要求路径存在。
"""

import threading
from typing import Literal
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Sequence

from gsuid_core.config import CONFIG_PATH
from gsuid_core.data_store import CONFIGS_PATH, PLUGINS_CONFIGS_PATH, gs_data_path
from gsuid_core.utils.path_safety import confine_to_root
from gsuid_core.utils.database.base_models import DB_PATH, is_sqlite_backend
from gsuid_core.utils.plugins_config.models import GsListStrConfig
from gsuid_core.utils.plugins_config.gs_config import backup_config

BackupHintKind = Literal["user_data", "config", "regenerable"]

# 树排序档位：user_data=0 / config=1 / 含建议子孙的目录=2 / 无建议=3 / regenerable=4
HINT_KIND_PRIORITY: dict[BackupHintKind, int] = {"user_data": 0, "config": 1, "regenerable": 4}
DIR_WITH_HINTS_PRIORITY = 2
NO_HINT_PRIORITY = 3

_CORE_PLUGIN = "GsCore"

# 备份目录配置的读-改-写锁：ensure_default_hints 与 set_backup_config 共用。
backup_config_lock = threading.Lock()


@dataclass(frozen=True)
class BackupHint:
    plugin: str
    rel: str
    kind: BackupHintKind
    desc: str
    default_selected: bool
    replaces: tuple[str, ...] = field(default_factory=tuple)


_HINTS: dict[str, BackupHint] = {}


def _normalize_rel(rel: str) -> str:
    return rel.strip().replace("\\", "/").strip("/")


def _to_rel(path: Path | str) -> str:
    return confine_to_root(str(path), gs_data_path).relative_to(gs_data_path.resolve()).as_posix()


def register_backup_path(
    plugin: str,
    path: Path | str,
    kind: BackupHintKind,
    desc: str,
    default_selected: bool | None = None,
    replaces: Sequence[str | Path] = (),
) -> BackupHint:
    rel = _to_rel(path)
    if rel == ".":
        raise ValueError("备份建议不能指向数据根目录")
    old_rels = tuple(r for r in (_to_rel(old) for old in replaces) if r != rel)
    selected = default_selected if default_selected is not None else kind == "user_data"
    if kind == "regenerable":
        selected = False
    hint = BackupHint(
        plugin=plugin,
        rel=rel,
        kind=kind,
        desc=desc,
        default_selected=selected,
        replaces=old_rels,
    )
    _HINTS[rel] = hint
    return hint


def unregister_backup_paths(plugin: str) -> None:
    for rel in [r for r, h in _HINTS.items() if h.plugin == plugin]:
        del _HINTS[rel]


def get_backup_hints() -> list[BackupHint]:
    return sorted(_HINTS.values(), key=lambda h: (HINT_KIND_PRIORITY[h.kind], h.rel))


def hint_for(rel: str) -> BackupHint | None:
    key = _normalize_rel(rel)
    if key in _HINTS:
        return _HINTS[key]
    return None


def hints_under(rel: str) -> list[BackupHint]:
    prefix = _normalize_rel(rel)
    return [h for h in get_backup_hints() if h.rel != prefix and (prefix == "" or h.rel.startswith(prefix + "/"))]


def merge_backup_dir(current: list[str], added: list[str], removed: list[str]) -> list[str]:
    """把 ``added``/``removed`` 增量归一成 data 内 rel posix 后套到 ``current``。

    越界路径抛 ``PathEscapeError``；removed 按归一化结果精确匹配，added 去重追加。
    """
    # 延迟 import 避免与 backup_core 模块级循环依赖
    from gsuid_core.utils.backup.backup_core import resolve_backup_src

    removed_set = {_to_rel(r) for r in removed}
    # 存量值可能是外系统绝对路径，按备份时的同一规则解析，解析不了的丢弃，不能让整次保存失败
    current_rel: list[str] = []
    for c in current:
        src = resolve_backup_src(c)
        if src is not None:
            current_rel.append(_to_rel(src))
    merged = [r for r in current_rel if r not in removed_set]
    for raw in added:
        rel = _to_rel(raw)
        if rel not in merged:
            merged.append(rel)
    return merged


def _resolved_rel(raw: str) -> str:
    # 延迟 import 避免与 backup_core 模块级循环依赖
    from gsuid_core.utils.backup.backup_core import resolve_backup_src

    src = resolve_backup_src(raw)
    if src is not None:
        return src.relative_to(gs_data_path.resolve()).as_posix()
    return _normalize_rel(raw)


def apply_default_hints(config_paths: list[str], applied: list[str]) -> tuple[list[str], list[str], bool]:
    from gsuid_core.utils.backup.backup_core import backup_dir_covers_path

    new_paths = list(config_paths)
    new_applied = list(applied)
    changed = False
    for hint in get_backup_hints():
        if hint.rel in new_applied:
            continue
        old_set = set(hint.replaces)
        moved_from = [p for p in new_paths if _resolved_rel(p) in old_set]
        if moved_from:
            # 插件搬了路径：用户勾选过旧路径就换成新路径，与 default_selected 无关
            new_paths = [p for p in new_paths if p not in moved_from]
            new_applied.append(hint.rel)
            changed = True
            if not backup_dir_covers_path(gs_data_path / hint.rel, new_paths):
                new_paths.append(hint.rel)
            continue
        if any(_resolved_rel(a) in old_set for a in new_applied):
            # 旧路径曾被自动勾选又被用户取消：新路径同样不自动补回
            new_applied.append(hint.rel)
            changed = True
            continue
        if not hint.default_selected:
            continue
        new_applied.append(hint.rel)
        changed = True
        if not backup_dir_covers_path(gs_data_path / hint.rel, new_paths):
            new_paths.append(hint.rel)
    return new_paths, new_applied, changed


def ensure_default_backup_hints() -> None:
    with backup_config_lock:
        dir_cfg = backup_config.get_config("backup_dir")
        applied_cfg = backup_config.get_config("backup_hint_applied")
        config_paths = list(dir_cfg.data) if isinstance(dir_cfg, GsListStrConfig) else []
        applied = list(applied_cfg.data) if isinstance(applied_cfg, GsListStrConfig) else []
        new_paths, new_applied, changed = apply_default_hints(config_paths, applied)
        if not changed:
            return
        if new_paths != config_paths:
            backup_config.set_config("backup_dir", new_paths)
        if new_applied != applied:
            backup_config.set_config("backup_hint_applied", new_applied)
        backup_config.update_config()


if is_sqlite_backend():
    register_backup_path(_CORE_PLUGIN, DB_PATH, "user_data", "主数据库：UID 绑定、Cookie、订阅等")
register_backup_path(_CORE_PLUGIN, CONFIG_PATH, "config", "核心配置：HOST/PORT、管理员、前缀等", True)
register_backup_path(_CORE_PLUGIN, CONFIGS_PATH, "config", "核心组件配置（备份、日志、数据库等）", True)
register_backup_path(_CORE_PLUGIN, PLUGINS_CONFIGS_PATH, "config", "各插件独立配置", True)
