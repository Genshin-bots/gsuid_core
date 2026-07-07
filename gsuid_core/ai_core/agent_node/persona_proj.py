"""Persona → AgentNode 只读投影。

persona 磁盘布局（``persona/<name>/{persona.md, config.json, 资源}``）与
``PersonaConfigManager`` 写路径**保持不变**（部署者零迁移）；本模块把每个
persona 目录投影为一个 ``source="persona"`` 的 AgentNode 供统一注册表消费。
按 persona.md / config.json 的 mtime 缓存，文件变化即重投影（与 ai_router
的 persona 热重载同一信号源）。
"""

from typing import Dict, Tuple, Optional
from pathlib import Path

from gsuid_core.logger import logger

from .models import AgentNode

# persona_name -> (md_mtime, cfg_mtime, node)
_PROJECTION_CACHE: Dict[str, Tuple[float, float, AgentNode]] = {}


def _persona_root() -> Path:
    from gsuid_core.ai_core.resource import PERSONA_PATH

    return PERSONA_PATH


def _mtimes(persona_dir: Path) -> Tuple[float, float]:
    md = persona_dir / "persona.md"
    cfg = persona_dir / "config.json"
    return (
        md.stat().st_mtime if md.exists() else 0.0,
        cfg.stat().st_mtime if cfg.exists() else 0.0,
    )


def _project(persona_name: str) -> Optional[AgentNode]:
    """把一个 persona 目录投影为 AgentNode；persona.md 缺失时返回 None。"""
    persona_dir = _persona_root() / persona_name
    md_path = persona_dir / "persona.md"
    if not md_path.exists():
        return None

    from gsuid_core.ai_core.persona.config import persona_config_manager

    cfg = persona_config_manager.get_config(persona_name)
    prompt = md_path.read_text(encoding="utf-8")
    return AgentNode(
        node_id=persona_name,
        display_name=persona_name,
        prompt=prompt,
        prompt_style="roleplay",
        tool_packs=list(cfg.get_config("tool_packs").data),
        tool_names=list(cfg.get_config("tool_names").data),
        ai_mode=list(cfg.get_config("ai_mode").data),
        scope=str(cfg.get_config("scope").data),
        target_groups=list(cfg.get_config("target_groups").data),
        inspect_interval=int(cfg.get_config("inspect_interval").data),
        keywords=list(cfg.get_config("keywords").data),
        source="persona",
    )


def get_persona_node(persona_name: str) -> Optional[AgentNode]:
    """取某 persona 的投影节点（mtime 变化自动重投影）。"""
    persona_dir = _persona_root() / persona_name
    if not persona_dir.is_dir():
        _PROJECTION_CACHE.pop(persona_name, None)
        return None
    md_mtime, cfg_mtime = _mtimes(persona_dir)
    cached = _PROJECTION_CACHE.get(persona_name)
    if cached is not None and cached[0] == md_mtime and cached[1] == cfg_mtime:
        return cached[2]
    node = _project(persona_name)
    if node is None:
        _PROJECTION_CACHE.pop(persona_name, None)
        return None
    _PROJECTION_CACHE[persona_name] = (md_mtime, cfg_mtime, node)
    logger.debug(f"🧩 [AgentNode] persona 投影已刷新: {persona_name}")
    return node


def list_persona_nodes() -> Dict[str, AgentNode]:
    """扫描 persona 目录，返回全部投影节点（key=persona_name）。"""
    root = _persona_root()
    if not root.exists():
        return {}
    nodes: Dict[str, AgentNode] = {}
    for item in root.iterdir():
        if not item.is_dir():
            continue
        node = get_persona_node(item.name)
        if node is not None:
            nodes[item.name] = node
    return nodes
