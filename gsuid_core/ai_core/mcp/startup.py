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
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.mcp.client import MCPClient
from gsuid_core.ai_core.mcp.config_manager import MCPConfig, mcp_config_manager

# _TOOL_REGISTRY 使用延迟导入以避免循环导入
# （register -> utils -> image_understand -> minimax_understand -> mcp -> startup -> register）


def _get_tool_registry() -> dict:
    """延迟导入 _TOOL_REGISTRY 以避免循环导入"""
    from gsuid_core.ai_core.register import _TOOL_REGISTRY

    return _TOOL_REGISTRY


# 存储已注册的 MCP 客户端实例，用于关闭时清理
_mcp_clients: Dict[str, MCPClient] = {}

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


def _make_mcp_client(config: MCPConfig) -> MCPClient:
    return MCPClient(
        name=config.name,
        transport=config.get_transport(),
        command=config.command,
        args=config.args,
        env=config.env,
        url=config.url,
        headers=config.headers,
    )


def _register_catalog(config_id: str, config: MCPConfig) -> int:
    """按已保存的工具清单注册。不连接服务器。"""
    client = _make_mcp_client(config)
    _mcp_clients[config_id] = client
    registered_count = 0
    for tool_def in config.tools:
        try:
            _register_mcp_tool(
                client,
                tool_def.name,
                tool_def.description,
                _input_schema_from_parameters(tool_def.parameters),
                config=config,
            )
            registered_count += 1
        except Exception as e:
            logger.error(t("log.mcp.fail_register_tool_failed", p0=config.name, p1=tool_def.name, e=e))
    return registered_count


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

    for param_name in properties:
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
        """MCP 工具包装函数，调用 MCP 服务器执行工具"""
        # 过滤掉 None 值的可选参数
        call_args = {k: v for k, v in kwargs.items() if v is not None}

        logger.info(
            t(
                "log.mcp.tool_calling_name_args_call",
                p0=client.name,
                tool_name=tool_name,
                call_args=call_args,
            )
        )

        try:
            result = await client.call_tool(tool_name, call_args)
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
) -> None:
    """
    将单个 MCP 工具注册到 _TOOL_REGISTRY。

    Args:
        client: MCP 客户端实例
        tool_name: MCP 工具名称
        tool_description: MCP 工具描述
        input_schema: MCP 工具的输入参数 JSON Schema
        config: MCP 服务器配置（用于权限检查）
    """
    # 使用 client.name 作为前缀避免工具名冲突
    registered_name = f"mcp_{client.name}_{tool_name}"

    # 检查是否已注册
    tool_registry = _get_tool_registry()
    if MCP_CATEGORY in tool_registry and registered_name in tool_registry[MCP_CATEGORY]:
        logger.debug(t("log.mcp.registered_skipping_name", registered_name=registered_name))
        return

    # 创建包装函数
    wrapper_func = _build_mcp_tool_function(client, tool_name, tool_description, input_schema)

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
    )

    if MCP_CATEGORY not in tool_registry:
        tool_registry[MCP_CATEGORY] = {}
    tool_registry[MCP_CATEGORY][registered_name] = tool_base

    logger.info(t("log.mcp.register_registered_name", registered_name=registered_name, p0=client.name))


async def _register_mcp_server(config_id: str, config: MCPConfig) -> int:
    """
    注册单个 MCP 服务器的所有工具。

    Args:
        config_id: 配置 ID
        config: MCP 配置

    Returns:
        成功注册的工具数量
    """
    client = _make_mcp_client(config)

    try:
        tools = await client.list_tools()
    except Exception as e:
        logger.error(t("log.mcp.connect_server", p0=config.name, e=e))
        return 0

    # 保存客户端引用
    _mcp_clients[config_id] = client

    registered_count = 0
    for tool_info in tools:
        try:
            _register_mcp_tool(
                client,
                tool_info.name,
                tool_info.description,
                _input_schema_from_json_schema(tool_info.input_schema),
                config=config,
            )
            registered_count += 1
        except Exception as e:
            logger.error(t("log.mcp.fail_register_tool_failed", p0=config.name, p1=tool_info.name, e=e))

    return registered_count


async def unregister_mcp_server(config_id: str) -> int:
    """
    注销单个 MCP 服务器的所有已注册工具。

    Args:
        config_id: 配置 ID

    Returns:
        移除的工具数量
    """
    tool_registry = _get_tool_registry()
    if MCP_CATEGORY not in tool_registry:
        return 0

    # 找到该服务器注册的所有工具（通过 plugin 前缀匹配）
    config = mcp_config_manager.get_config(config_id)
    if config is None:
        # 配置已删除，尝试从 _mcp_clients 获取名称
        client = _mcp_clients.get(config_id)
        server_name = client.name if client else config_id
    else:
        server_name = config.name

    plugin_prefix = f"mcp_{server_name}_"
    tools_to_remove = [name for name in tool_registry[MCP_CATEGORY] if name.startswith(plugin_prefix)]

    for tool_name in tools_to_remove:
        del tool_registry[MCP_CATEGORY][tool_name]
        logger.info(t("log.mcp.unregister_name_tool", tool_name=tool_name))

    # 清理客户端引用
    if config_id in _mcp_clients:
        del _mcp_clients[config_id]

    return len(tools_to_remove)


async def register_single_mcp_server(config_id: str) -> tuple[int, str]:
    """
    注册单个 MCP 服务器的工具（用于 API 实时注册）。

    先注销该服务器的旧工具，再重新注册。

    Args:
        config_id: 配置 ID

    Returns:
        (注册的工具数量, 消息)
    """
    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return 0, f"配置 '{config_id}' 不存在"

    if not config.enabled:
        # 如果配置被禁用，只注销旧工具
        removed = await unregister_mcp_server(config_id)
        return 0, f"配置 '{config.name}' 已禁用，移除了 {removed} 个旧工具"

    # 先注销旧工具
    await unregister_mcp_server(config_id)

    # 重新注册
    count = await _register_mcp_server(config_id, config)
    return count, f"注册完成，共 {count} 个工具"


async def register_all_mcp_tools() -> None:
    """启动时按已保存的工具清单注册。不连接 MCP 服务器。

    由 ai_core/startup.py 的 init_ai_core() 在框架启动后台阶段调用。
    清单为空的服务器跳过，等控制台单独注册时再连接。
    """
    enabled_configs = mcp_config_manager.get_enabled_configs()

    if not enabled_configs:
        logger.info(t("log.mcp.enabled_configurations_skipping"))
        return

    logger.info(t("log.mcp.found_enabled_configurations", p0=len(enabled_configs)))

    total_registered = 0
    for config_id, config in enabled_configs:
        logger.info(t("log.mcp.registering_server_config", p0=config.name, config_id=config_id))
        if not config.tools:
            logger.info(t("log.mcp.catalog_empty_defer_connect", p0=config.name, config_id=config_id))
            continue
        count = _register_catalog(config_id, config)
        total_registered += count
        logger.info(t("log.mcp.registration_tools_total", p0=config.name, count=count))

    logger.info(t("log.mcp.tools_registered_total", total_registered=total_registered))


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
