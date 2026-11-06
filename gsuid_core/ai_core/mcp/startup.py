"""
MCP 工具启动注册模块

启动时读取已启用的 MCP 配置。配置里已有工具清单时直接注册，不连接服务器。
真正调用工具时才建立连接。清单为空时启动跳过；控制台单独注册才会去连。
"""

import inspect
from typing import Any, Dict, Optional, TypedDict

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.mcp.client import MCPClient
from gsuid_core.ai_core.mcp.config_manager import (
    MCPConfig,
    MCPToolDefinition,
    mcp_config_manager,
    input_schema_to_parameters,
)

# 延迟导入 _TOOL_REGISTRY 避免循环依赖。


def _get_tool_registry() -> dict:
    """延迟导入 _TOOL_REGISTRY 以避免循环导入"""
    from gsuid_core.ai_core.register import _TOOL_REGISTRY

    return _TOOL_REGISTRY


# 存储已注册的 MCP 客户端实例，用于关闭时清理。
# 键格式 ``{config_id}@{scope}``：scope=shared 走共享凭据；
# scope=user:{bot_id}:{user_id} 走发起人绑定凭据（永不回落 shared）。
_mcp_clients: Dict[str, MCPClient] = {}

_SHARED_SCOPE = "shared"


def _scope_key(config_id: str, scope: str) -> str:
    return f"{config_id}@{scope}"


def _user_scope(bot_id: str, user_id: str) -> str:
    return f"user:{bot_id}:{user_id}"


def drop_user_client_cache(config_id: str, bot_id: str, user_id: str) -> None:
    """重绑/解绑/过期/吊销后调用：丢弃该用户对该服务的缓存客户端。"""
    _mcp_clients.pop(_scope_key(config_id, _user_scope(bot_id, user_id)), None)


def _drop_config_clients(config_id: str) -> None:
    """丢弃该服务全部 scope 的缓存客户端（更新/删除/禁用/关闭时调用）。"""
    prefix = f"{config_id}@"
    for key in [k for k in _mcp_clients if k.startswith(prefix)]:
        del _mcp_clients[key]


# MCP 工具分类名称
MCP_CATEGORY = "mcp"

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class JsonSchemaProperty(TypedDict, total=False):
    type: str
    description: str
    enum: list[JsonValue]
    default: JsonValue
    anyOf: list[JsonValue]
    allOf: list[JsonValue]
    oneOf: list[JsonValue]
    items: JsonValue


class JsonSchemaObject(TypedDict, total=False):
    type: str
    properties: dict[str, JsonSchemaProperty]
    required: list[str]


def _as_json_value(raw: object) -> JsonValue | None:
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return raw
    if isinstance(raw, list):
        items: list[JsonValue] = []
        for item in raw:
            converted = _as_json_value(item)
            if converted is None and item is not None:
                continue
            items.append(converted)
        return items
    if isinstance(raw, dict):
        mapping: dict[str, JsonValue] = {}
        for key, val in raw.items():
            if not isinstance(key, str):
                continue
            converted = _as_json_value(val)
            if converted is None and val is not None:
                continue
            mapping[key] = converted
        return mapping
    return None


def _json_schema_property(spec: object) -> JsonSchemaProperty:
    prop: JsonSchemaProperty = {}
    if not isinstance(spec, dict):
        return prop
    type_name = spec["type"] if "type" in spec else ""
    if isinstance(type_name, str) and type_name:
        prop["type"] = type_name
    description = spec["description"] if "description" in spec else ""
    if isinstance(description, str) and description:
        prop["description"] = description
    for extra_key in ("enum", "default", "anyOf", "allOf", "oneOf", "items"):
        if extra_key not in spec:
            continue
        converted = _as_json_value(spec[extra_key])
        if converted is None and spec[extra_key] is not None:
            continue
        if extra_key == "enum" and isinstance(converted, list):
            prop["enum"] = converted
        elif extra_key == "items" and converted is not None:
            prop["items"] = converted
        elif extra_key == "anyOf" and isinstance(converted, list):
            prop["anyOf"] = converted
        elif extra_key == "allOf" and isinstance(converted, list):
            prop["allOf"] = converted
        elif extra_key == "oneOf" and isinstance(converted, list):
            prop["oneOf"] = converted
        elif extra_key == "default":
            prop["default"] = converted
    return prop


