"""人格重置期间的写入冻结标记（进程内，单进程部署见 ``supervisor.py``）。

叶子模块：不依赖 ai_core 其它子模块，供观察入队、摄入、手账、好感、人格状态等写入口廉价判断。
``self:{bot}:{persona}`` 归属人格；其余 scope（群 / 用户）归入共享组，只在清全局图谱时冻结。
"""

from __future__ import annotations

from collections.abc import Iterable

_frozen_personas: dict[str, int] = {}
_frozen_scope_keys: dict[str, int] = {}
_shared_depth = 0


def _bump(table: dict[str, int], key: str, step: int) -> None:
    left = (table[key] if key in table else 0) + step
    if left > 0:
        table[key] = left
    elif key in table:
        del table[key]


def freeze(personas: Iterable[str], *, shared: bool, scope_keys: Iterable[str] = ()) -> None:
    """进入冻结。可重入：同一人格冻结几次就要解冻几次。"""
    global _shared_depth
    for name in personas:
        _bump(_frozen_personas, name, 1)
    for key in scope_keys:
        _bump(_frozen_scope_keys, key, 1)
    if shared:
        _shared_depth += 1


def unfreeze(personas: Iterable[str], *, shared: bool, scope_keys: Iterable[str] = ()) -> None:
    global _shared_depth
    for name in personas:
        _bump(_frozen_personas, name, -1)
    for key in scope_keys:
        _bump(_frozen_scope_keys, key, -1)
    if shared and _shared_depth > 0:
        _shared_depth -= 1


def is_persona_frozen(persona_name: str | None) -> bool:
    return bool(persona_name) and persona_name in _frozen_personas


def is_scope_frozen(scope_key: str) -> bool:
    """``self:{bot}:{persona}`` 看人格；旧 ``self:{bot}`` 看显式键；其余看共享组。"""
    if scope_key in _frozen_scope_keys:
        return True
    if scope_key.startswith("self:"):
        parts = scope_key.split(":", 2)
        return len(parts) == 3 and parts[2] in _frozen_personas
    return _shared_depth > 0
