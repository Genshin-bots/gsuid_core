import asyncio
import inspect
from typing import Dict, List, Tuple, Union, TypeVar, Callable, Optional, Awaitable, cast, overload

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from gsuid_core.logger import logger
from gsuid_core.segment import Message
from gsuid_core.ai_core.utils import handle_tool_result
from gsuid_core.ai_core.models import ToolContext

from .models import ToolBase, ImageEntity, KnowledgeBase, KnowledgePoint, ManualKnowledgeBase

F = TypeVar("F", bound=Callable[..., Awaitable[Union[str, Message, bytes]]])

# 定义 check_func 的类型 - 支持同步和异步函数
CheckFunc = Callable[..., Union[Tuple[bool, str], Awaitable[Tuple[bool, str]]]]


def _get_plugin_name_from_module(module_path: str) -> str:
    """根据模块路径获取插件名称

    Args:
        module_path: 函数的模块路径，例如 gsuid_core.ai_core.buildin_tools.web_search

    Returns:
        插件名称，如果在plugins下则返回插件文件夹名，否则返回"core"
    """
    parts = module_path.split(".")
    # 查找 gsuid_core.plugins 的位置
    try:
        plugins_idx = parts.index("plugins")
        if plugins_idx >= 0 and plugins_idx < len(parts) - 1:
            # 返回 plugins 后的第一个文件夹名称
            return parts[plugins_idx + 1]
    except ValueError:
        pass

    # 如果不在plugins下，检查是否在 gsuid_core 目录下（核心模块）
    if "gsuid_core" in parts:
        return "core"

    return "unknown"


# --- 全局注册表和客户端 ---
# 工具注册表: Dict[分类名, Dict[工具名, ToolBase]]
_TOOL_REGISTRY: Dict[str, Dict[str, ToolBase]] = {}
_ENTITIES: List[Union[KnowledgePoint, KnowledgeBase, ImageEntity]] = []  # 来自插件注册的知识和图片
_MANUAL_ENTITIES: List[ManualKnowledgeBase] = []  # 手动添加的知识，不会自动同步
_IMAGE_ENTITIES: List[ImageEntity] = []  # 来自插件注册的图片
# 别名注册表（C2-d 分 scope 防跨域串味）：
# 结构为 {scope: {别名: [正式名候选, ...]}}。
# scope 默认 "global"（插件注册的通用别名）；插件可传业务 scope（如 "Genshin"）
# 隔离同名别名（如"深渊"在不同游戏指代不同对象）。
# 值为 List 以天然支持一对多 / 多候选映射，供动态实体链接按上下文消歧（C2-e）。
_ALIASES: Dict[str, Dict[str, List[str]]] = {}


@overload
def ai_tools(
    func: F,
    /,
) -> F: ...


@overload
def ai_tools(
    func: None = None,
    /,
    *,
    category: str = "default",
    check_func: Optional[CheckFunc] = None,
    **check_kwargs,
) -> Callable[[F], F]: ...