def _schema_object(properties: dict[str, JsonSchemaProperty], required: list[str]) -> JsonSchemaObject:
    schema: JsonSchemaObject = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _input_schema_from_parameters(parameters: object) -> JsonSchemaObject:
    """把配置里的扁平参数还原成注册函数签名用的 JSON Schema。"""
    properties: dict[str, JsonSchemaProperty] = {}
    required: list[str] = []
    if not isinstance(parameters, dict):
        return _schema_object(properties, required)
    for name, spec in parameters.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        properties[name] = _json_schema_property(spec)
        if "required" in spec and spec["required"] is True:
            required.append(name)
    return _schema_object(properties, required)


def _input_schema_from_json_schema(raw: object) -> JsonSchemaObject:
    """MCP list_tools 给的已是 JSON Schema object。"""
    properties: dict[str, JsonSchemaProperty] = {}
    required: list[str] = []
    if not isinstance(raw, dict):
        return _schema_object(properties, required)
    raw_props = raw["properties"] if "properties" in raw else None
    if isinstance(raw_props, dict):
        for name, spec in raw_props.items():
            if not isinstance(name, str):
                continue
            properties[name] = _json_schema_property(spec)
    raw_required = raw["required"] if "required" in raw else None
    if isinstance(raw_required, list):
        required = [item for item in raw_required if isinstance(item, str)]
    return _schema_object(properties, required)


def _make_mcp_client(config: MCPConfig, creds: Optional[Dict[str, str]] = None) -> MCPClient:
    from gsuid_core.ai_core.mcp.credential_store import render_client_kwargs

    kwargs = render_client_kwargs(config, creds)
    return MCPClient(
        name=config.name,
        transport=config.get_transport(),
        command=kwargs["command"],
        args=kwargs["args"],
        env=kwargs["env"],
        url=kwargs["url"],
        headers=kwargs["headers"],
        cwd=config.cwd,
    )


async def _resolve_scope_client(
    config_id: str,
    config: MCPConfig,
    ev: Optional["Event"],
) -> "tuple[MCPClient | None, str | None, Dict[str, str] | None]":
    """裁决 private 执行 scope → (client, 错误/BIND_GUIDE, creds map)。

    发起人本人凭据；未绑/过期→BIND_GUIDE；ev None→按
    allow_autonomous_private 决定（mutating 由调用方先拒）。
    """
    from gsuid_core.ai_core.mcp.client import ensure_checkout_for_config
    from gsuid_core.ai_core.mcp.bind_command import bind_guide_for_tool
    from gsuid_core.ai_core.mcp.credential_store import resolve_user_creds

    checkout_err = await ensure_checkout_for_config(config)
    if checkout_err:
        logger.warning(t("log.mcp.checkout_failed", p0=config.name, p1=checkout_err))

    if ev is None:
        if not config.allow_autonomous_private:
            return None, "该工具需要用户凭据，且服务未允许无人任务使用共享凭据执行", None
        return _make_mcp_client(config), None, dict(config.env)

    scope = _user_scope(ev.bot_id, ev.user_id)
    cache_key = _scope_key(config_id, scope)

    creds, row = await resolve_user_creds(config_id, ev.bot_id, ev.user_id)
    if creds is None:
        _mcp_clients.pop(cache_key, None)
        return None, bind_guide_for_tool(config_id), None

    cached = _mcp_clients.get(cache_key)
    if cached is not None:
        return cached, None, creds

    client = _make_mcp_client(config, creds)
    _mcp_clients[cache_key] = client
    return client, None, creds


# 共享 scope 下拒绝的账号指针参数（public 工具不该收这些）
_SHARED_POINTER_RE = (
    "orderid|order_id|addressid|address_id|phone|mobile|uid|userid|user_id|"
    "idcard|id_card|openid|open_id|member|customer"
)


def _reject_shared_pointer_args(tool_name: str, call_args: Dict[str, Any]) -> Optional[str]:
    """shared scope 参数指针拒绝：账号级指针不应出现在共享凭据调用里。"""
    import re as _re

    pattern = _re.compile(_SHARED_POINTER_RE, _re.IGNORECASE)
    for key in call_args:
        if pattern.search(key):
            return (
                f"⚠️ 该工具以共享凭据执行，参数 '{key}' 疑似账号指针。"
                f"如需查询个人数据，请将该工具设为私有并绑定本人凭据。"
            )
    return None


