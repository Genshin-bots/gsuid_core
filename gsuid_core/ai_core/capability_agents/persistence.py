"""用户自定义节点（webconsole 手工新建 / 编辑）的本地持久化。

框架内置节点由 ``profiles.py`` 在进程启动时重建；插件节点由插件自己的启动钩子
负责——两者都**不**持久化。本模块只管"用户在 webconsole 上手工新建 / 编辑的节点"：

- 落盘路径：``data/ai_core/capability_agents/<node_id>.json``（沿用旧目录，
  旧版画像 JSON（v1：``profile_id`` / ``system_prompt`` / ``max_*``）读取时
  自动迁移为 AgentNode 形状，下次保存即落新格式——部署者无需手动操作。
- 启动顺序：``init_planning()`` 调完 ``register_builtin_profiles()`` 之后立即调
  ``load_user_profiles()``，把磁盘上的用户节点挂回统一注册表。

只有 ``source="user"`` 的节点允许通过 webconsole 修改 / 删除。
"""

import json
from typing import Any, Dict, List, Literal, Optional, TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.agent_node import (
    TASK_BASICS_PACK,
    AgentNode,
    get_node,
    register_agent_node,
    unregister_agent_node,
)

# 与 `ai_core/resource.py` 解耦——直接落到 data/ai_core/capability_agents/
_PERSIST_DIR: Path = Path(__file__).resolve().parents[3] / "data" / "ai_core" / "capability_agents"
# MCP 节点用户覆盖层独立子目录：自动生成的节点本体不落盘，只存用户差量。
_MCP_OVERLAY_DIR: Path = _PERSIST_DIR / "mcp"

ProfileSource = Literal["builtin", "plugin", "user", "persona", "mcp"]
ProfileSourceWithMissing = Literal["builtin", "plugin", "user", "persona", "mcp", "missing"]


class MCPNodeOverlay(TypedDict, total=False):
    """MCP 节点的用户覆盖层：只存与自动生成值的差量字段。"""

    display_name: str
    when_to_use: str
    match_keywords: List[str]
    prompt: str
    boundary_override: str
    master_only: bool
    tool_packs: List[str]


class AgentNodeDTO(TypedDict, total=False):
    """webconsole / 持久化层共享的节点传输 / 落盘字典（v2 形状）。"""

    node_id: str
    display_name: str
    when_to_use: str
    prompt: str
    prompt_style: str
    match_keywords: List[str]
    tool_packs: List[str]
    tool_names: List[str]
    tool_query: str
    boundary_override: str
    master_only: bool
    source: ProfileSource
    plugin: str
    version: int
    in_roster: bool
    # 仅 source="mcp"：when_to_use 文案来源（user/market/generated/auto）
    desc_origin: str


def _node_to_dto(node: AgentNode) -> AgentNodeDTO:
    """把内存里的 AgentNode 序列化为 DTO（落盘 + 前端 JSON 都用这个形状）。"""
    dto = AgentNodeDTO(
        node_id=node.node_id,
        display_name=node.display_name,
        when_to_use=node.when_to_use,
        prompt=node.prompt,
        prompt_style=node.prompt_style,
        match_keywords=list(node.match_keywords),
        tool_packs=list(node.tool_packs),
        tool_names=list(node.tool_names),
        tool_query=node.tool_query,
        boundary_override=node.boundary_override,
        master_only=node.master_only,
        source=node.source,
        plugin=node.plugin or "",
        version=node.version,
        in_roster=node.in_roster,
    )
    if node.source == "mcp":
        from gsuid_core.ai_core.mcp.node_profile import mcp_desc_origin

        dto["desc_origin"] = mcp_desc_origin(node.node_id)
    return dto


def _dto_to_node(dto: Dict[str, Any]) -> Optional[AgentNode]:
    """把落盘字典反序列化为 AgentNode；同时兼容 v1 旧画像格式（自动迁移字段名）。

    v1 → v2 映射：profile_id→node_id、system_prompt→prompt；max_iterations /
    max_tokens 丢弃（预算已抹平为全局配置）；tool_packs 补默认 task_basics。
    """
    node_id = dto["node_id"] if "node_id" in dto else (dto["profile_id"] if "profile_id" in dto else "")
    if not node_id:
        return None
    prompt = dto["prompt"] if "prompt" in dto else (dto["system_prompt"] if "system_prompt" in dto else "")
    style = dto["prompt_style"] if "prompt_style" in dto and dto["prompt_style"] == "roleplay" else "plain"
    return AgentNode(
        node_id=str(node_id),
        display_name=str(dto["display_name"]) if "display_name" in dto else str(node_id),
        prompt=str(prompt),
        prompt_style=style,
        when_to_use=str(dto["when_to_use"]) if "when_to_use" in dto else "",
        match_keywords=[str(x) for x in dto["match_keywords"]] if "match_keywords" in dto else [],
        tool_packs=(
            [str(x) for x in dto["tool_packs"]] if "tool_packs" in dto and dto["tool_packs"] else [TASK_BASICS_PACK]
        ),
        tool_names=[str(x) for x in dto["tool_names"]] if "tool_names" in dto else [],
        tool_query=str(dto["tool_query"]) if "tool_query" in dto and dto["tool_query"] else "",
        boundary_override=str(dto["boundary_override"]) if "boundary_override" in dto else "",
        master_only=bool(dto["master_only"]) if "master_only" in dto else False,
        source="user",
        plugin=str(dto["plugin"]) if "plugin" in dto and dto["plugin"] else "",
        in_roster=bool(dto["in_roster"]) if "in_roster" in dto else True,
    )


