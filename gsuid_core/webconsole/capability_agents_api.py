"""
Capability Agents APIs（AgentNode 统一版）。

提供给前端管理"能力代理节点（AgentNode）"的 RESTful 接口。节点分四种来源：

- ``builtin`` / ``plugin``：仅可读，**禁止改 / 删**。
- ``user``：管理员在 webconsole 上手工新建 / 编辑，允许 PATCH / DELETE，落盘在
  ``data/ai_core/capability_agents/<node_id>.json``（v1 旧画像文件自动迁移）。
- ``persona``：persona 目录投影节点，经 persona 管理页维护，本 API 只读列出。

破坏性变更（v2）：字段 ``profile_id``→``node_id``、``system_prompt``→``prompt``；
``max_iterations`` / ``max_tokens`` 移除（预算统一走 AI 配置的任务档）；新增
``tool_packs`` / ``prompt_style`` / ``boundary_override``。
"""

import re
from typing import Any, Dict, List, Optional

from fastapi import Query, Depends
from pydantic import Field, BaseModel

from gsuid_core.logger import logger
from gsuid_core.ai_core.agent_node import (
    TASK_BASICS_PACK,
    AgentNode,
    get_node,
    list_nodes,
    register_agent_node,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.capability_agents import (
    AgentNodeDTO,
    save_user_profile,
    get_profile_as_dto,
    get_profile_source,
    delete_user_profile,
)

from ._api_tags import CAPABILITY_AGENTS

# node_id 命名规范：英数下划线，1~64 字符，字母开头。
_NODE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")


# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────


class CreateNodeRequest(BaseModel):
    node_id: str = Field(..., description="英数下划线，1~64 字符，字母开头")
    display_name: str = Field(..., description="给人格 / 用户看的名字，如「操盘助手」")
    when_to_use: str = Field("", description="何时该派给它（一句话）")
    prompt: str = Field(..., description="身份提示词正文（task-mode 时框架自动叠加交付边界）")
    match_keywords: List[str] = Field(default_factory=list)
    tool_packs: List[str] = Field(
        default_factory=lambda: [TASK_BASICS_PACK],
        description="工具能力族（task_basics / dynamic / capability_domain 族名）",
    )
    tool_names: List[str] = Field(default_factory=list, description="显式工具白名单（按名）")
    tool_query: str = Field("", description="可选：再做一次向量检索补充工具的查询词")
    boundary_override: str = Field("", description="可选：覆写 task-mode 交付边界（空=框架默认）")
    base: Optional[str] = Field(None, description="（可选）以哪个已存在节点为模板复制字段，再用本请求覆盖")


class PatchNodeRequest(BaseModel):
    """对一个用户节点做局部更新。所有字段都可选，未传的保持不变。"""

    display_name: Optional[str] = None
    when_to_use: Optional[str] = None
    prompt: Optional[str] = None
    match_keywords: Optional[List[str]] = None
    tool_packs: Optional[List[str]] = None
    tool_names: Optional[List[str]] = None
    tool_query: Optional[str] = None
    boundary_override: Optional[str] = None


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────


@app.get("/api/ai/capability-agents/list", summary="列表", tags=CAPABILITY_AGENTS)
async def list_capability_agents(
    _: Dict[str, Any] = Depends(require_auth),
    source: Optional[str] = Query(None, description="按来源筛选：builtin / plugin / user / persona"),
) -> Dict[str, Any]:
    """列出所有节点（含来源标记）。``source=persona`` 时列 persona 投影节点。"""
    from gsuid_core.ai_core.capability_agents.persistence import _node_to_dto

    include_persona = source is None or source == "persona"
    items: List[AgentNodeDTO] = [_node_to_dto(n) for n in list_nodes(include_persona=include_persona)]
    if source:
        items = [x for x in items if "source" in x and x["source"] == source]
    return {"status": 0, "msg": "ok", "data": {"items": items, "count": len(items)}}


@app.get("/api/ai/capability-agents/{node_id}", summary="详情", tags=CAPABILITY_AGENTS)
async def get_capability_agent_detail(
    node_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """获取单个节点详情。"""
    dto = get_profile_as_dto(node_id)
    if dto is None:
        return {"status": 1, "msg": f"节点 {node_id} 不存在", "data": None}
    return {"status": 0, "msg": "ok", "data": dto}


@app.post("/api/ai/capability-agents", summary="新建", tags=CAPABILITY_AGENTS)
async def create_capability_agent(
    body: CreateNodeRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """新建一个用户自定义节点（写入磁盘 + 统一注册表）。

    ``base`` 字段允许"以某节点为模板复制字段再覆盖"：请求中某字段为空时
    fallback 到 ``base`` 节点的同名字段；显式传值以请求为准。
    """
    if not _NODE_ID_RE.match(body.node_id):
        return {
            "status": 1,
            "msg": "node_id 必须以字母开头，仅含英数下划线，长度 1~64",
            "data": None,
        }
    if get_node(body.node_id) is not None:
        return {"status": 1, "msg": f"节点 {body.node_id} 已存在，请改用 PATCH 编辑", "data": None}

    base_node = get_node(body.base) if body.base else None
    node = AgentNode(
        node_id=body.node_id,
        display_name=body.display_name or (base_node.display_name if base_node is not None else body.node_id),
        prompt=body.prompt or (base_node.prompt if base_node is not None else ""),
        prompt_style="plain",
        when_to_use=body.when_to_use or (base_node.when_to_use if base_node is not None else ""),
        match_keywords=list(
            body.match_keywords if body.match_keywords else (base_node.match_keywords if base_node is not None else [])
        ),
        tool_packs=list(
            body.tool_packs
            if body.tool_packs
            else (base_node.tool_packs if base_node is not None else [TASK_BASICS_PACK])
        ),
        tool_names=list(
            body.tool_names if body.tool_names else (base_node.tool_names if base_node is not None else [])
        ),
        tool_query=body.tool_query or (base_node.tool_query if base_node is not None else ""),
        boundary_override=body.boundary_override or (base_node.boundary_override if base_node is not None else ""),
        source="user",
    )

    register_agent_node(node)
    save_user_profile(node)
    logger.info(
        f"🤖 [CapabilityAgent] webconsole 新建用户节点: {node.node_id}"
        f"（{'基于 ' + body.base if body.base else '无模板'}）"
    )
    return {"status": 0, "msg": "ok", "data": get_profile_as_dto(node.node_id)}


@app.patch("/api/ai/capability-agents/{node_id}", summary="编辑", tags=CAPABILITY_AGENTS)
async def patch_capability_agent(
    node_id: str,
    body: PatchNodeRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """编辑一个**用户自建**节点。builtin / plugin / persona 节点拒绝修改。"""
    source = get_profile_source(node_id)
    if source == "missing":
        return {"status": 1, "msg": f"节点 {node_id} 不存在", "data": None}
    if source != "user":
        return {
            "status": 1,
            "msg": f"节点 {node_id} 是 {source} 节点，框架不允许通过网页修改；"
            "请用 POST /api/ai/capability-agents 复制一份用户版本再改",
            "data": None,
        }

    existing = get_node(node_id)
    if existing is None:  # 双保险：source 标记和注册表应同步
        return {"status": 1, "msg": f"节点 {node_id} 已失踪", "data": None}

    patched = AgentNode(
        node_id=existing.node_id,
        display_name=body.display_name if body.display_name is not None else existing.display_name,
        prompt=body.prompt if body.prompt is not None else existing.prompt,
        prompt_style=existing.prompt_style,
        when_to_use=body.when_to_use if body.when_to_use is not None else existing.when_to_use,
        match_keywords=list(body.match_keywords) if body.match_keywords is not None else list(existing.match_keywords),
        tool_packs=list(body.tool_packs) if body.tool_packs is not None else list(existing.tool_packs),
        tool_names=list(body.tool_names) if body.tool_names is not None else list(existing.tool_names),
        tool_query=body.tool_query if body.tool_query is not None else existing.tool_query,
        boundary_override=(
            body.boundary_override if body.boundary_override is not None else existing.boundary_override
        ),
        source="user",
    )
    register_agent_node(patched)
    save_user_profile(patched)
    return {"status": 0, "msg": "ok", "data": get_profile_as_dto(node_id)}


@app.delete("/api/ai/capability-agents/{node_id}", summary="删除", tags=CAPABILITY_AGENTS)
async def delete_capability_agent(
    node_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """删除一个**用户自建**节点（同时清磁盘文件 + 统一注册表）。"""
    source = get_profile_source(node_id)
    if source == "missing":
        return {"status": 1, "msg": f"节点 {node_id} 不存在", "data": None}
    if source != "user":
        return {
            "status": 1,
            "msg": f"节点 {node_id} 是 {source} 节点，框架不允许通过网页删除",
            "data": None,
        }
    ok = delete_user_profile(node_id)
    if not ok:
        return {"status": 1, "msg": "删除失败（非用户节点）", "data": None}
    return {"status": 0, "msg": "ok", "data": {"node_id": node_id}}


@app.get("/api/ai/capability-agents/_tools/available", summary="可挂载工具枚举", tags=CAPABILITY_AGENTS)
async def list_available_tools_for_profiles(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """枚举所有可挂载到节点的工具名，前端做 tool_names 多选框时用。"""
    from gsuid_core.ai_core.register import get_registered_tools

    by_cat = get_registered_tools()
    items: List[Dict[str, Any]] = []
    for cat, tools in by_cat.items():
        for name, base in tools.items():
            items.append(
                {
                    "name": name,
                    "description": base.description,
                    "category": cat,
                    "plugin": base.plugin,
                }
            )
    items.sort(key=lambda x: (x["plugin"], x["category"], x["name"]))
    return {"status": 0, "msg": "ok", "data": {"items": items, "count": len(items)}}