def _register_catalog(config_id: str, config: MCPConfig) -> list[str]:
    """按已保存的工具清单注册。不连接服务器。返回实际注册工具名列表。"""
    from gsuid_core.ai_core.mcp.node_profile import mcp_tool_covers

    client = _make_mcp_client(config)
    _mcp_clients[_scope_key(config_id, _SHARED_SCOPE)] = client
    registered_names: list[str] = []
    for tool_def in config.tools:
        try:
            registered_names.append(
                _register_mcp_tool(
                    client,
                    tool_def.name,
                    tool_def.description,
                    _input_schema_from_parameters(tool_def.parameters),
                    config=config,
                    config_id=config_id,
                    tool_def=tool_def,
                    covers=mcp_tool_covers(config, tool_def.description),
                )
            )
        except Exception as e:
            logger.error(t("log.mcp.fail_register_tool_failed", p0=config.name, p1=tool_def.name, e=e))
    return registered_names


def _json_schema_type_to_python(json_type: str) -> type:
    """将 JSON Schema 类型映射为 Python 类型"""
    type_map: Dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    return type_map.get(json_type, str)


def _build_mcp_tool_function(
    client: MCPClient,
    tool_name: str,
    tool_description: str,
    input_schema: JsonSchemaObject,
    config_id: str = "",
    config: "MCPConfig | None" = None,
    tool_def: "MCPToolDefinition | None" = None,
) -> Any:
    """
    为 MCP 工具动态创建包装函数。

    根据 MCP 工具的 input_schema 生成正确的函数签名，
    使 PydanticAI 能够正确生成工具的 JSON Schema 给 LLM。

    Args:
        client: MCP 客户端实例
        tool_name: MCP 工具名称
        tool_description: MCP 工具描述
        input_schema: MCP 工具的 JSON Schema 输入参数定义

    Returns:
        动态创建的异步函数
    """
    properties: dict[str, JsonSchemaProperty] = input_schema["properties"] if "properties" in input_schema else {}
    required_fields: list[str] = input_schema["required"] if "required" in input_schema else []

    # credential_params 从 LLM 可见签名剔除——凭据参数由 wrapper 内 _do_call 注入，
    # agent 从头到尾不知道该参数存在（不进上下文/日志/approval 载荷）。
    cred_params = tool_def.credential_params if tool_def is not None else []
    for cred_param in cred_params:
        properties.pop(cred_param, None)
        if cred_param in required_fields:
            required_fields.remove(cred_param)
    exposure = tool_def.exposure if tool_def is not None else "private"
    mutating = tool_def.mutating if tool_def is not None else False
    sensitive_args = tool_def.sensitive_args if tool_def is not None else bool(cred_params)

    # 构建函数参数注解
    annotations: Dict[str, Any] = {"ctx": RunContext[ToolContext]}
    default_values: Dict[str, Any] = {}

    for param_name, param_schema in properties.items():
        param_type_str = param_schema["type"] if "type" in param_schema else "string"
        param_type = _json_schema_type_to_python(param_type_str)
        annotations[param_name] = param_type

        # 非必填参数给默认值
        if param_name not in required_fields:
            default_values[param_name] = None
            # 将类型改为 Optional
            annotations[param_name] = Optional[param_type]

    # 构建函数签名参数列表
    params = [
        inspect.Parameter(
            "ctx",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=RunContext[ToolContext],
        )
    ]

    # 必填参数必须在带默认值参数之前，否则 inspect.Signature 报
    # "non-default argument follows default argument"（schema 属性顺序不可控）。
    ordered_param_names = [n for n in properties if n in required_fields] + [
        n for n in properties if n not in required_fields
    ]
    for param_name in ordered_param_names:
        default = default_values.get(param_name, inspect.Parameter.empty)
        params.append(
            inspect.Parameter(
                param_name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=annotations[param_name],
                default=default,
            )
        )

    # 创建包装函数
    async def mcp_tool_wrapper(ctx: RunContext[ToolContext], **kwargs: Any) -> str:
        """MCP 工具包装函数：scope 裁决 → 凭据注入 → mutating 审批 → 调用。"""
        from gsuid_core.ai_core.mcp.pii import mask_pii

        ev = ctx.deps.ev if ctx.deps is not None else None

        # mutating + 无人执行体无条件拒（写操作必须有人确认）
        if mutating and ev is None:
            return "该工具会修改账号状态，无人任务不允许执行。"

        # 1. scope 裁决：private→发起人凭据；public→共享凭据
        run_client = client
        scope_creds: "Dict[str, str] | None" = dict(config.env) if config is not None else None
        if exposure == "private":
            if config is None or not config_id:
                return "MCP 配置缺失，无法执行私有工具。"
            run_client, err, scope_creds = await _resolve_scope_client(config_id, config, ev)
            if run_client is None:
                return err or "需要绑定凭据。"

        # 2. 过滤 None + sensitive_args 掩码（参数不记日志/trace）
        call_args = {k: v for k, v in kwargs.items() if v is not None}
        if exposure == "public" or ev is None:
            pointer_err = _reject_shared_pointer_args(tool_name, call_args)
            if pointer_err is not None:
                return pointer_err

        safe_args_repr = mask_pii(repr(call_args)) if sensitive_args else repr(call_args)
        logger.info(
            t(
                "log.mcp.tool_calling_name_args_call",
                p0=client.name,
                tool_name=tool_name,
                call_args=safe_args_repr,
            )
        )

        # 3. mutating → tool_call_gate 用户确认（args_repr 不含注入凭据）
        from gsuid_core.ai_core.approval import tool_call_gate

        async def _do_call() -> str:
            """凭据注入只发生在审批通过后的这个闭包里。"""
            inject_args = dict(call_args)
            if cred_params and scope_creds is not None:
                for p in cred_params:
                    if p in scope_creds:
                        inject_args[p] = scope_creds[p]
            try:
                result = await run_client.call_tool(tool_name, inject_args)
                if result.is_error:
                    error_text = result.text
                    logger.warning(
                        t(
                            "log.mcp.mcp_tool_name_error_text_fail",
                            p0=client.name,
                            tool_name=tool_name,
                            error_text=error_text,
                        )
                    )
                    return f"MCP 工具执行失败: {error_text}"
                return result.text
            except Exception as e:
                logger.error(t("log.mcp.mcp_tool_name_call_fail", p0=client.name, tool_name=tool_name, e=e))
                return f"MCP 工具调用异常: {e}"

        if mutating:
            gate_msg = await tool_call_gate(ev, tool_name, "user", safe_args_repr[:2000], _do_call)
            if gate_msg is not None:
                return gate_msg if isinstance(gate_msg, str) else str(gate_msg)

        return await _do_call()

    # 设置函数元数据
    mcp_tool_wrapper.__name__ = tool_name
    mcp_tool_wrapper.__doc__ = tool_description
    mcp_tool_wrapper.__qualname__ = f"mcp.{client.name}.{tool_name}"
    mcp_tool_wrapper.__module__ = "gsuid_core.ai_core.mcp.startup"
    mcp_tool_wrapper.__annotations__ = annotations
    # mcp_tool_wrapper 是 FunctionType, 静态类型无 __signature__ 字段;
    # setattr 动态写入，pyright 不再标红（与 register.py 一致）。
    setattr(mcp_tool_wrapper, "__signature__", inspect.Signature(parameters=params))

    return mcp_tool_wrapper


