"""Agent 套件 / Hook 总线 / 关系温度的只读治理接口。

槽位健康是本次套件化最重要的可观测面：**第一方套件 fail-open 会导致「记忆全无却
无告警」**——控制台必须能一眼看出某个槽是不是空的、hook 有没有挂上。

关系温度只读页解决另一个排障盲区：分数为什么变、上次因为什么变（``last_reason``）。
"""

from typing import Any, Dict, List, Optional

from fastapi import Query, Depends

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import AGENT_KITS, RELATIONSHIP


@app.get("/api/agent_kits/slots", summary="套件槽位健康", tags=AGENT_KITS)
async def agentKitSlots(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    """列出全部槽位、默认占用者、当前占用者与密封标记。

    ``occupants`` 为空 = 该槽 ``off``（对应能力整块关闭）。密封槽被关会拆安全面，
    前端应对 ``sealed=true 且 occupants=[]`` 打红字。
    """
    from gsuid_core.ai_core.kits import KIT_SLOTS, get_kit, occupants_of, resolve_slot_config

    slots: List[Dict[str, Any]] = []
    for slot in KIT_SLOTS:
        occupants = list(occupants_of(slot.name))
        configured = list(resolve_slot_config(slot.name))
        slots.append(
            {
                "name": slot.name,
                "description": slot.description,
                "default_kit_id": slot.default_kit_id,
                "exclusive": slot.exclusive,
                "sealed": slot.sealed,
                "configured": configured,
                "occupants": occupants,
                "healthy": bool(occupants) or not configured,
                "candidates": [
                    {"kit_id": k.kit_id, "display_name": k.display_name, "owns_tools": list(k.owns_tools)}
                    for k in (get_kit(cid) for cid in configured)
                    if k is not None
                ],
            }
        )
    return {"status_code": 200, "data": {"slots": slots}}


@app.get("/api/agent_kits/hooks", summary="Hook 点位与挂载情况", tags=AGENT_KITS)
async def agentKitHooks(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    """列出 31 个点位的契约（内核锚点 / 能力票 / 超时预算）与当前挂载者。

    未接线的点位 ``owners`` 为空——这既可能是「本期没接」，也可能是槽位被关了。
    """
    from gsuid_core.ai_core.hooks import (
        HOOK_POINT_SPECS,
        AgentHookPoint,
        hook_count,
        list_hooks,
        hooks_enabled,
    )

    owners = list_hooks()
    points: List[Dict[str, Any]] = []
    for point in AgentHookPoint:
        spec = HOOK_POINT_SPECS[point]
        points.append(
            {
                "id": point.value,
                "name": point.name,
                "anchor": spec.anchor,
                "capabilities": sorted(c.value for c in spec.capabilities),
                "default_timeout_ms": spec.default_timeout_ms,
                "wired": spec.wired,
                "owners": owners[point.name] if point.name in owners else [],
            }
        )
    return {
        "status_code": 200,
        "data": {
            "enabled": hooks_enabled(),
            "total_hooks": hook_count(),
            "points": points,
        },
    }


@app.get("/api/relationship/view", summary="某人的关系温度", tags=RELATIONSHIP)
async def relationshipView(
    user_id: str = Query(..., description="用户 ID"),
    bot_id: str = Query("", description="Bot ID，留空取任一条"),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """只读：当前 zone / 分数 / 上次变更原因与时间。

    分数在 prompt 里是内部量（不给模型看），但排障必须能看到——「为什么突然变冷」
    的答案就在 ``last_reason``（如 ``neg.insult`` / ``none.budget`` / ``decay.idle``）。
    """
    from gsuid_core.ai_core.relationship import view_from_score, zone_level_name
    from gsuid_core.ai_core.database.models import UserFavorability

    record: Optional[UserFavorability] = None
    if bot_id:
        record = await UserFavorability.get_user_favorability(user_id=user_id, bot_id=bot_id)
    else:
        scores = await UserFavorability.get_scores_for([user_id], "")
        if user_id in scores:
            record = await UserFavorability.get_user_favorability(user_id=user_id, bot_id="")
    if record is None:
        view = view_from_score(None, False)
        return {
            "status_code": 200,
            "data": {
                "user_id": user_id,
                "scored": False,
                "zone": view.zone.value,
                "zone_label": zone_level_name(view.zone),
                "line": view.line,
            },
        }
    view = view_from_score(record.favorability, False)
    return {
        "status_code": 200,
        "data": {
            "user_id": user_id,
            "bot_id": record.bot_id,
            "scored": True,
            "score": record.favorability,
            "zone": view.zone.value,
            "zone_label": zone_level_name(view.zone),
            "line": view.line,
            "last_delta": record.last_delta,
            "last_reason": record.last_reason,
            "last_eval_at": record.last_eval_at,
            "daily_gain": record.daily_gain,
            "daily_loss": record.daily_loss,
            "daily_ymd": record.daily_ymd,
            "last_positive_interact_at": record.last_positive_interact_at,
            "interaction_count": record.interaction_count,
        },
    }


@app.get("/api/cognition/nodes", summary="认知节点检索", tags=AGENT_KITS)
async def cognitionNodes(
    keyword: str = Query("", description="摘要关键词"),
    scope_key: str = Query("", description="可见范围，留空只看公共节点"),
    owner_user_id: str = Query("", description="属主，留空只看无属主的公共节点"),
    limit: int = Query(20, ge=1, le=100),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """只读：按关键词看认知节点（跨 kind 的蒸馏结论索引）。

    节点只存身份 / 摘要 / 句柄——正文仍在原库，靠 ``handle`` 去 ``read_handle`` 取。

    ``owner_user_id`` 走与 ``search_cognition`` **同一条**行级 ACL：留空时
    ``tool_output`` / ``artifact`` 这类必须带属主的节点不可见，运维面板不能成为
    绕过属主过滤的后门。
    """
    from gsuid_core.ai_core.cognition.nodes import (
        AICogNode,
        AICogAttachment,
        node_to_dict,
        attachment_to_dict,
    )

    rows = await AICogNode.search(
        keyword,
        scope_keys=[scope_key] if scope_key else [],
        owner_user_id=owner_user_id,
        limit=limit,
    )
    ids = [row.id for row in rows if row.id is not None]
    by_node: Dict[int, List[Any]] = {nid: [] for nid in ids}
    if ids:
        for att in await AICogAttachment.list_for_nodes(ids):
            by_node[att.node_id].append(att)
    nodes = []
    for row in rows:
        item = dict(node_to_dict(row))
        atts = [] if row.id is None else by_node[row.id]
        item["attachments"] = [attachment_to_dict(a) for a in atts]
        nodes.append(item)
    return {"status_code": 200, "data": {"nodes": nodes}}


@app.get("/api/cognition/nodes/{node_id}", summary="认知节点详情（含挂件）", tags=AGENT_KITS)
async def cognitionNodeDetail(
    node_id: int,
    owner_user_id: str = Query("", description="属主，留空只看无属主的公共节点"),
    scope_key: str = Query("", description="可见范围，留空只看公共节点"),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    from gsuid_core.ai_core.cognition.nodes import (
        AICogNode,
        AICogAttachment,
        node_to_dict,
        node_visible_to,
        attachment_to_dict,
    )

    node = await AICogNode.get_by_id(node_id)
    scope_keys = [scope_key] if scope_key else []
    if node is None or not node_visible_to(node, owner_user_id=owner_user_id, scope_keys=scope_keys):
        return {"status_code": 404, "data": None}
    atts = await AICogAttachment.list_for_node(node_id)
    data = dict(node_to_dict(node))
    data["attachments"] = [attachment_to_dict(a) for a in atts]
    return {"status_code": 200, "data": data}


@app.post("/api/cognition/rebuild_mount", summary="重建认知挂载（不碰记忆图）", tags=AGENT_KITS)
async def cognitionRebuildMount(_user: Dict = Depends(require_auth)) -> Dict[str, Any]:
    from gsuid_core.ai_core.cognition.hub import rebuild_cognition_mount

    stats = await rebuild_cognition_mount()
    return {
        "status_code": 200,
        "data": {
            "hubs": stats.hubs,
            "attachments": stats.attachments,
            "linked_env": stats.linked_env,
            "skipped_ambiguous": stats.skipped_ambiguous,
            "skipped_unresolved": stats.skipped_unresolved,
            "last_error": stats.last_error,
        },
    }
