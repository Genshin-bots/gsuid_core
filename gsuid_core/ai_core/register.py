from typing import Any, Dict, List, Union, TypeVar, Callable, Optional, overload

from gsuid_core.logger import logger

from .utils import function_to_schema
from .models import ToolSchema, KnowledgePoint

F = TypeVar("F", bound=Callable)

# --- 全局注册表和客户端 ---
_TOOL_REGISTRY: Dict[str, ToolSchema] = {}
_ENTITIES: List[KnowledgePoint] = []
_ALIASES: Dict[str, List[str]] = {}


def ai_alias(name: str, alias: Union[str, List[str]]):
    """为特定实体注册别名"""
    if isinstance(alias, str):
        alias = [alias]

    for a in alias:
        if a not in _ALIASES:
            _ALIASES[a] = []

        if name not in _ALIASES[a]:
            _ALIASES[a].append(name)

    logger.debug(f"🧠 [AI][Registry] Registered aliases for {name}: {alias}")


def get_registered_tools():
    logger.info(f"🧠 [AI][Registry] Registered tools: {_TOOL_REGISTRY.keys()}")
    # logger.debug(f"🧠 [AI][Registry] Registered tools schema: {_TOOL_REGISTRY}")
    return _TOOL_REGISTRY


@overload
def ai_tools(func: F, /) -> F: ...


@overload
def ai_tools(
    *,
    check_func: Optional[Callable] = None,
    **check_kwargs: Any,
) -> Callable[[F], F]: ...


def ai_tools(
    func: Optional[F] = None,
    *,
    check_func: Optional[Callable] = None,
    **check_kwargs: Any,
):
    """
    装饰器：将函数注册为大模型工具。
    在启动时，自动生成 msgspec Schema 并存入注册表。

    可选参数:
        check_func: 一个异步函数，用于在执行工具前进行验证。
                    函数签名应为 async def check_func(bot, ev, **kwargs) -> bool
        **check_kwargs: 传递给 check_func 的额外参数
    """

    def decorator(func: F) -> F:
        func_name = func.__name__

        # 1. 生成 Schema
        schema = function_to_schema(func)

        # 2. 存入全局注册表
        _TOOLS: ToolSchema = {
            "name": func_name,
            "desc": schema["function"]["description"],
            "params": schema["function"]["parameters"],
            "schema": schema,
            "func": func,
            "check_func": check_func,
            "check_kwargs": check_kwargs,
        }
        _TOOL_REGISTRY[func_name] = _TOOLS

        logger.trace(f"🧠 [AI][Registry] Tool registered: {func_name}")
        return func

    # 支持直接使用 @ai_tools 或带参数 @ai_tools(check_func=xxx)
    if callable(check_func) and not check_kwargs:
        # 如果第一个参数是可调用函数且没有其他参数，则作为普通装饰器使用
        actual_func: F = check_func  # type: ignore
        check_func = None
        return decorator(actual_func)

    if func is not None:
        return decorator(func)

    return decorator


def ai_entity(entity: KnowledgePoint):
    """
    将实体注册为大模型实体。
    在启动时，自动将实体存入全局注册表。
    """
    _ENTITIES.append(entity)
    logger.trace(f"🧠 [AI][Registry] Entity registered: {entity['title']}")
