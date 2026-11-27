"""按任务路由与覆盖模型（AI 配置 → 任务模型路由图谱）的 WebConsole API。

- ``GET    /api/ai_task_models``            全部任务类型 + 当前覆盖 + 实际生效配置 + 路由图谱
- ``PUT    /api/ai_task_models/graph``      保存星型节点模型路由图谱并同步高低档与任务覆盖
- ``PUT    /api/ai_task_models/{task_kind}`` 设置单个任务的覆盖（同步更新已存图谱）
- ``DELETE /api/ai_task_models/{task_kind}`` 清除单个任务的覆盖（恢复默认档位并同步图谱）
- ``DELETE /api/ai_task_models``            清除全部覆盖并重置图谱

覆盖值语义见 ``ai_core/configs/models.resolve_task_route``。
"""

from __future__ import annotations

import math
from typing import Literal, TypedDict
from dataclasses import dataclass

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.ai_core.agent_node import AgentNode, get_node, list_nodes
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.ai_core.configs.models import (
    TASK_ROUTE_LEVEL_REFS,
    TaskGraphPos,
    TaskModelGraph,
    TaskGraphHubNode,
    TaskGraphNodeRole,
    TaskModelOverride,
    get_level_primary,
    resolve_task_route,
    get_level_secondary,
    get_task_model_graph,
    save_task_model_graph,
    provider_config_exists,
    get_task_model_overrides,
    save_task_model_overrides,
    normalize_provider_config_name,
)
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.configs.task_kinds import (
    TASK_CHAT,
    TASK_GROUP_ORDER,
    TASK_TURN_ROUTER,
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


class GraphPosOut(TypedDict):
    x: float
    y: float


class GraphHubNodeOut(TypedDict):
    id: str
    primary: str
    secondary: str
    role: TaskGraphNodeRole
    valid: bool
    pos: GraphPosOut | None


class GraphDataOut(TypedDict):
    nodes: list[GraphHubNodeOut]
    edges: dict[str, str]
    split_subagents: list[str]
    task_positions: dict[str, GraphPosOut]


class TaskModelsData(TypedDict):
    groups: list[TaskGroup]
    levels: dict[str, LevelRouteOut]
    kinds: list[TaskKindOut]
    # 已保存但当前未登记的任务 ID（插件卸载后残留），前端可提示清理
    orphans: list[str]
    graph: GraphDataOut


class TaskModelsResp(TypedDict):
    status: int
    msg: str
    data: TaskModelsData | None


class TaskOverrideBody(BaseModel):
    primary: str
    secondary: str = ""


class TaskGraphPosIn(BaseModel):
    x: float
    y: float


class TaskGraphNodeIn(BaseModel):
    id: str
    primary: str = ""
    secondary: str = ""
    role: TaskGraphNodeRole = "custom"
    pos: TaskGraphPosIn | None = None


class TaskGraphBody(BaseModel):
    nodes: list[TaskGraphNodeIn]
    edges: dict[str, str] = {}
    split_subagents: list[str] = []
    task_positions: dict[str, TaskGraphPosIn] = {}


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


def _is_follows_caller_kind(task_kind: str) -> bool:
    meta = get_ai_task_kind(task_kind)
    return meta is not None and meta.follows_caller


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


def _hub_node_valid(primary: str, secondary: str) -> bool:
    if primary and not provider_config_exists(primary):
        return False
    return not secondary or provider_config_exists(secondary)


def _find_role_node_id(nodes: list[GraphHubNodeOut], role: TaskLevel) -> str:
    for node in nodes:
        if node["role"] == role:
            return node["id"]
    return nodes[0]["id"]


def _synthesize_default_graph(
    entries: list[_TaskKindEntry],
    overrides: dict[str, TaskModelOverride],
) -> GraphDataOut:
    """首次升级或新建实例无落库图谱时，按存量高低档与覆盖合成双星核（或多星核）拓扑。"""
    high_p = normalize_provider_config_name(get_level_primary("high"))
    high_s = normalize_provider_config_name(get_level_secondary("high")) if high_p else ""
    if high_s == high_p:
        high_s = ""
    low_p = normalize_provider_config_name(get_level_primary("low"))
    low_s = normalize_provider_config_name(get_level_secondary("low")) if low_p else ""
    if low_s == low_p:
        low_s = ""

    nodes: list[GraphHubNodeOut] = [
        {
            "id": "slot_high",
            "primary": high_p,
            "secondary": high_s,
            "role": "high",
            "valid": _hub_node_valid(high_p, high_s),
            "pos": None,
        },
        {
            "id": "slot_low",
            "primary": low_p,
            "secondary": low_s,
            "role": "low",
            "valid": _hub_node_valid(low_p, low_s),
            "pos": None,
        },
    ]

    routable_top = [e.kind for e in entries if e.parent_id is None and not e.kind.follows_caller]
    routable_top_ids = {k.id for k in routable_top}
    valid_sub_ids = {e.kind.id for e in entries if e.parent_id == TASK_CAPABILITY_AGENT}

    edges: dict[str, str] = {k.id: ("slot_high" if k.default_level == "high" else "slot_low") for k in routable_top}
    split_subagents: list[str] = []
    custom_seq = 1

    for tk, ov in overrides.items():
        if tk not in routable_top_ids and tk not in valid_sub_ids:
            continue
        if tk in valid_sub_ids and tk not in split_subagents:
            split_subagents.append(tk)
        if ov.primary == "@high":
            edges[tk] = "slot_high"
            continue
        if ov.primary == "@low":
            edges[tk] = "slot_low"
            continue
        norm_p = normalize_provider_config_name(ov.primary)
        if not norm_p:
            continue
        norm_s = normalize_provider_config_name(ov.secondary)
        if norm_s == norm_p:
            norm_s = ""

        # 显式配置只匹配 custom 节点：连上 slot 节点意味着跟随档位（保存时物化为档位引用），
        # 而显式覆盖的语义是冻结在该配置，二者不能混
        matched_id = ""
        for existing in nodes:
            if existing["role"] == "custom" and existing["primary"] == norm_p and existing["secondary"] == norm_s:
                matched_id = existing["id"]
                break
        if not matched_id:
            matched_id = f"hub_{custom_seq}"
            custom_seq += 1
            nodes.append(
                {
                    "id": matched_id,
                    "primary": norm_p,
                    "secondary": norm_s,
                    "role": "custom",
                    "valid": _hub_node_valid(norm_p, norm_s),
                    "pos": None,
                }
            )
        edges[tk] = matched_id

    return {
        "nodes": nodes,
        "edges": edges,
        "split_subagents": split_subagents,
        "task_positions": {},
    }


def _wire_target_from_override(
    ov: TaskModelOverride,
    nodes: list[GraphHubNodeOut],
    node_ids: set[str],
    high_anchor_id: str,
    low_anchor_id: str,
) -> str:
    """按覆盖反推连线目标（缺边任务的兜底）：档位引用回锚点，显式配置匹配或新建非锚点枢纽。

    覆盖已失效（配置文件被删）时运行时本就回退档位解析，返回空串由调用方回默认锚点。
    """
    if ov.primary == "@high":
        return high_anchor_id
    if ov.primary == "@low":
        return low_anchor_id
    norm_p = normalize_provider_config_name(ov.primary)
    if not norm_p or not provider_config_exists(norm_p):
        return ""
    norm_s = normalize_provider_config_name(ov.secondary)
    if norm_s == norm_p:
        norm_s = ""
    anchor_ids = {high_anchor_id, low_anchor_id}
    for n in nodes:
        if n["id"] not in anchor_ids and n["primary"] == norm_p and n["secondary"] == norm_s:
            return n["id"]
    seq = 1
    while f"hub_{seq}" in node_ids:
        seq += 1
    new_id = f"hub_{seq}"
    node_ids.add(new_id)
    nodes.append(
        {
            "id": new_id,
            "primary": norm_p,
            "secondary": norm_s,
            "role": "custom",
            "valid": _hub_node_valid(norm_p, norm_s),
            "pos": None,
        }
    )
    return new_id


def _build_graph_out(
    entries: list[_TaskKindEntry],
    overrides: dict[str, TaskModelOverride],
) -> GraphDataOut:
    saved = get_task_model_graph()
    if saved is None:
        return _synthesize_default_graph(entries, overrides)

    nodes: list[GraphHubNodeOut] = [
        {
            "id": n.id,
            "primary": n.primary,
            "secondary": n.secondary,
            "role": n.role,
            "valid": _hub_node_valid(n.primary, n.secondary),
            "pos": {"x": n.pos.x, "y": n.pos.y} if n.pos is not None else None,
        }
        for n in saved.nodes
    ]
    node_ids = {n["id"] for n in nodes}
    role_high_id = _find_role_node_id(nodes, "high")
    role_low_id = _find_role_node_id(nodes, "low")
    # 晚注册任务缺省锚点对齐 chat / turn_router 所在枢纽，与运行时 high/low 槽位保持完全一致
    high_anchor_id = (
        saved.edges[TASK_CHAT] if TASK_CHAT in saved.edges and saved.edges[TASK_CHAT] in node_ids else role_high_id
    )
    low_anchor_id = (
        saved.edges[TASK_TURN_ROUTER]
        if TASK_TURN_ROUTER in saved.edges and saved.edges[TASK_TURN_ROUTER] in node_ids
        else role_low_id
    )

    routable_top = [e.kind for e in entries if e.parent_id is None and not e.kind.follows_caller]
    valid_sub_ids = {e.kind.id for e in entries if e.parent_id == TASK_CAPABILITY_AGENT}

    split_subagents: list[str] = [s for s in saved.split_subagents if s in valid_sub_ids]
    for tk in overrides:
        if tk in valid_sub_ids and tk not in split_subagents:
            split_subagents.append(tk)

    edges: dict[str, str] = {}
    for k in routable_top:
        if k.id in saved.edges and saved.edges[k.id] in node_ids:
            edges[k.id] = saved.edges[k.id]
            continue
        # 缺边（晚注册 / orphan 重装后枢纽被删）时优先按覆盖反推，保持连线与运行时解析一致
        derived = ""
        if k.id in overrides:
            derived = _wire_target_from_override(overrides[k.id], nodes, node_ids, high_anchor_id, low_anchor_id)
        if derived:
            edges[k.id] = derived
        else:
            edges[k.id] = high_anchor_id if k.default_level == "high" else low_anchor_id

    cap_parent_node = edges[TASK_CAPABILITY_AGENT] if TASK_CAPABILITY_AGENT in edges else high_anchor_id
    for sub_id in split_subagents:
        if sub_id in saved.edges and saved.edges[sub_id] in node_ids:
            edges[sub_id] = saved.edges[sub_id]
            continue
        derived = ""
        if sub_id in overrides:
            derived = _wire_target_from_override(overrides[sub_id], nodes, node_ids, high_anchor_id, low_anchor_id)
        edges[sub_id] = derived if derived else cap_parent_node

    task_positions: dict[str, GraphPosOut] = {tk: {"x": p.x, "y": p.y} for tk, p in saved.task_positions.items()}
    return {
        "nodes": nodes,
        "edges": edges,
        "split_subagents": split_subagents,
        "task_positions": task_positions,
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
        "graph": _build_graph_out(entries, overrides),
    }


def _fail(msg: str) -> TaskModelsResp:
    return {"status": 1, "msg": msg, "data": None}


def _ok() -> TaskModelsResp:
    return {"status": 0, "msg": "ok", "data": _build_data()}


@app.get("/api/ai_task_models", summary="获取全部 AI 任务类型及模型覆盖", tags=PROVIDER_CONFIG)
async def get_ai_task_models(_: SessionRecord = Depends(require_auth)) -> TaskModelsResp:
    return _ok()


@app.put("/api/ai_task_models/graph", summary="保存星型节点模型路由图谱", tags=PROVIDER_CONFIG)
async def save_ai_task_model_graph(
    body: TaskGraphBody,
    _: SessionRecord = Depends(require_admin),
) -> TaskModelsResp:
    if not body.nodes:
        return _fail("图谱至少需要保留一个配置节点")

    normalized_nodes: list[TaskGraphHubNode] = []
    node_by_id: dict[str, TaskGraphHubNode] = {}
    seen_default_roles: set[str] = set()
    for item in body.nodes:
        nid = item.id.strip()
        if not nid:
            return _fail("配置节点 ID 不能为空")
        if nid in node_by_id:
            return _fail(f"重复的配置节点 ID: {nid}")
        if item.role in ("high", "low"):
            if item.role in seen_default_roles:
                return _fail(f"默认角色 '{item.role}' 节点最多只能有一个")
            seen_default_roles.add(item.role)
        if item.pos is not None and not (math.isfinite(item.pos.x) and math.isfinite(item.pos.y)):
            return _fail("配置节点坐标必须为有限数值")
        raw_p = item.primary.strip()
        raw_s = item.secondary.strip()
        if not raw_p and raw_s:
            return _fail("尚未选择主配置的节点不能设置备用配置")
        norm_p = ""
        norm_s = ""
        if raw_p:
            norm_p = normalize_provider_config_name(raw_p)
            if not provider_config_exists(norm_p):
                return _fail(f"配置文件 '{raw_p}' 不存在")
        if raw_s:
            norm_s = normalize_provider_config_name(raw_s)
            if not provider_config_exists(norm_s):
                return _fail(f"配置文件 '{raw_s}' 不存在")
            if norm_s == norm_p:
                return _fail("备用配置不能与主配置相同")
        pos = TaskGraphPos(item.pos.x, item.pos.y) if item.pos is not None else None
        hub = TaskGraphHubNode(id=nid, primary=norm_p, secondary=norm_s, role=item.role, pos=pos)
        normalized_nodes.append(hub)
        node_by_id[nid] = hub

    for p in body.task_positions.values():
        if not (math.isfinite(p.x) and math.isfinite(p.y)):
            return _fail("任务节点坐标必须为有限数值")

    entries = _all_task_entries()
    known_entry_ids = {e.kind.id for e in entries}
    routable_top = [e.kind for e in entries if e.parent_id is None and not e.kind.follows_caller]
    valid_sub_ids = {e.kind.id for e in entries if e.parent_id == TASK_CAPABILITY_AGENT}

    split_subagents: list[str] = []
    for raw_sub in body.split_subagents:
        sub_id = raw_sub.strip()
        if not sub_id:
            continue
        if sub_id not in valid_sub_ids:
            return _fail(f"未知的能力代理子任务: {sub_id}")
        if sub_id not in split_subagents:
            split_subagents.append(sub_id)

    role_high_id = next((n.id for n in normalized_nodes if n.role == "high"), normalized_nodes[0].id)
    role_low_id = next((n.id for n in normalized_nodes if n.role == "low"), normalized_nodes[0].id)
    raw_chat_target = body.edges[TASK_CHAT].strip() if TASK_CHAT in body.edges else ""
    raw_router_target = body.edges[TASK_TURN_ROUTER].strip() if TASK_TURN_ROUTER in body.edges else ""
    high_default_id = raw_chat_target if raw_chat_target in node_by_id else role_high_id
    low_default_id = raw_router_target if raw_router_target in node_by_id else role_low_id

    resolved_edges: dict[str, str] = {}
    for k in routable_top:
        target_id = body.edges[k.id].strip() if k.id in body.edges else ""
        if not target_id:
            target_id = high_default_id if k.default_level == "high" else low_default_id
        if target_id not in node_by_id:
            return _fail(f"任务 '{k.id}' 连接了不存在的节点 '{target_id}'")
        if not node_by_id[target_id].primary:
            return _fail(f"任务「{k.title}」所连接的节点尚未选择模型配置，请先完成选择")
        resolved_edges[k.id] = target_id

    cap_parent_id = (
        resolved_edges[TASK_CAPABILITY_AGENT] if TASK_CAPABILITY_AGENT in resolved_edges else high_default_id
    )
    for sub_id in split_subagents:
        target_id = body.edges[sub_id].strip() if sub_id in body.edges else ""
        if not target_id:
            target_id = cap_parent_id
        if target_id not in node_by_id:
            return _fail(f"子代理 '{sub_id}' 连接了不存在的节点 '{target_id}'")
        if not node_by_id[target_id].primary:
            return _fail("已拆分的能力代理所连接的节点尚未选择模型配置，请先完成选择")
        resolved_edges[sub_id] = target_id

    chat_hub = node_by_id[resolved_edges[TASK_CHAT]] if TASK_CHAT in resolved_edges else normalized_nodes[0]
    router_hub = node_by_id[resolved_edges[TASK_TURN_ROUTER]] if TASK_TURN_ROUTER in resolved_edges else chat_hub

    # 保留插件临时卸载期间尚未登记的 orphan 覆盖与连线，避免保存图谱时静默丢失
    existing_overrides = get_task_model_overrides()
    # 除 orphan 外，follows_caller 任务的存量覆盖也原样保留：它们在运行时生效，
    # 静默丢弃会改变线上行为；新增已被 PUT 拒绝，清理走 DELETE
    overrides: dict[str, TaskModelOverride] = {
        tk: ov for tk, ov in existing_overrides.items() if tk not in known_entry_ids or _is_follows_caller_kind(tk)
    }
    prev_graph = get_task_model_graph()
    if prev_graph is not None:
        for tk, prev_target in prev_graph.edges.items():
            if tk not in known_entry_ids and prev_target in node_by_id:
                resolved_edges[tk] = prev_target
        for prev_sub in prev_graph.split_subagents:
            if prev_sub not in known_entry_ids and prev_sub not in split_subagents:
                split_subagents.append(prev_sub)

    chat_anchor_id = resolved_edges[TASK_CHAT] if TASK_CHAT in resolved_edges else ""
    router_anchor_id = resolved_edges[TASK_TURN_ROUTER] if TASK_TURN_ROUTER in resolved_edges else ""

    # 物化按节点身份而非配置值：连本档锚点不写覆盖（随槽位动态走），连对方锚点写
    # 档位引用（锚点随旧槽位接口同步变更时仍能跟随），连其余节点写显式快照——
    # 非锚点节点只在保存图谱时变更、保存即重算，快照不会漂移
    for k in routable_top:
        target_id = resolved_edges[k.id]
        target_hub = node_by_id[target_id]
        if k.default_level == "high":
            own_anchor, other_anchor, other_ref = chat_anchor_id, router_anchor_id, "@low"
        else:
            own_anchor, other_anchor, other_ref = router_anchor_id, chat_anchor_id, "@high"
        if target_id == own_anchor:
            continue
        if target_id == other_anchor:
            overrides[k.id] = TaskModelOverride(other_ref, "")
        else:
            overrides[k.id] = TaskModelOverride(target_hub.primary, target_hub.secondary)
    for sub_id in split_subagents:
        if sub_id in valid_sub_ids:
            target_id = resolved_edges[sub_id]
            target_hub = node_by_id[target_id]
            if target_id == chat_anchor_id:
                overrides[sub_id] = TaskModelOverride("@high", "")
            elif target_id == router_anchor_id:
                overrides[sub_id] = TaskModelOverride("@low", "")
            else:
                overrides[sub_id] = TaskModelOverride(target_hub.primary, target_hub.secondary)

    task_positions: dict[str, TaskGraphPos] = {
        tk.strip(): TaskGraphPos(p.x, p.y) for tk, p in body.task_positions.items() if tk.strip()
    }
    graph_obj = TaskModelGraph(
        nodes=tuple(normalized_nodes),
        edges=resolved_edges,
        split_subagents=tuple(split_subagents),
        task_positions=task_positions,
    )

    ok_slots = (
        ai_config.set_config("high_level_provider_config_name", chat_hub.primary)
        and ai_config.set_config("high_level_2nd_provider_config_name", chat_hub.secondary)
        and ai_config.set_config("low_level_provider_config_name", router_hub.primary)
        and ai_config.set_config("low_level_2nd_provider_config_name", router_hub.secondary)
    )
    if not ok_slots or not save_task_model_overrides(overrides) or not save_task_model_graph(graph_obj):
        return _fail("保存失败")
    return _ok()


def _sync_saved_graph_for_single_task(task_kind: str, override: TaskModelOverride | None) -> None:
    """单任务 PUT/DELETE 更新覆盖后就地同步已存图谱连线，保持 GET 与后续图谱保存一致。"""
    saved = get_task_model_graph()
    if saved is None:
        return
    nodes = list(saved.nodes)
    node_ids = {n.id for n in nodes}
    role_high_id = next((n.id for n in nodes if n.role == "high"), nodes[0].id)
    role_low_id = next((n.id for n in nodes if n.role == "low"), nodes[0].id)
    high_anchor = (
        saved.edges[TASK_CHAT] if TASK_CHAT in saved.edges and saved.edges[TASK_CHAT] in node_ids else role_high_id
    )
    low_anchor = (
        saved.edges[TASK_TURN_ROUTER]
        if TASK_TURN_ROUTER in saved.edges and saved.edges[TASK_TURN_ROUTER] in node_ids
        else role_low_id
    )

    edges = dict(saved.edges)
    split_subagents = list(saved.split_subagents)
    is_subagent = task_kind.startswith(CAPABILITY_AGENT_TASK_PREFIX)
    kind_meta = get_ai_task_kind(task_kind)
    if kind_meta is not None and kind_meta.follows_caller:
        return

    if override is None:
        if is_subagent:
            edges.pop(task_kind, None)
            if task_kind in split_subagents:
                split_subagents.remove(task_kind)
        elif kind_meta is not None:
            edges[task_kind] = high_anchor if kind_meta.default_level == "high" else low_anchor
        else:
            edges.pop(task_kind, None)
    else:
        if is_subagent and task_kind not in split_subagents:
            split_subagents.append(task_kind)
        if override.primary == "@high":
            edges[task_kind] = high_anchor
        elif override.primary == "@low":
            edges[task_kind] = low_anchor
        else:
            # 显式配置只匹配非锚点节点：锚点随档位同步变更，连上去会把冻结语义变成跟随
            anchor_ids = {high_anchor, low_anchor}
            matched_id = next(
                (
                    n.id
                    for n in nodes
                    if n.id not in anchor_ids and n.primary == override.primary and n.secondary == override.secondary
                ),
                "",
            )
            if not matched_id:
                seq = 1
                while f"hub_{seq}" in node_ids:
                    seq += 1
                matched_id = f"hub_{seq}"
                nodes.append(
                    TaskGraphHubNode(
                        id=matched_id,
                        primary=override.primary,
                        secondary=override.secondary,
                        role="custom",
                        pos=None,
                    )
                )
            edges[task_kind] = matched_id

    save_task_model_graph(
        TaskModelGraph(
            nodes=tuple(nodes),
            edges=edges,
            split_subagents=tuple(split_subagents),
            task_positions=dict(saved.task_positions),
        )
    )


@app.put("/api/ai_task_models/{task_kind}", summary="设置单个任务的模型覆盖", tags=PROVIDER_CONFIG)
async def set_ai_task_model(
    task_kind: str,
    body: TaskOverrideBody,
    _: SessionRecord = Depends(require_admin),
) -> TaskModelsResp:
    if not _is_known_task_kind(task_kind):
        return _fail(f"未知的任务类型: {task_kind}")
    if _is_follows_caller_kind(task_kind):
        return _fail(f"任务 '{task_kind}' 固定跟随调用方档位，不支持单独覆盖")
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

    ov = TaskModelOverride(primary, secondary)
    overrides = get_task_model_overrides()
    overrides[task_kind] = ov
    if not save_task_model_overrides(overrides):
        return _fail("保存失败")
    _sync_saved_graph_for_single_task(task_kind, ov)
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
    _sync_saved_graph_for_single_task(task_kind, None)
    return _ok()


@app.delete("/api/ai_task_models", summary="清除全部任务的模型覆盖", tags=PROVIDER_CONFIG)
async def clear_all_ai_task_models(_: SessionRecord = Depends(require_admin)) -> TaskModelsResp:
    if not save_task_model_overrides({}) or not save_task_model_graph(None):
        return _fail("保存失败")
    return _ok()