def _build_mcp_check_func(
    config: MCPConfig,
    tool_name: str,
) -> Any:
    """根据 MCPConfig 的 tool_permissions 为工具生成权限检查函数。

    权限等级与 Event.user_pm 对比：
    - pm=0: 仅 master 用户
    - pm=1: superuser 及以上
    - pm=2: 群主及以上
    - pm=3: 群管理员及以上
    - pm=6: 所有用户（默认，不生成检查函数）

    Args:
        config: MCP 服务器配置
        tool_name: MCP 工具名称

    Returns:
        权限检查函数，如果不需要权限检查则返回 None
    """
    required_pm = config.get_tool_required_pm(tool_name)

    # required_pm >= 6 表示所有人可用，无需检查
    if required_pm >= 6:
        return None

    async def _mcp_check_func(ev: Any) -> tuple[bool, str]:
        """MCP 工具权限检查函数"""
        if ev.user_pm > required_pm:
            return False, (
                f"❌ 权限不足：MCP 工具 '{tool_name}' 需要 pm<={required_pm}，当前用户权限等级为 {ev.user_pm}。"
            )
        return True, ""

    _mcp_check_func.__name__ = f"_check_mcp_{tool_name}"
    return _mcp_check_func


def _register_mcp_tool(
    client: MCPClient,
    tool_name: str,
    tool_description: str,
    input_schema: JsonSchemaObject,
    config: MCPConfig | None = None,
    config_id: str = "",
    tool_def: "MCPToolDefinition | None" = None,
    covers: list[str] | None = None,
) -> str:
    """
    将单个 MCP 工具注册到 _TOOL_REGISTRY。返回实际注册名（已存在也返回）。

    Args:
        client: MCP 客户端实例
        tool_name: MCP 工具名称
        tool_description: MCP 工具描述
        input_schema: MCP 工具的输入参数 JSON Schema
        config: MCP 服务器配置（用于权限检查）
        covers: 检索短语（服务级描述 + 工具描述首行）
    """
    # 使用 client.name 作为前缀避免工具名冲突
    registered_name = f"mcp_{client.name}_{tool_name}"

    # 检查是否已注册
    tool_registry = _get_tool_registry()
    if MCP_CATEGORY in tool_registry and registered_name in tool_registry[MCP_CATEGORY]:
        logger.debug(t("log.mcp.registered_skipping_name", registered_name=registered_name))
        return registered_name

    # 创建包装函数（tool_def 缺失时按名字从 config.tools 兜底查分级字段）
    if tool_def is None and config is not None:
        for candidate in config.tools:
            if candidate.name == tool_name:
                tool_def = candidate
                break
    wrapper_func = _build_mcp_tool_function(
        client,
        tool_name,
        tool_description,
        input_schema,
        config_id=config_id,
        config=config,
        tool_def=tool_def,
    )

    # 根据 tool_permissions 生成权限检查函数
    check_func = None
    if config is not None:
        check_func = _build_mcp_check_func(config, tool_name)
        if check_func is not None:
            logger.info(
                t(
                    "log.mcp.registered_name_permission_check",
                    registered_name=registered_name,
                    p0=config.get_tool_required_pm(tool_name),
                )
            )

    # 创建 PydanticAI Tool 对象
    tool_obj = Tool(wrapper_func, takes_ctx=True)

    # 创建 ToolBase 并注册
    tool_base = ToolBase(
        name=registered_name,
        description=f"[MCP:{client.name}] {tool_description}",
        plugin=f"mcp_{client.name}",
        tool=tool_obj,
        check_func=check_func,
        category=MCP_CATEGORY,
        covers=covers or [],
    )

    if MCP_CATEGORY not in tool_registry:
        tool_registry[MCP_CATEGORY] = {}
    tool_registry[MCP_CATEGORY][registered_name] = tool_base

    logger.info(t("log.mcp.register_registered_name", registered_name=registered_name, p0=client.name))
    return registered_name


