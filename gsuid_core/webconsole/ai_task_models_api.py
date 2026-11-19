"""按任务覆盖模型（AI 配置 → 任务配置 → 高级面板）的 WebConsole API。

- ``GET    /api/ai_task_models``            全部任务类型 + 当前覆盖 + 实际生效配置
- ``PUT    /api/ai_task_models/{task_kind}`` 设置单个任务的覆盖
- ``DELETE /api/ai_task_models/{task_kind}`` 清除单个任务的覆盖（恢复默认档位）
- ``DELETE /api/ai_task_models``            清除全部覆盖

覆盖值语义见 ``ai_core/configs/models.resolve_task_route``。
"""

from __future__ import annotations

from typing import Literal, TypedDict
from dataclasses import dataclass

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.ai_core.agent_node import AgentNode, get_node, list_nodes
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.ai_core.configs.models import (
    TASK_ROUTE_LEVEL_REFS,
    TaskModelOverride,
    resolve_task_route,
    provider_config_exists,
    get_task_model_overrides,
    save_task_model_overrides,
    normalize_provider_config_name,
)
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.ai_core.configs.task_kinds import (
    TASK_GROUP_ORDER,
    TASK_CAPABILITY_AGENT,
    CAPABILITY_AGENT_TASK_PREFIX,
    TaskGroup,
    TaskLevel,
    AITaskKind,
    get_ai_task_kind,
    list_ai_task_kinds,
    capability_agent_task_kind,
)

from ._api_tags import PROVIDER_CONFIG

NodeSourceOut = Literal["builtin", "plugin", "user", "mcp"]
_NODE_SOURCE_ORDER: dict[NodeSourceOut, int] = {
    "builtin": 0,
    "plugin": 1,
    "user": 2,
    "mcp": 3,
}


class OverrideOut(TypedDict):
    primary: str
    secondary: str
    # False=覆盖指向的配置文件已不存在，运行时已回退默认档位
    valid: bool


class EffectiveOut(TypedDict):
    primary: str
    secondary: str
    source: Literal["default", "level", "config"]
    level: TaskLevel | None
    # 生效配置继承自哪个父任务 ID（如能力代理子项继承自 "capability_agent"）；None=非继承
    inherited_from: str | None


class TaskKindOut(TypedDict):
    id: str
    title: str
    desc: str
    group: TaskGroup
    default_level: TaskLevel
    follows_caller: bool
    fast: bool
    # 该任务在 token 统计（by_type）里的记账键，前端据此 join 用量
    stats_keys: list[str]
    override: OverrideOut | None
    effective: EffectiveOut
    # 父任务 ID（能力代理子节点指向 "capability_agent"）；顶层任务为 None
    parent_id: str | None
    # 能力代理节点来源；非能力代理子项为 None
    node_source: NodeSourceOut | None
    # 能力代理宿主插件名（builtin 为 "core"）；非能力代理子项或无宿主插件为 None
    plugin: str | None


class LevelRouteOut(TypedDict):
    primary: str
    secondary: str


class TaskModelsData(TypedDict):
    groups: list[TaskGroup]
    levels: dict[str, LevelRouteOut]
    kinds: list[TaskKindOut]
    # 已保存但当前未登记的任务 ID（插件卸载后残留），前端可提示清理
    orphans: list[str]


class TaskModelsResp(TypedDict):
    status: int
    msg: str
    data: TaskModelsData | None


class TaskOverrideBody(BaseModel):
    primary: str
    secondary: str = ""


@dataclass(frozen=True)
class _TaskKindEntry:
    kind: AITaskKind
    parent_id: str | None = None
    node_source: NodeSourceOut | None = None
    plugin: str | None = None


def _delegable_node_source(node: AgentNode | None) -> NodeSourceOut | None:
    """判定节点是否为可单独配置模型的能力代理（排除 persona 与 capability_evaluator）。"""
    if node is None or node.node_id == "capability_evaluator":
        return None
    if node.source == "builtin":
        return "builtin"
    if node.source == "plugin":
        return "plugin"
    if node.source == "user":
        return "user"
    if node.source == "mcp":
        return "mcp"
    return None


def _projected_capability_entries() -> list[_TaskKindEntry]:
    """实时投影全部可委派能力代理节点，免维护独立注册表同步。"""
    staged: list[tuple[int, _TaskKindEntry]] = []
    for node in list_nodes(include_persona=False):
        source = _delegable_node_source(node)
        if source is None:
            continue
        kind_id = capability_agent_task_kind(node.node_id)
        title = node.display_name.strip() or node.node_id
        desc = node.when_to_use.strip() or f"能力代理节点 {node.node_id}"
        plugin = node.plugin.strip() or None
        kind = AITaskKind(
            id=kind_id,
            title=title,
            desc=desc,
            group="agent",
            default_level="high",
            stats_keys=(kind_id.lower(),),
        )
        staged.append(
            (
                _NODE_SOURCE_ORDER[source],
                _TaskKindEntry(
                    kind=kind,
                    parent_id=TASK_CAPABILITY_AGENT,
                    node_source=source,
                    plugin=plugin,
                ),
            )
        )
    staged.sort(key=lambda pair: pair[0])
    return [entry for _, entry in staged]