def ai_tools(
    func: Optional[Callable] = None,
    /,
    *,
    category: str = "default",
    check_func: Optional[CheckFunc] = None,
    context_tags: Optional[List[str]] = None,
    capability_domain: Optional[str] = None,
    **check_kwargs,
) -> Callable[[F], F] | F:
    """
    用法: @ai_tools 或 @ai_tools(check_func=my_check) 或 @ai_tools(category="buildin")
    自动从被装饰函数的 __name__ 和 __doc__ 获取工具信息。
    支持智能推断原函数参数，自动注入上下文，并对 PydanticAI 提供完美兼容的 Schema 签名。

    Args:
        func: 被装饰的函数
        category: 工具分类名称，默认 "default"。用于将工具放入不同的分类字典中
        check_func: 可选的权限校验函数
        context_tags: 可选的语境标签列表，如 ["原神", "游戏"]。
            声明后，框架会在匹配该语境的群聊中自动加载本工具（语境工具池）。
        capability_domain: 可选的能力域名称，如 "原神数据"、"网络搜索"。
            声明后，框架会按 domain 聚合成自然语言能力清单注入自我认知（C3-d），
            替代生硬的函数名罗列。未声明时按 category 兜底。
        **check_kwargs: 传递给 check_func 的额外参数
    """

    def decorator(fn: F) -> F:
        # 0. 检查AI是否启用，未启用则跳过工具注册
        try:
            from gsuid_core.ai_core.configs.ai_config import ai_config

            if not ai_config.get_config("enable").data:
                return cast(F, fn)
        except Exception:
            pass

        # 1. 解析原函数的参数签名
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())

        # 2. 判断原函数需要什么类型的上下文
        func_takes_run_context = False
        func_takes_tool_context = False

        # 记录需要自动注入且不需要暴露给 LLM 的参数 {参数名: "Event" | "Bot"}
        injected_params: Dict[str, str] = {}

        for p in params:
            anno_str = str(p.annotation)
            if "RunContext" in anno_str:
                func_takes_run_context = True
            elif "ToolContext" in anno_str:
                func_takes_tool_context = True
            elif "Event" in anno_str:
                injected_params[p.name] = "Event"
            elif "Bot" in anno_str:
                injected_params[p.name] = "Bot"

        # 3. 构造实际被 PydanticAI 调用的 wrapper
        async def wrapped_tool(ctx: RunContext[ToolContext], *args, **kwargs):
            # 执行拦截器校验
            if check_func:
                # 根据 check_func 的参数签名自动注入依赖
                check_sig = inspect.signature(check_func)

                # 构建调用参数
                check_call_kwargs: Dict[str, object] = {}

                for name, param in check_sig.parameters.items():
                    # 获取类型注解的字符串表示
                    anno_str = str(param.annotation)
                    if "Event" in anno_str:
                        check_call_kwargs[name] = ctx.deps.ev
                    elif "Bot" in anno_str:
                        check_call_kwargs[name] = ctx.deps.bot

                # 合并用户传入的 check_kwargs
                final_check_kwargs = {**check_call_kwargs, **check_kwargs}

                # 支持同步和异步 check_func
                check_result = check_func(**final_check_kwargs)
                if asyncio.iscoroutine(check_result):
                    is_passed, message = await check_result
                elif isinstance(check_result, Tuple):
                    is_passed, message = check_result[0], check_result[1]
                else:
                    logger.warning("🧠 [Register] @ai_tools 装饰器 check_func 存在问题, 请开发者检查...")
                    return "@ai_tools 装饰器 check_func 存在问题, 请开发者检查"

                if not is_passed:
                    return message

            # 复制一份 kwargs 以防修改原始引用
            call_kwargs = dict(kwargs)
            for param_name, inject_type in injected_params.items():
                if inject_type == "Event":
                    call_kwargs[param_name] = ctx.deps.ev
                elif inject_type == "Bot":
                    call_kwargs[param_name] = ctx.deps.bot

            # ===== 智能传参 =====
            if func_takes_run_context:
                raw_result = await fn(ctx, *args, **call_kwargs)
            elif func_takes_tool_context:
                raw_result = await fn(ctx.deps, *args, **call_kwargs)
            else:
                raw_result = await fn(*args, **call_kwargs)

            # 处理并返回结果
            result = await handle_tool_result(ctx.deps.bot, raw_result)
            return result

        # 4. 手动复制核心元数据 (必须包含 __module__ 和 __annotations__)
        wrapped_tool.__name__ = fn.__name__
        wrapped_tool.__doc__ = fn.__doc__
        wrapped_tool.__qualname__ = fn.__qualname__
        wrapped_tool.__module__ = fn.__module__  # 确保 typing.get_type_hints 能找到正确的上下文变量

        # 将原函数的注解复制过来，并补上正确的 ctx 注解
        annotations: Dict[str, object] = getattr(fn, "__annotations__", {}).copy()
        annotations["ctx"] = RunContext[ToolContext]
        for injected_name in injected_params.keys():
            annotations.pop(injected_name, None)
        wrapped_tool.__annotations__ = annotations

        # 5. 重写函数的 __signature__
        new_params = [
            inspect.Parameter(
                "ctx",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=RunContext[ToolContext],
            )
        ]

        for i, p in enumerate(params):
            if i == 0 and (func_takes_run_context or func_takes_tool_context):
                continue  # 跳过原函数旧的 context 参数
            if p.name in injected_params:
                continue
            new_params.append(p)

        wrapped_tool.__signature__ = sig.replace(parameters=new_params)

        # 6. 注册工具
        tool_obj = Tool(wrapped_tool, takes_ctx=True)

        # 获取插件名称
        plugin_name = _get_plugin_name_from_module(fn.__module__)

        logger.debug(f"🧠 [Register] @ai_tools 装饰器执行，注册工具: {fn.__name__} (分类: {category})")

        tool_base = ToolBase(
            name=fn.__name__,
            description=(wrapped_tool.__doc__ or "").strip(),
            plugin=plugin_name,
            tool=tool_obj,
            context_tags=context_tags,
            capability_domain=capability_domain,
        )

        # 根据 category 分类注册工具
        if category not in _TOOL_REGISTRY:
            _TOOL_REGISTRY[category] = {}
        _TOOL_REGISTRY[category][fn.__name__] = tool_base

        return cast(F, wrapped_tool)

    if func is None:
        return decorator
    return cast(F, decorator(cast(F, func)))


