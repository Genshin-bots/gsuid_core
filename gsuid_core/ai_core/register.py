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
    """
    为特定实体注册别名, 用于大模型调用前进行专有名词归一化
    调用时, 例如:

    from gsuid_core.ai_core.register import ai_alias

    ai_alias("丝柯克", ['skk', '斯柯克'])

    """
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
                    bot: Bot,
                    ev: Event,
                    bot和ev为事件触发时的bot和事件对象，可以不作为传参
                    如果作为传参, 可以不在kwargs中传递, 会自动执行依赖注入
        **check_kwargs: 传递给 check_func 的额外参数

    装饰的函数:
        需要包含docstring, 用于生成工具的描述
        所有的传参需要进行详细的类型提示, 包括所有的参数, 以便大模型调用

        例如:

        from typing import Tuple, Optional, Annotated
        from msgspec import Meta

        @ai_tools(check_func=check_pm)
        async def deduct_user_points(
            target_user_id: Annotated[str, Meta(description="目标用户的唯一标识 ID")],
            point_num: Annotated[int, Meta(description="要扣除的积分数量,必须大于 0")],
            ev: Event,
        ) -> str:
        '''
        扣除目标用户的积分
        '''
            pass
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

    if func is not None:
        return decorator(func)

    return decorator


def ai_entity(entity: KnowledgePoint):
    """
    将实体注册为大模型实体。
    在启动时，自动将实体存入全局注册表。

        entity: 一个包含实体信息的字典

            id: str
            plugin: str
            type: str
            category: str
            title: str
            content: str
            tags: List[str]
            _hash: str

    例如:

    from gsuid_core.ai_core.models import KnowledgePoint
    from gsuid_core.ai_core.register import ai_entity

    ai_entity(KnowledgePoint(
        id="123",
        plugin="Genshin",
        type="角色介绍",
        category="角色",
        title="角色介绍和详情 - 丝柯克",
        content="角色的详细信息, # 丝柯克 ## 武器类型xx ## 技能 ## 命之座",
        tags=["角色", "丝柯克", "skk", "Genshin"],
        _hash="123456",
    ))
    """
    _ENTITIES.append(entity)
    logger.trace(f"🧠 [AI][Registry] Entity registered: {entity['title']}")