async def _register_mcp_server(config_id: str, config: MCPConfig) -> list[str]:
    """
    连接服务器注册其全部工具，返回实际注册工具名列表（连接失败返回空）。
    """
    from gsuid_core.ai_core.mcp.client import ensure_checkout_for_config
    from gsuid_core.ai_core.mcp.node_profile import mcp_tool_covers

    checkout_err = await ensure_checkout_for_config(config)
    if checkout_err:
        logger.warning(t("log.mcp.checkout_failed", p0=config.name, p1=checkout_err))
    client = _make_mcp_client(config)

    try:
        tools = await client.list_tools()
    except Exception as e:
        logger.error(t("log.mcp.connect_server", p0=config.name, e=e))
        return []

    # 保存客户端引用（shared scope；用户 scope 客户端按需另建）
    _mcp_clients[_scope_key(config_id, _SHARED_SCOPE)] = client

    # 回写工具清单：重启后按目录直注册，不再依赖此刻可连服务器。
    ok, save_msg = mcp_config_manager.update_config(
        config_id,
        {
            "tools": [
                {
                    "name": tool_info.name,
                    "description": tool_info.description,
                    "parameters": input_schema_to_parameters(tool_info.input_schema),
                }
                for tool_info in tools
            ]
        },
    )
    if not ok:
        logger.warning(t("log.mcp.catalog_persist_failed", p0=config.name, p1=save_msg))

    registered_names: list[str] = []
    for tool_info in tools:
        try:
            registered_names.append(
                _register_mcp_tool(
                    client,
                    tool_info.name,
                    tool_info.description,
                    _input_schema_from_json_schema(tool_info.input_schema),
                    config=config,
                    config_id=config_id,
                    covers=mcp_tool_covers(config, tool_info.description),
                )
            )
        except Exception as e:
            logger.error(t("log.mcp.fail_register_tool_failed", p0=config.name, p1=tool_info.name, e=e))

    return registered_names