def get_registered_tools() -> Dict[str, Dict[str, ToolBase]]:
    """获取所有已注册的工具（按分类）"""
    return _TOOL_REGISTRY


def get_all_tools() -> Dict[str, ToolBase]:
    """获取所有已注册的工具（平铺结构）"""
    result = {}
    for category_tools in _TOOL_REGISTRY.values():
        result.update(category_tools)
    return result


def ai_alias(name: str, alias: Union[str, List[str]], scope: str = "global"):
    """
    为特定实体注册别名。

    注册的别名会接入 AI 记忆摄入链路（C2-c）：实体抽取时框架会把命中的别名
    作为"本群已知别名"注入提取提示词，指导 LLM 把别名对齐到正式名；
    检索期也会用于查询展开。

    Args:
        name:  正式名称
        alias: 单个别名或别名列表
        scope: 别名作用域，默认 "global"（通用）。插件可传业务 scope（如 "Genshin"）
               隔离同名别名，避免"深渊"等词在不同游戏间串味。

    调用时, 例如:

        from gsuid_core.ai_core.register import ai_alias

        ai_alias("丝柯克", ['skk', '斯柯克'])
        ai_alias("幽境危战", "深渊", scope="WutheringWaves")
    """
    # 检查AI是否启用，未启用则跳过别名注册
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if not ai_config.get_config("enable").data:
            return
    except Exception:
        pass

    if isinstance(alias, str):
        alias = [alias]

    scope_map = _ALIASES.setdefault(scope, {})
    for a in alias:
        formals = scope_map.setdefault(a, [])
        if name not in formals:
            formals.append(name)

    logger.trace(f"🧠 [AI][Registry] Registered aliases for {name} (scope={scope}): {alias}")


def get_aliases_for_scope(scope: str = "global") -> Dict[str, List[str]]:
    """获取指定 scope 的别名映射（合并 global 兜底）。

    返回扁平的 {别名: [正式名候选, ...]} 字典，供记忆摄入提示词注入（C2-a）
    与检索期查询展开 / 动态实体链接（C2-e）消费。

    Args:
        scope: 业务 scope；始终额外并入 "global" 通用别名。
    """
    merged: Dict[str, List[str]] = {}
    for src_scope in ("global", scope):
        scope_map = _ALIASES[src_scope] if src_scope in _ALIASES else None
        if not scope_map:
            continue
        for alias, formals in scope_map.items():
            bucket = merged.setdefault(alias, [])
            for f in formals:
                if f not in bucket:
                    bucket.append(f)
    return merged


