from typing import Dict, List, Callable

from gsuid_core.logger import logger

from .utils import function_to_schema
from .models import ToolSchema, EntitySchema

# --- 全局注册表和客户端 ---
_TOOL_REGISTRY: Dict[str, ToolSchema] = {}
_ENTITIES: List[EntitySchema] = []


def get_registered_tools():
    return _TOOL_REGISTRY


def ai_tools(func: Callable) -> Callable:
    """
    装饰器：将函数注册为大模型工具。
    在启动时，自动生成 msgspec Schema 并存入注册表。
    """
    func_name = func.__name__

    # 1. 生成 Schema
    schema = function_to_schema(func)

    # 2. 存入全局注册表
    _TOOL_REGISTRY[func_name] = {
        "name": func_name,
        "desc": schema["description"],
        "params": schema["parameters"],
        "schema": schema,
        "func": func,
    }

    logger.trace(f"[AI Tools][Registry] Tool registered: {func_name}")
    return func


def ai_entity(name: str, domain: str, entity_type: str, aliases: List[str] = []):
    """
    将实体注册为大模型实体。
    在启动时，自动将实体存入全局注册表。
    """
    _ENTITIES.append(
        {
            "name": name,
            "aliases": aliases,
            "domain": domain,
            "type": entity_type,
        }
    )
    logger.trace(f"[AI Entities][Registry] Entity registered: {name}")


def startup_reverse_map():
    """
    构建反向映射，将实体名称和别名映射为实体信息。
    """
    reverse_map = {}
    for entity in _ENTITIES:
        reverse_map[entity["name"]] = entity
        for alias in entity["aliases"]:
            reverse_map[alias] = entity
    return reverse_map