def _all_task_entries() -> list[_TaskKindEntry]:
    entries: list[_TaskKindEntry] = []
    cap_children = _projected_capability_entries()
    inserted_children = False
    for kind in list_ai_task_kinds():
        entries.append(_TaskKindEntry(kind=kind))
        if kind.id == TASK_CAPABILITY_AGENT:
            entries.extend(cap_children)
            inserted_children = True
    if not inserted_children:
        entries.extend(cap_children)
    return entries


def _is_known_task_kind(task_kind: str) -> bool:
    if get_ai_task_kind(task_kind) is not None:
        return True
    if task_kind.startswith(CAPABILITY_AGENT_TASK_PREFIX):
        node_id = task_kind[len(CAPABILITY_AGENT_TASK_PREFIX) :]
        return bool(node_id) and _delegable_node_source(get_node(node_id)) is not None
    return False


def _override_valid(ov: TaskModelOverride) -> bool:
    if ov.primary in TASK_ROUTE_LEVEL_REFS:
        return True
    if not provider_config_exists(ov.primary):
        return False
    return not ov.secondary or provider_config_exists(ov.secondary)


def _kind_out(entry: _TaskKindEntry, overrides: dict[str, TaskModelOverride]) -> TaskKindOut:
    kind = entry.kind
    route = resolve_task_route(kind.default_level, kind.id)
    override: OverrideOut | None = None
    if kind.id in overrides:
        ov = overrides[kind.id]
        override = {"primary": ov.primary, "secondary": ov.secondary, "valid": _override_valid(ov)}
    return {
        "id": kind.id,
        "title": kind.title,
        "desc": kind.desc,
        "group": kind.group,
        "default_level": kind.default_level,
        "follows_caller": kind.follows_caller,
        "fast": kind.fast,
        "stats_keys": list(kind.stats_keys),
        "override": override,
        "effective": {
            "primary": route.primary,
            "secondary": route.secondary,
            "source": route.source,
            "level": route.level,
            "inherited_from": route.inherited_from,
        },
        "parent_id": entry.parent_id,
        "node_source": entry.node_source,
        "plugin": entry.plugin,
    }


def _build_data() -> TaskModelsData:
    overrides = get_task_model_overrides()
    entries = _all_task_entries()
    levels: dict[str, LevelRouteOut] = {}
    for lvl in ("high", "low"):
        route = resolve_task_route(lvl)
        levels[lvl] = {"primary": route.primary, "secondary": route.secondary}
    known = {e.kind.id for e in entries}
    return {
        "groups": list(TASK_GROUP_ORDER),
        "levels": levels,
        "kinds": [_kind_out(e, overrides) for e in entries],
        "orphans": sorted(k for k in overrides if k not in known),
    }


def _fail(msg: str) -> TaskModelsResp:
    return {"status": 1, "msg": msg, "data": None}


def _ok() -> TaskModelsResp:
    return {"status": 0, "msg": "ok", "data": _build_data()}


@app.get("/api/ai_task_models", summary="获取全部 AI 任务类型及模型覆盖", tags=PROVIDER_CONFIG)
async def get_ai_task_models(_: SessionRecord = Depends(require_auth)) -> TaskModelsResp:
    return _ok()


@app.put("/api/ai_task_models/{task_kind}", summary="设置单个任务的模型覆盖", tags=PROVIDER_CONFIG)
async def set_ai_task_model(
    task_kind: str,
    body: TaskOverrideBody,
    _: SessionRecord = Depends(require_admin),
) -> TaskModelsResp:
    if not _is_known_task_kind(task_kind):
        return _fail(f"未知的任务类型: {task_kind}")
    primary = body.primary.strip()
    secondary = body.secondary.strip()
    if not primary:
        return _fail("主配置不能为空；要恢复默认请调用 DELETE")
    if primary in TASK_ROUTE_LEVEL_REFS:
        secondary = ""
    else:
        primary = normalize_provider_config_name(primary)
        if not provider_config_exists(primary):
            return _fail(f"配置文件 '{body.primary.strip()}' 不存在")
    if secondary:
        secondary = normalize_provider_config_name(secondary)
        if not provider_config_exists(secondary):
            return _fail(f"配置文件 '{body.secondary.strip()}' 不存在")
        if secondary == primary:
            return _fail("备用配置不能与主配置相同")

    overrides = get_task_model_overrides()
    overrides[task_kind] = TaskModelOverride(primary, secondary)
    if not save_task_model_overrides(overrides):
        return _fail("保存失败")
    return _ok()


@app.delete("/api/ai_task_models/{task_kind}", summary="清除单个任务的模型覆盖", tags=PROVIDER_CONFIG)
async def clear_ai_task_model(
    task_kind: str,
    _: SessionRecord = Depends(require_admin),
) -> TaskModelsResp:
    overrides = get_task_model_overrides()
    if task_kind in overrides:
        del overrides[task_kind]
        if not save_task_model_overrides(overrides):
            return _fail("保存失败")
    return _ok()


@app.delete("/api/ai_task_models", summary="清除全部任务的模型覆盖", tags=PROVIDER_CONFIG)
async def clear_all_ai_task_models(_: SessionRecord = Depends(require_admin)) -> TaskModelsResp:
    if not save_task_model_overrides({}):
        return _fail("保存失败")
    return _ok()