def ai_entity(entity: Union[KnowledgePoint, KnowledgeBase]):
    """
    将实体注册为大模型实体。
    在启动时，自动将实体存入全局注册表。
    知识库同步时会检查插件注册的知识，新增/修改/删除操作。

        entity: 一个包含实体信息的字典, 不需要传入 _hash, 会自动计算

            id: str
            plugin: str
            type: str
            category: str
            title: str
            content: str
            tags: List[str]
            source: str (自动设置为 "plugin")

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
    # 检查AI是否启用，未启用则跳过实体注册
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if not ai_config.get_config("enable").data:
            return
    except Exception:
        pass

    # 自动添加 source="plugin" 标识，表示来自插件注册
    entity["source"] = "plugin"
    _ENTITIES.append(entity)
    logger.trace(f"🧠 [AI][Registry] Entity registered (plugin): {entity['title']}")


def add_manual_knowledge(entity: ManualKnowledgeBase) -> bool:
    """
    手动添加知识库条目。
    这些知识不会在启动时被检查、不会自动修改或删除，
    也不会参与插件知识库的同步流程。

    适用于通过前端API手动添加的持久化知识。

        entity: 知识库条目，需包含以下字段

            id: str
            plugin: str
            type: str
            category: str
            title: str
            content: str
            tags: List[str]
            source: str (固定为 "manual")

    返回:
        bool: 如果 id 已存在则返回 False，否则返回 True

    例如:

    from gsuid_core.ai_core.models import ManualKnowledgeBase
    from gsuid_core.ai_core.register import add_manual_knowledge

    add_manual_knowledge(ManualKnowledgeBase(
        id="manual_001",
        plugin="manual",
        type="自定义知识",
        category="自定义",
        title="手动添加的知识",
        content="这是手动添加的知识内容...",
        tags=["手动", "自定义"],
        source="manual",
    ))
    """
    # 检查是否已存在相同 id
    for existing in _MANUAL_ENTITIES:
        if existing["id"] == entity["id"]:
            logger.warning(f"🧠 [AI][Registry] Manual entity already exists: {entity['id']}")
            return False

    # 确保 source 为 "manual"
    entity["source"] = "manual"
    _MANUAL_ENTITIES.append(entity)
    logger.trace(f"🧠 [AI][Registry] Manual entity added: {entity['title']}")
    return True


def update_manual_knowledge(entity_id: str, updates: dict) -> bool:
    """
    更新手动添加的知识库条目。

    Args:
        entity_id: 要更新的知识库 ID
        updates: 要更新的字段

    Returns:
        bool: 如果找到并更新则返回 True，否则返回 False
    """
    for i, existing in enumerate(_MANUAL_ENTITIES):
        if existing["id"] == entity_id:
            # 不允许修改 id 和 source
            updates.pop("id", None)
            updates.pop("source", None)
            _MANUAL_ENTITIES[i].update(updates)
            logger.trace(f"🧠 [AI][Registry] Manual entity updated: {entity_id}")
            return True
    return False


def delete_manual_knowledge(entity_id: str) -> bool:
    """
    删除手动添加的知识库条目。

    Args:
        entity_id: 要删除的知识库 ID

    Returns:
        bool: 如果找到并删除则返回 True，否则返回 False
    """
    for i, existing in enumerate(_MANUAL_ENTITIES):
        if existing["id"] == entity_id:
            _MANUAL_ENTITIES.pop(i)
            logger.trace(f"🧠 [AI][Registry] Manual entity deleted: {entity_id}")
            return True
    return False


def get_manual_entities() -> List[ManualKnowledgeBase]:
    """获取所有手动添加的知识库条目"""
    return _MANUAL_ENTITIES.copy()


def get_manual_entity(entity_id: str) -> Optional[ManualKnowledgeBase]:
    """获取指定 ID 的手动添加的知识库条目"""
    for existing in _MANUAL_ENTITIES:
        if existing["id"] == entity_id:
            return existing
    return None


def ai_image(entity: ImageEntity):
    """
    将图片实体注册为可检索的图片。
    在启动时，自动将图片实体存入全局注册表，并同步到向量库。

    插件作者可以通过此函数注册图片，让 AI 能够根据描述语义搜索到图片。

    Args:
        entity: 图片实体，包含以下字段:

            id: str - 唯一标识符
            plugin: str - 插件名称
            path: str - 图片文件路径（绝对路径或相对路径）
            tags: List[str] - 图片标签，用于描述图片内容，如 ["胡桃", "原神", "角色"]
            content: str - 详细描述文本，可选
            source: str (自动设置为 "plugin")

    例如:

    from gsuid_core.ai_core.models import ImageEntity
    from gsuid_core.ai_core.register import ai_image

    ai_image(ImageEntity(
        id="hutao_character",
        plugin="GenshinUID",
        path="./resources/characters/hutao.png",
        tags=["胡桃", "原神", "角色", "火系"],
        content="胡桃角色立绘图片，往生堂第七十七代堂主",
        source="plugin",
        _hash="",
    ))

    然后在代码中可以通过 RAG 搜索获取图片:

    from gsuid_core.ai_core.rag.image_rag import search_and_load_image

    image = await search_and_load_image("给我看看胡桃的图片")
    if image:
        await bot.send(image)
    """
    # 检查AI是否启用，未启用则跳过图片注册
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if not ai_config.get_config("enable").data:
            return
    except Exception:
        pass

    # 自动添加 source="plugin" 标识
    entity["source"] = "plugin"
    _ENTITIES.append(entity)
    _IMAGE_ENTITIES.append(entity)
    logger.trace(f"🧠 [AI][Registry] Image registered: {entity.get('tags', [])}")


def get_image_entities() -> List[ImageEntity]:
    """获取所有已注册的图片实体"""
    return _IMAGE_ENTITIES.copy()


def get_image_entity(entity_id: str) -> Optional[ImageEntity]:
    """获取指定 ID 的图片实体"""
    for entity in _IMAGE_ENTITIES:
        if entity["id"] == entity_id:
            return entity
    return None