def _forget_mcp_server(config_id: str) -> int:
    """从注册表摘掉该服务器的工具。不写向量库。"""
    tool_registry = _get_tool_registry()
    if MCP_CATEGORY not in tool_registry:
        return 0

    config = mcp_config_manager.get_config(config_id)
    if config is None:
        client = _mcp_clients.get(_scope_key(config_id, _SHARED_SCOPE))
        server_name = client.name if client is not None else config_id
    else:
        server_name = config.name

    # 按 ToolBase.plugin 精确匹配，避免 "a" 误摘 "a_b" 的工具。
    plugin_name = f"mcp_{server_name}"
    tools_to_remove = [
        name for name, tool_base in tool_registry[MCP_CATEGORY].items() if tool_base.plugin == plugin_name
    ]

    for tool_name in tools_to_remove:
        del tool_registry[MCP_CATEGORY][tool_name]
        logger.info(t("log.mcp.unregister_name_tool", tool_name=tool_name))

    _drop_config_clients(config_id)
    return len(tools_to_remove)


async def _sync_tool_index() -> None:
    # RAG 的 sync_tools 早于 MCP 注册。这里不再同步，向量检索就看不到这些工具。
    # 向量库/嵌入服务不可达时只告警：索引是辅助链路，不能反过来把安装/更新主流程打成 500。
    from gsuid_core.ai_core.register import get_all_tools
    from gsuid_core.ai_core.rag.tools import sync_tools

    try:
        await sync_tools(get_all_tools())
    except Exception as e:
        logger.warning(t("log.mcp.sync_tool_index_failed", e=e))


async def _warm_agent_node_index() -> None:
    """节点注册后预热语义向量索引；失败只告警，不影响注册结果。"""
    try:
        from gsuid_core.ai_core.agent_node.semantic_routing import sync_agent_nodes

        await sync_agent_nodes()
    except Exception as e:
        logger.warning(t("log.mcp.node_index_warm_failed", e=e))


def _cleanup_orphan_mcp_nodes(alive_node_ids: set[str]) -> int:
    """摘掉注册表里已没有对应配置的 mcp_* 节点（删配置/关注册的残留）。"""
    from gsuid_core.ai_core.agent_node import list_nodes, unregister_agent_node

    removed = 0
    for node in list_nodes():
        if node.source != "mcp" or node.node_id in alive_node_ids:
            continue
        unregister_agent_node(node.node_id)
        removed += 1
        logger.info(t("log.mcp.node_orphan_removed", node_id=node.node_id))
    return removed


async def unregister_mcp_server(config_id: str) -> int:
    """
    注销单个 MCP 服务器的所有已注册工具与能力节点，并同步向量库。

    Args:
        config_id: 配置 ID

    Returns:
        移除的工具数量
    """
    from gsuid_core.ai_core.agent_node import unregister_agent_node
    from gsuid_core.ai_core.mcp.node_profile import mcp_node_id

    removed = _forget_mcp_server(config_id)
    unregister_agent_node(mcp_node_id(config_id))
    await _sync_tool_index()
    return removed


async def register_single_mcp_server(config_id: str) -> tuple[int, str]:
    """
    注册单个 MCP 服务器的工具（用于 API 实时注册）。

    先注销该服务器的旧工具，再重新注册，然后同步向量库。

    Args:
        config_id: 配置 ID

    Returns:
        (注册的工具数量, 消息)
    """
    from gsuid_core.ai_core.agent_node import register_agent_node, unregister_agent_node
    from gsuid_core.ai_core.mcp.node_profile import mcp_node_id, build_mcp_node

    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return 0, f"配置 '{config_id}' 不存在"

    if not config.enabled:
        removed = await unregister_mcp_server(config_id)
        return 0, f"配置 '{config.name}' 已禁用，移除了 {removed} 个旧工具"

    _forget_mcp_server(config_id)

    if not config.register_as_ai_tools:
        # 不注册工具也不建节点；call_mcp_tool 业务调用不受影响。
        unregister_agent_node(mcp_node_id(config_id))
        await _sync_tool_index()
        return 0, f"配置 '{config.name}' 未开启 AI 工具注册"

    names: list[str] = []
    try:
        names = await _register_mcp_server(config_id, config)
    finally:
        await _sync_tool_index()

    if names:
        register_agent_node(build_mcp_node(config_id, config, names))
        await _warm_agent_node_index()
        from gsuid_core.ai_core.mcp.profile_generator import schedule_profile_generation

        schedule_profile_generation(config_id)
    else:
        unregister_agent_node(mcp_node_id(config_id))

    return len(names), f"注册完成，共 {len(names)} 个工具"