def _profile_path(node_id: str) -> Path:
    return _PERSIST_DIR / f"{node_id}.json"


def _mcp_overlay_path(node_id: str) -> Path:
    return _MCP_OVERLAY_DIR / f"{node_id}.json"


def load_mcp_overlay(node_id: str) -> MCPNodeOverlay:
    """读取 MCP 节点用户覆盖层；文件缺失 / 结构不合法返回空覆盖。"""
    path = _mcp_overlay_path(node_id)
    if not path.exists():
        return MCPNodeOverlay()
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(t("log.ai.cap_load_user_node", p0=path.name, e=e))
        return MCPNodeOverlay()
    if not isinstance(raw, dict):
        return MCPNodeOverlay()
    overlay = MCPNodeOverlay()
    for key in ("display_name", "when_to_use", "prompt", "boundary_override"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            overlay[key] = value
    for key in ("match_keywords", "tool_packs"):
        value = raw.get(key)
        if isinstance(value, list):
            overlay[key] = [str(x) for x in value if isinstance(x, str) and x.strip()]
    master_only = raw.get("master_only")
    if isinstance(master_only, bool):
        overlay["master_only"] = master_only
    return overlay


def save_mcp_overlay(node_id: str, overlay: MCPNodeOverlay) -> Path:
    """落盘 MCP 节点覆盖层；空覆盖直接删文件（等价"恢复自动生成"）。"""
    path = _mcp_overlay_path(node_id)
    if not overlay:
        if path.exists():
            path.unlink()
        logger.info(t("log.ai.cap_mcp_overlay_cleared", node_id=node_id))
        return path
    _MCP_OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(dict(overlay), f, ensure_ascii=False, indent=2)
    import os

    os.replace(tmp_path, path)
    logger.info(t("log.ai.cap_mcp_overlay_saved", node_id=node_id))
    return path


def delete_mcp_overlay(node_id: str) -> bool:
    """删除 MCP 节点覆盖层文件；返回是否真的删了一项。"""
    path = _mcp_overlay_path(node_id)
    if not path.exists():
        return False
    path.unlink()
    logger.info(t("log.ai.cap_mcp_overlay_cleared", node_id=node_id))
    return True


def is_user_profile(node_id: str) -> bool:
    """判定一个节点是否由用户在 webconsole 上新建（据 node.source）。"""
    node = get_node(node_id)
    return node is not None and node.source == "user"


def get_profile_source(node_id: str) -> ProfileSourceWithMissing:
    """判定节点来源（前端展示与权限）。缺失时返回 "missing"。"""
    node = get_node(node_id)
    if node is None:
        return "missing"
    return node.source


def save_user_profile(node: AgentNode) -> Path:
    """把用户自建 / 编辑后的节点落盘（v2 格式，source 强制 user）。

    本函数不调 ``register_agent_node``——调用方自己决定 register 时机。
    """
    _PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    node.source = "user"
    path = _profile_path(node.node_id)
    payload = _node_to_dto(node)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # 原子替换，避免半文件
    import os

    os.replace(tmp_path, path)
    logger.info(t("log.ai.cap_agent_node_user_persisted", p0=node.node_id, p1=path.name))
    return path


def delete_user_profile(node_id: str) -> bool:
    """从磁盘和内存里删除某用户自建节点；仅 source="user" 允许删除。"""
    if not is_user_profile(node_id):
        return False
    path = _profile_path(node_id)
    if path.exists():
        path.unlink()
    unregister_agent_node(node_id)
    logger.info(t("log.ai.cap_user_node_deleted_id", node_id=node_id))
    return True


def load_user_profiles() -> int:
    """启动时把磁盘上的用户节点挂回统一注册表。返回挂回的节点数量。

    v1 旧画像文件自动迁移读入（append-only 升级，无需部署者操作）。
    """
    if not _PERSIST_DIR.exists():
        return 0
    count = 0
    for path in _PERSIST_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                dto = json.load(f)
            if not isinstance(dto, dict):
                logger.warning(t("log.ai.cap_skip_invalid_node_file", p0=path.name))
                continue
            node = _dto_to_node(dto)
            if node is None:
                logger.warning(t("log.ai.cap_skip_node_file_missing", p0=path.name))
                continue
            register_agent_node(node)
            count += 1
        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning(t("log.ai.cap_load_user_node", p0=path.name, e=e))
    if count:
        logger.info(t("log.ai.cap_user_nodes_startup", count=count))
    return count


def export_all_profiles_as_dto() -> List[AgentNodeDTO]:
    """导出统一注册表所有节点（不含 persona 投影）的 DTO（webconsole list 端点用）。"""
    from gsuid_core.ai_core.agent_node import list_nodes

    return [_node_to_dto(n) for n in list_nodes()]


def get_profile_as_dto(node_id: str) -> Optional[AgentNodeDTO]:
    """导出单个节点的 DTO（webconsole detail 端点用）。"""
    node = get_node(node_id)
    if node is None:
        return None
    return _node_to_dto(node)
