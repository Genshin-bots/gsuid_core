import asyncio
import inspect
from typing import Dict, List, Tuple, Union, TypeVar, Callable, Optional, Awaitable, cast, overload
from pathlib import Path

from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.tools import Tool

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.segment import Message
from gsuid_core.ai_core.utils import handle_tool_result
from gsuid_core.ai_core.models import ToolContext

from .models import ToolBase, ImageEntity, KnowledgeBase, KnowledgePoint, ManualKnowledgeBase, ManualKnowledgeUpdate

# 工具函数返回契约：str/Message/bytes 经 handle_tool_result 序列化；
# ToolReturn 原样透传 pydantic_ai（多模态内容注入会话，如 read_image 直投）
ToolFunc = Callable[..., Awaitable[Union[str, Message, bytes, ToolReturn]]]
F = TypeVar("F", bound=ToolFunc)

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
# 框架特权分类：self/buildin 无条件进保底池、meta 为 gs_agent 门控专用，仅核心代码可用；
# 插件声明时重定向到 common——仍可被向量检索/语境池召回，但不进保底池、不碰门控。
_CORE_ONLY_CATEGORIES = frozenset({"self", "buildin", "meta"})

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
    func: Optional[ToolFunc] = None,
    /,
    *,
    category: str = "default",
    check_func: Optional[CheckFunc] = None,
    context_tags: Optional[List[str]] = None,
    capability_domain: Optional[str] = None,
    visible_when: Optional[Callable[..., Union[bool, Awaitable[bool]]]] = None,
    timeout: Optional[float] = 300.0,
    approval: Optional[str] = None,
    **check_kwargs,
) -> Callable[[F], F] | F:
    """
    用法: @ai_tools 或 @ai_tools(check_func=my_check) 或 @ai_tools(category="buildin")
    自动从被装饰函数的 __name__ 和 __doc__ 获取工具信息。
    支持智能推断原函数参数，自动注入上下文，并对 PydanticAI 提供完美兼容的 Schema 签名。

    Args:
        func: 被装饰的函数
        category: 工具分类名称，默认 "default"。用于将工具放入不同的分类字典中。
            self/buildin/meta 为框架特权分类，仅核心代码可用；插件声明时会被
            自动重定向到 common 注册（见 _CORE_ONLY_CATEGORIES）。
        check_func: 可选的权限校验函数
        context_tags: 可选的语境标签列表，如 ["原神", "游戏"]。
            声明后，框架会在匹配该语境的群聊中自动加载本工具（语境工具池）。
        capability_domain: 可选的能力域名称，如 "原神数据"、"网络搜索"。
            声明后，框架会按 domain 聚合成自然语言能力清单注入自我认知（C3-d），
            替代生硬的函数名罗列。未声明时按 category 兜底。
        visible_when: 可选的"可见性谓词"（Phase 3 条件隐藏）。签名为
            ``(ctx: RunContext[ToolContext]) -> bool | Awaitable[bool]``。
            返回 False 时，本工具在**该 step**对模型隐藏（schema 都不下发），
            从源头减少无关工具噪声。它在**每个 step**对每个工具求值，因此谓词
            必须**廉价且为内存判定**（读 ev/bot/扩展字段即可，切忌每步查库/发网络）。
            与 check_func 的区别：check_func 在"已调用"后拦截执行并回错误文案；
            visible_when 在"是否展示"阶段决定模型能否看到该工具。判定抛异常时默认可见。
        timeout: 工具调用的最大等待时间（秒），默认 300 秒（5 分钟）。
            超时后工具返回错误字符串，agent 可继续而不会永久挂起。
            设为 None 表示不限制超时。
        approval: 可选的强制审批级别（"user" / "master"）。声明后每次调用先过
            统一审批中心策略门：user 级可被「完全访问」豁免（照常留审计记录）、
            master 级永不可豁免；无有效放行 grant 时拦截并自动提交审批请求，
            批准后重新调用即执行（不依赖 LLM 自觉，防幻觉绕过）。
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
                    logger.warning(t("🧠 [Register] @ai_tools 装饰器 check_func 存在问题, 请开发者检查..."))
                    return "@ai_tools 装饰器 check_func 存在问题, 请开发者检查"

                if not is_passed:
                    return message

            # 审批策略门：声明了 approval 级别的工具在执行前强制过审批中心
            # （check_func 之后——权限不通过的调用不该触发审批请求）。
            if approval in ("user", "master"):
                from gsuid_core.ai_core.approval import tool_call_gate

                gate_msg = await tool_call_gate(ctx.deps.ev, fn.__name__, approval, str(kwargs)[:2000])
                if gate_msg is not None:
                    return gate_msg

            # 复制一份 kwargs 以防修改原始引用
            call_kwargs = dict(kwargs)
            for param_name, inject_type in injected_params.items():
                if inject_type == "Event":
                    call_kwargs[param_name] = ctx.deps.ev
                elif inject_type == "Bot":
                    call_kwargs[param_name] = ctx.deps.bot

            # ===== 智能传参 =====
            async def _call() -> Union[str, Message, bytes, ToolReturn]:
                if func_takes_run_context:
                    return await fn(ctx, *args, **call_kwargs)
                elif func_takes_tool_context:
                    return await fn(ctx.deps, *args, **call_kwargs)
                else:
                    return await fn(*args, **call_kwargs)

            try:
                raw_result = await asyncio.wait_for(_call(), timeout=timeout)
            except asyncio.TimeoutError:
                timeout_sec = int(timeout) if timeout is not None else 0
                logger.warning(
                    t(
                        "🧠 [Register] 工具 [{p0}] 执行超时（>{timeout_sec}s），已中断",
                        p0=fn.__name__,
                        timeout_sec=timeout_sec,
                    )
                )
                return f"⚠️ 工具 {fn.__name__} 执行超时（超过 {timeout_sec} 秒），请稍后重试或换个方式"

            # ToolReturn 原样透传给 pydantic_ai（多模态内容注入会话，如 read_image 直投图片）。
            # 走 handle_tool_result 会被兜底 str() 成 dataclass repr——模型只会看到裸 base64 文本
            if isinstance(raw_result, ToolReturn):
                return raw_result

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

        # 函数对象运行时支持 __signature__（inspect 协议），但 FunctionType 静态无此属性
        setattr(wrapped_tool, "__signature__", sig.replace(parameters=new_params))

        # 5.5 条件隐藏（Phase 3）：visible_when 谓词包装成 pydantic-ai 的 prepare 函数。
        # prepare 在每个 step 被调用，返回 None 即本步不向模型暴露该工具。
        prepare_fn = None
        if visible_when is not None:

            async def _prepare(ctx, tool_def, _pred=visible_when, _name=fn.__name__):
                try:
                    res = _pred(ctx)
                    if inspect.isawaitable(res):
                        res = await res
                except Exception as e:
                    logger.debug(
                        t("🧠 [Register] 工具 [{_name}] visible_when 判定异常，默认可见: {e}", _name=_name, e=e)
                    )
                    return tool_def
                return tool_def if res else None

            prepare_fn = _prepare

        # 6. 注册工具
        tool_obj = Tool(wrapped_tool, takes_ctx=True, prepare=prepare_fn)

        # 获取插件名称
        plugin_name = _get_plugin_name_from_module(fn.__module__)

        # 框架特权分类防护：非核心代码（plugins/ 或未知来源）注册 self/buildin/meta
        # 时重定向到 common（见 _CORE_ONLY_CATEGORIES 注释）。
        reg_category = category
        if plugin_name != "core" and reg_category in _CORE_ONLY_CATEGORIES:
            logger.warning(
                t(
                    "🧠 [Register] 插件 [{plugin_name}] 的工具 [{p0}] 声明了框架特权分类"
                    " [{reg_category}]，已重定向到 [common] 注册",
                    plugin_name=plugin_name,
                    p0=fn.__name__,
                    reg_category=reg_category,
                )
            )
            reg_category = "common"

        logger.debug(
            t(
                "🧠 [Register] @ai_tools 装饰器执行，注册工具: {p0} (分类: {reg_category})",
                p0=fn.__name__,
                reg_category=reg_category,
            )
        )

        # docstring 是工具**唯一**的向量检索文本（入库文本 = name + description），
        # 缺失即等同于"注册了一个永远召不回的工具"，必须吵出来而不是静默注册。
        tool_description = (wrapped_tool.__doc__ or "").strip()
        if not tool_description:
            logger.warning(
                t(
                    "🧠 [Register] 工具 [{p0}]（来源 {plugin_name}）没有 docstring，向量检索只剩"
                    " 函数名、几乎不可能被召回。常见成因：docstring 被写在了函数体首条语句"
                    "（如 logger 调用）之后，那样它只是个普通字符串表达式，不是 docstring。",
                    p0=fn.__name__,
                    plugin_name=plugin_name,
                )
            )

        tool_base = ToolBase(
            name=fn.__name__,
            description=tool_description,
            plugin=plugin_name,
            tool=tool_obj,
            context_tags=context_tags,
            capability_domain=capability_domain,
        )

        # 根据 category 分类注册工具
        if reg_category not in _TOOL_REGISTRY:
            _TOOL_REGISTRY[reg_category] = {}
        _TOOL_REGISTRY[reg_category][fn.__name__] = tool_base

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


def find_tool_base(tool_name: str) -> Optional[ToolBase]:
    """按工具名跨所有分类查找 ToolBase，找不到返回 None。"""
    for category_tools in _TOOL_REGISTRY.values():
        tb = category_tools.get(tool_name)
        if tb is not None:
            return tb
    return None


def get_tools_by_capability_domain(domain: str) -> List[ToolBase]:
    """返回声明了同一 capability_domain（能力族）的所有工具。

    能力族 = 注册时 ``@ai_tools(capability_domain="定时任务")`` 声明的同名工具集合。
    用于"能力族整体召回（L4）"：召回族内任一工具时把整族一起加载，
    保证"能创建就能改/删"（如召回 add_once_task 即带出 modify/cancel_scheduled_task）。
    """
    if not domain:
        return []
    result: List[ToolBase] = []
    for category_tools in _TOOL_REGISTRY.values():
        for tb in category_tools.values():
            if tb.capability_domain == domain:
                result.append(tb)
    return result


def get_family_members(tool_name: str) -> List[ToolBase]:
    """给定工具名，返回与其同属一个能力族（capability_domain）的全部工具。

    - 工具未注册：返回空列表（调用方应回退到仅用该工具自身）。
    - 工具已注册但未声明 capability_domain：返回仅含自身的列表（单工具"族"）。
    """
    target = find_tool_base(tool_name)
    if target is None:
        return []
    if not target.capability_domain:
        return [target]
    return get_tools_by_capability_domain(target.capability_domain)


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

    # 同步进实体身份索引（纯增量，_ALIASES 行为不变——memory 消费方依赖 global scope）。
    # 正式名本身不是 _ALIASES 的键（只有别名是），这里一并登记，否则"玄翎秧秧"查不到。
    _index_entity_surfaces(name, alias)

    logger.trace(f"🧠 [AI][Registry] Registered aliases for {name} (scope={scope}): {alias}")


def _index_entity_surfaces(name: str, alias: List[str]) -> None:
    """把正式名 + 别名登记到 `entity_index`，插件归属取自**调用方所在模块**。

    `ai_alias` 是普通函数（不是装饰器），拿不到 `fn.__module__`，只能回溯调用栈。
    """
    from gsuid_core.ai_core.entity_index import register_entity_surface

    frame = inspect.currentframe()
    caller = frame.f_back.f_back if frame is not None and frame.f_back is not None else None
    module_name = ""
    if caller is not None and "__name__" in caller.f_globals:
        module_name = str(caller.f_globals["__name__"])

    plugin = _get_plugin_name_from_module(module_name)
    register_entity_surface(name, name, plugin)
    for a in alias:
        register_entity_surface(a, name, plugin)


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


def update_manual_knowledge(entity_id: str, updates: ManualKnowledgeUpdate) -> bool:
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


def ai_skill(path: Union[str, Path], plugin: Optional[str] = None) -> None:
    """注册插件 repo 内的 AI Skill 目录（运行时 Skill，非 docs/skills 开发文档）。

    让插件作者把 Skill 随插件一起放在**自己仓库内**管理，无需手动把 skill 文件夹
    挪进 ``data/ai_core/skills/`` 才能生效。注册的目录下可含一个或多个
    ``<skill-name>/SKILL.md``（可选 ``scripts/*.py`` 与资源文件），框架会自动发现，
    主人格 / 能力代理通过 ``list_skills`` / ``load_skill`` / ``run_skill_script`` 调用。

    Skill 与 ``@ai_tools`` 工具的区别：``@ai_tools`` 是 Python 函数（按需向量检索装配）；
    Skill 是 Markdown「带元数据的可执行操作」，由模型主动发现并加载。

    Args:
        path: 插件 repo 内的 skill 根目录，通常为
            ``Path(__file__).parent / "skills"``。
        plugin: 来源插件名；省略时自动从调用方模块路径推断。

    用法（在插件 ``__init__.py`` 顶层调用，import 即注册）::

        from pathlib import Path
        from gsuid_core.ai_core.register import ai_skill

        ai_skill(Path(__file__).parent / "skills")

    目录结构示例::

        MyPlugin/
          skills/
            my-skill/
              SKILL.md          # 必须，含 frontmatter: name / description
              scripts/run.py    # 可选，run_skill_script 调用

    经 webconsole 查看时这些 skill 会被标记为 ``source="plugin"`` 且只读
    （不可在控制台删除 / 改写），请在插件仓库内维护。
    """
    # 检查 AI 是否启用，未启用则跳过技能注册（与 ai_tools / ai_image 一致）
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if not ai_config.get_config("enable").data:
            return
    except Exception:
        pass

    p = Path(path)

    # 未显式传 plugin 时，从调用方模块路径推断插件名
    if plugin is None:
        try:
            caller_module = inspect.stack()[1].frame.f_globals.get("__name__", "")
        except Exception:
            caller_module = ""
        plugin = _get_plugin_name_from_module(caller_module)

    from gsuid_core.ai_core.skills.operations import register_plugin_skill_directory

    result = register_plugin_skill_directory(p.resolve(), plugin)
    if result["status"] == 0:
        logger.info(
            t(
                "🧠 [AI][Registry] Skill 目录注册成功（plugin={plugin}, count={p0}）: {p1}",
                plugin=plugin,
                p0=result.get("count", 0),
                p1=p.resolve(),
            )
        )
    else:
        logger.warning(t("🧠 [AI][Registry] Skill 目录注册失败: {p0}", p0=result["msg"]))