async def register_single_from_catalog(config_id: str) -> tuple[int, str]:
    """按已保存的工具清单注册单服务，不连接 MCP 服务器（导入/安装预览路径用）。"""
    from gsuid_core.ai_core.agent_node import register_agent_node, unregister_agent_node
    from gsuid_core.ai_core.mcp.node_profile import mcp_node_id, build_mcp_node

    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return 0, f"配置 '{config_id}' 不存在"
    if not config.enabled:
        return 0, f"配置 '{config.name}' 已禁用"

    _forget_mcp_server(config_id)
    if not config.register_as_ai_tools:
        unregister_agent_node(mcp_node_id(config_id))
        await _sync_tool_index()
        return 0, f"配置 '{config.name}' 未开启 AI 工具注册"

    names = _register_catalog(config_id, config)
    await _sync_tool_index()
    if names:
        register_agent_node(build_mcp_node(config_id, config, names))
        await _warm_agent_node_index()
        from gsuid_core.ai_core.mcp.profile_generator import schedule_profile_generation

        schedule_profile_generation(config_id)
    else:
        unregister_agent_node(mcp_node_id(config_id))
    return len(names), f"注册完成，共 {len(names)} 个工具"


async def register_all_mcp_tools() -> None:
    """启动时按已保存的工具清单注册。不连接 MCP 服务器。

    由 ai_core/startup.py 的 init_ai_core() 在框架启动后台阶段调用。
    清单为空的服务器跳过，等控制台单独注册时再连接。
    注册表稳定后同步一次工具向量库；顺带按清单生成/清理 mcp_* 能力节点。
    """
    from gsuid_core.ai_core.agent_node import register_agent_node
    from gsuid_core.ai_core.mcp.node_profile import mcp_node_id, build_mcp_node

    enabled_configs = mcp_config_manager.get_enabled_configs()
    alive_node_ids: set[str] = set()

    if not enabled_configs:
        logger.info(t("log.mcp.enabled_configurations_skipping"))
    else:
        logger.info(t("log.mcp.found_enabled_configurations", p0=len(enabled_configs)))

        total_registered = 0
        for config_id, config in enabled_configs:
            logger.info(t("log.mcp.registering_server_config", p0=config.name, config_id=config_id))
            if not config.register_as_ai_tools:
                logger.info(t("log.mcp.register_ai_tools_off_skip", p0=config.name, config_id=config_id))
                continue
            if not config.tools:
                logger.info(t("log.mcp.catalog_empty_defer_connect", p0=config.name, config_id=config_id))
                continue
            names = _register_catalog(config_id, config)
            total_registered += len(names)
            if names:
                register_agent_node(build_mcp_node(config_id, config, names))
                alive_node_ids.add(mcp_node_id(config_id))
            logger.info(t("log.mcp.registration_tools_total", p0=config.name, count=len(names)))

        logger.info(t("log.mcp.tools_registered_total", total_registered=total_registered))

    _cleanup_orphan_mcp_nodes(alive_node_ids)
    await _sync_tool_index()
    await _warm_agent_node_index()


async def shutdown_mcp_clients() -> None:
    """关闭时清理 MCP 客户端资源"""
    _mcp_clients.clear()
    logger.info(t("log.mcp.client_resources_cleaned"))


# 启动/关闭函数（由 ai_core/startup.py 的 init_ai_core() 统一调用）
async def init_mcp_tools():
    """框架启动时注册 MCP 工具（在 RAG 等基础模块初始化之后执行）"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.info(t("log.mcp.ai_master_switch_skipping_2"))
        return

    await register_all_mcp_tools()


@on_core_shutdown(priority=5)
async def _on_shutdown():
    """框架关闭时清理 MCP 资源"""
    await shutdown_mcp_clients()
