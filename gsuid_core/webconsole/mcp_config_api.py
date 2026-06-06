"""
MCP Config APIs

提供 MCP 服务器配置管理的 RESTful APIs，包括增删改查、启用/禁用、热重载等。
用户可以通过这些 API 自由添加和管理 MCP 服务器配置。
所有增删改和 toggle 操作会自动触发实时工具注册/注销，无需重启服务。
"""

from typing import Any, Dict, List, Optional

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.mcp.startup import (
    unregister_mcp_server,
    register_all_mcp_tools,
    register_single_mcp_server,
)
from gsuid_core.ai_core.mcp.mcp_presets import MCP_PRESETS
from gsuid_core.ai_core.mcp.config_manager import MCPConfig, MCPToolDefinition, mcp_config_manager
from gsuid_core.utils.plugins_config.models import GsStrConfig


class MCPToolDefinitionModel(BaseModel):
    """MCP 工具定义模型"""

    name: str
    description: str = ""
    parameters: Dict[str, Any] = {}
    input_schema: Optional[Dict[str, Any]] = None
    """MCP 工具的 JSON Schema 输入定义（discover 返回的原始格式）

    前端可直接传入 discover API 返回的 input_schema，后端会自动提取
    properties 并转换为 parameters 格式存储。
    """


def _convert_input_schema_to_parameters(
    input_schema: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """将 MCP 工具的 JSON Schema (input_schema) 转换为 parameters 格式。

    discover API 返回的 input_schema 是标准 JSON Schema 格式::

        {
            "properties": {
                "query": {"title": "Query", "type": "string"},
                "image_source": {"title": "Image Source", "type": "string"},
            },
            "required": ["query", "image_source"],
            "type": "object",
        }

    转换后的 parameters 是扁平字典格式::

        {
            "query": {"type": "string", "description": "Query"},
            "image_source": {"type": "string", "description": "Image Source"},
        }

    Args:
        input_schema: MCP 工具的 JSON Schema 输入定义

    Returns:
        扁平化的参数字典
    """
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])

    parameters: Dict[str, Dict[str, Any]] = {}
    for param_name, param_schema in properties.items():
        param_def: Dict[str, Any] = {}
        # 提取类型
        if "type" in param_schema:
            param_def["type"] = param_schema["type"]
        # 提取描述（优先用 description，其次用 title）
        desc = param_schema.get("description", "") or param_schema.get("title", "")
        if desc:
            param_def["description"] = desc
        # 标记是否必填
        if param_name in required:
            param_def["required"] = True
        # 保留其他字段（enum, default 等）
        for extra_key in ("enum", "default", "anyOf", "allOf", "oneOf", "items"):
            if extra_key in param_schema:
                param_def[extra_key] = param_schema[extra_key]

        parameters[param_name] = param_def

    return parameters


class MCPConfigCreate(BaseModel):
    """MCP 配置创建请求模型"""

    name: str
    transport: str = "stdio"  # "stdio" 或 "sse"
    command: str = ""
    args: List[str] = []
    env: Dict[str, str] = {}
    url: str = ""  # SSE 服务器 URL
    headers: Dict[str, str] = {}  # SSE HTTP 请求头
    enabled: bool = True
    register_as_ai_tools: bool = False
    tools: List[MCPToolDefinitionModel] = []
    tool_permissions: Dict[str, int] = {}


class MCPConfigUpdate(BaseModel):
    """MCP 配置更新请求模型"""

    name: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None
    register_as_ai_tools: Optional[bool] = None
    tools: Optional[List[MCPToolDefinitionModel]] = None
    tool_permissions: Optional[Dict[str, int]] = None


@app.get("/api/ai/mcp/list")
async def get_mcp_configs_list(
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取所有 MCP 配置列表

    Returns:
        status: 0成功
        data: MCP 配置列表
    """
    configs = mcp_config_manager.list_configs()
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "configs": configs,
            "count": len(configs),
        },
    }


@app.get("/api/ai/mcp/presets")
async def get_mcp_presets(
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 MCP 预设配置列表

    返回常用的 MCP 服务提供商预设配置，用户可以快速添加。
    预设包含默认的 command、args，但不包含实际的环境变量值。

    Returns:
        status: 0成功
        data: 预设列表
    """
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "presets": MCP_PRESETS,
            "count": len(MCP_PRESETS),
        },
    }


# 静态路由名称（与动态路由 /{config_id} 冲突）
MCP_STATIC_ROUTES = frozenset({"list", "presets", "discover", "import", "reload"})


@app.get("/api/ai/mcp/{config_id}")
async def get_mcp_config_detail(
    config_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定 MCP 配置的详细信息

    Args:
        config_id: 配置 ID

    Returns:
        status: 0成功，1失败
        data: 配置详情
    """
    # 排除静态路由
    if config_id in MCP_STATIC_ROUTES:
        return {
            "status": 1,
            "msg": f"无效的配置 ID '{config_id}'，这是一个保留路由",
            "data": None,
        }

    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return {
            "status": 1,
            "msg": f"MCP 配置 '{config_id}' 不存在",
            "data": None,
        }

    data = config.to_dict()
    data["config_id"] = config_id
    return {
        "status": 0,
        "msg": "ok",
        "data": data,
    }


@app.post("/api/ai/mcp")
async def create_mcp_config(
    body: MCPConfigCreate,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    创建新的 MCP 配置

    Args:
        body: MCP 配置信息

    Returns:
        status: 0成功，1失败
        data: 创建结果
    """
    import re

    # 从 name 生成 config_id（支持 Unicode 字符如中文，仅替换文件系统不安全字符）
    config_id = re.sub(r"[^\w-]", "_", body.name, flags=re.UNICODE).strip("_").lower()
    if not config_id:
        return {
            "status": 1,
            "msg": "无效的配置名称，无法生成 config_id",
            "data": None,
        }

    tools = []
    for t in body.tools:
        # 优先使用 input_schema 自动转换为 parameters
        if t.input_schema and not t.parameters:
            parameters = _convert_input_schema_to_parameters(t.input_schema)
        else:
            parameters = t.parameters

        tools.append(
            MCPToolDefinition(
                name=t.name,
                description=t.description,
                parameters=parameters,
            )
        )

    config = MCPConfig(
        name=body.name,
        transport=body.transport,
        command=body.command,
        args=body.args,
        env=body.env,
        url=body.url,
        headers=body.headers,
        enabled=body.enabled,
        register_as_ai_tools=body.register_as_ai_tools,
        tools=tools,
        tool_permissions=body.tool_permissions,
    )

    success, msg = mcp_config_manager.create_config(config_id, config)
    if not success:
        return {
            "status": 1,
            "msg": msg,
            "data": None,
        }

    # 实时注册 MCP 工具
    tool_count = 0
    register_msg = ""
    if config.enabled:
        tool_count, register_msg = await register_single_mcp_server(config_id)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "config_id": config_id,
            "name": body.name,
            "tool_count": tool_count,
            "register_msg": register_msg,
        },
    }


@app.put("/api/ai/mcp/{config_id}")
async def update_mcp_config(
    config_id: str,
    body: MCPConfigUpdate,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新 MCP 配置

    Args:
        config_id: 配置 ID
        body: 要更新的字段

    Returns:
        status: 0成功，1失败
        data: 更新结果
    """
    # 过滤掉 None 字段
    updates = {k: v for k, v in body.model_dump().items() if v is not None}

    if not updates:
        return {
            "status": 1,
            "msg": "没有提供要更新的字段",
            "data": None,
        }

    success, msg = mcp_config_manager.update_config(config_id, updates)
    if not success:
        return {
            "status": 1,
            "msg": msg,
            "data": None,
        }

    # 实时重新注册 MCP 工具（配置变更后重新连接服务器）
    tool_count, register_msg = await register_single_mcp_server(config_id)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "config_id": config_id,
            "tool_count": tool_count,
            "register_msg": register_msg,
        },
    }


@app.delete("/api/ai/mcp/{config_id}")
async def delete_mcp_config(
    config_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除 MCP 配置

    Args:
        config_id: 配置 ID

    Returns:
        status: 0成功，1失败
        data: 删除结果
    """
    # 先注销已注册的 MCP 工具
    removed_count = await unregister_mcp_server(config_id)

    success, msg = mcp_config_manager.delete_config(config_id)
    if not success:
        return {
            "status": 1,
            "msg": msg,
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "config_id": config_id,
            "removed_tool_count": removed_count,
        },
    }


@app.post("/api/ai/mcp/{config_id}/toggle")
async def toggle_mcp_config(
    config_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    切换 MCP 配置的启用/禁用状态

    Args:
        config_id: 配置 ID

    Returns:
        status: 0成功，1失败
        data: 切换后的状态
    """
    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return {
            "status": 1,
            "msg": f"MCP 配置 '{config_id}' 不存在",
            "data": None,
        }

    new_enabled = not config.enabled
    success, msg = mcp_config_manager.update_config(config_id, {"enabled": new_enabled})
    if not success:
        return {
            "status": 1,
            "msg": msg,
            "data": None,
        }

    # 实时注册或注销 MCP 工具
    if new_enabled:
        tool_count, register_msg = await register_single_mcp_server(config_id)
    else:
        removed_count = await unregister_mcp_server(config_id)
        tool_count = 0
        register_msg = f"已禁用，移除了 {removed_count} 个工具"

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "config_id": config_id,
            "enabled": new_enabled,
            "tool_count": tool_count,
            "register_msg": register_msg,
        },
    }


@app.post("/api/ai/mcp/reload")
async def reload_mcp_configs(
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    热重载所有 MCP 配置并重新注册工具

    重新加载配置文件，并重新连接所有启用的 MCP 服务器注册工具。
    此操作会清除已注册的 MCP 工具并重新注册。

    Returns:
        status: 0成功
        data: 重载结果
    """
    from gsuid_core.ai_core.register import _TOOL_REGISTRY
    from gsuid_core.ai_core.mcp.startup import MCP_CATEGORY

    # 清除已注册的 MCP 工具
    if MCP_CATEGORY in _TOOL_REGISTRY:
        old_count = len(_TOOL_REGISTRY[MCP_CATEGORY])
        _TOOL_REGISTRY[MCP_CATEGORY].clear()
    else:
        old_count = 0

    # 重新加载配置
    mcp_config_manager.reload()

    # 重新注册工具
    await register_all_mcp_tools()

    new_count = len(_TOOL_REGISTRY.get(MCP_CATEGORY, {}))

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "old_tool_count": old_count,
            "new_tool_count": new_count,
            "config_count": len(mcp_config_manager.list_configs()),
        },
    }


@app.get("/api/ai/mcp/{config_id}/tools")
async def discover_mcp_tools(
    config_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    从已配置的 MCP 服务器发现可用工具列表

    连接 MCP 服务器并列出其提供的所有工具，包括工具名称、描述和参数定义。
    发现的工具可以用于更新配置中的 tools 列表。

    Args:
        config_id: MCP 配置 ID

    Returns:
        status: 0成功，1失败
        data: 工具列表
    """
    from gsuid_core.ai_core.mcp import MCPClient

    config = mcp_config_manager.get_config(config_id)
    if not config:
        return {
            "status": 1,
            "msg": f"MCP 配置 '{config_id}' 不存在",
            "data": None,
        }

    try:
        client = MCPClient(
            name=config.name,
            command=config.command,
            args=config.args,
            env=config.env,
            url=config.url,
            headers=config.headers,
        )
        tools = await client.list_tools()

        # 转换为前端需要的格式
        tool_list = []
        for tool in tools:
            # 同时返回原始 input_schema 和转换后的 parameters
            converted_params = _convert_input_schema_to_parameters(tool.input_schema)
            tool_list.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "parameters": converted_params,
                }
            )

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "config_id": config_id,
                "tools": tool_list,
                "count": len(tool_list),
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"连接 MCP 服务器失败: {e}",
            "data": None,
        }


class MCPDiscoverRequest(BaseModel):
    """MCP 临时配置（仅用于发现工具，不保存）"""

    name: str
    transport: str = "stdio"  # "stdio" 或 "sse"
    command: str = ""
    args: List[str] = []
    env: Dict[str, str] = {}
    url: str = ""  # SSE 服务器 URL
    headers: Dict[str, str] = {}  # SSE HTTP 请求头


@app.post("/api/ai/mcp/tools/discover")
async def discover_tools_from_temp_config(
    body: MCPDiscoverRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    从临时 MCP 配置发现可用工具（不保存配置）

    用户输入 MCP 服务器配置后，先连接服务器发现其提供的工具，
    确认后再决定是否保存配置。

    Args:
        body: 临时 MCP 配置

    Returns:
        status: 0成功，1失败
        data: 工具列表
    """
    from gsuid_core.ai_core.mcp import MCPClient

    try:
        client = MCPClient(
            name=body.name,
            command=body.command,
            args=body.args,
            env=body.env,
            url=body.url,
            headers=body.headers,
        )
        tools = await client.list_tools()

        tool_list = []
        for tool in tools:
            tool_list.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
            )

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "tools": tool_list,
                "count": len(tool_list),
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"连接 MCP 服务器失败: {e}",
            "data": None,
        }


class MCPImportRequest(BaseModel):
    """MCP 导入请求模型"""

    json_config: str  # 粘贴的 JSON 配置


@app.post("/api/ai/mcp/tools/import")
async def import_mcp_from_json(
    body: MCPImportRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    从 JSON 配置导入 MCP 服务器

    支持粘贴 MCP 官方格式的 JSON 配置（如 MiniMax MCP 的配置），
    自动解析并创建配置。

    Args:
        body: JSON 配置字符串

    Returns:
        status: 0成功，1失败
        data: 导入结果
    """
    import re
    import json

    try:
        config_data = json.loads(body.json_config)
    except json.JSONDecodeError:
        return {
            "status": 1,
            "msg": "无效的 JSON 格式",
            "data": None,
        }

    # 解析 MCP 官方格式: { "mcpServers": { "Name": { ... } } }
    if "mcpServers" in config_data:
        servers = config_data["mcpServers"]
        if not servers:
            return {
                "status": 1,
                "msg": "mcpServers 为空",
                "data": None,
            }

        # 只处理第一个服务器
        server_name, server_config = next(iter(servers.items()))

        # 生成 config_id
        config_id = re.sub(r"[^a-zA-Z0-9_-]", "_", server_name.lower()).strip("_")
        if not config_id:
            config_id = "mcp_server"

        # 检查是否已存在
        if mcp_config_manager.get_config(config_id):
            return {
                "status": 1,
                "msg": f"配置 '{config_id}' 已存在，请先删除或重命名",
                "data": None,
            }

        # 构建 MCPConfig（兼容 stdio 和 sse 两种格式）
        env = server_config.get("env", {})
        args = server_config.get("args", [])
        url = server_config.get("url", server_config.get("sseUrl", ""))
        headers = server_config.get("headers", server_config.get("headersTemplate", {}))
        transport = server_config.get("transport", "")

        # 自动推断 transport：如果 url 存在且以 http 开头，则为 sse
        if not transport or transport == "auto":
            if url and isinstance(url, str) and url.startswith("http"):
                transport = "sse"
            else:
                transport = "stdio"

        # 如果没有 tools，先连接服务器发现工具
        tools = []
        try:
            from gsuid_core.ai_core.mcp import MCPClient

            client = MCPClient(
                name=server_name,
                command=server_config.get("command", ""),
                args=args,
                env=env,
                url=url,
                headers=headers,
            )
            raw_tools = await client.list_tools()
            tools = [
                MCPToolDefinition(
                    name=t.name,
                    description=t.description,
                    parameters=_convert_input_schema_to_parameters(t.input_schema) if t.input_schema else {},
                )
                for t in raw_tools
            ]
        except Exception:
            # 发现工具失败，继续创建配置但不带 tools
            pass

        mcp_config = MCPConfig(
            name=server_name,
            transport=transport,
            command=server_config.get("command", ""),
            args=args,
            env=env,
            url=url,
            headers=headers,
            enabled=True,
            register_as_ai_tools=False,
            tools=tools,
        )

        success, msg = mcp_config_manager.create_config(config_id, mcp_config)
        if not success:
            return {
                "status": 1,
                "msg": f"创建配置失败: {msg}",
                "data": None,
            }

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "config_id": config_id,
                "name": server_name,
                "tools_count": len(tools),
                "tool_names": [t.name for t in tools],
            },
        }

    return {
        "status": 1,
        "msg": "不支持的 JSON 格式，请确保包含 mcpServers 字段",
        "data": None,
    }


# ---------------------------------------------------------------------------
# MCP 工具配置 (mcp_tools_config) — details 参数映射 API
# ---------------------------------------------------------------------------


@app.get("/api/ai/mcp-tools-config/list")
async def get_mcp_tools_config_list(
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 MCP 工具配置列表

    返回所有 mcp_tools_config 中各业务模块对应的 MCP 工具 ID 和 details 参数映射。

    Returns:
        status: 0成功
        data: 配置列表
    """
    from gsuid_core.ai_core.mcp.mcp_tools_config import mcp_tools_config

    items = []
    for key, config_item in mcp_tools_config.config.items():
        item_data = {
            "key": key,
            "title": config_item.title,
            "desc": config_item.desc,
            "data": config_item.data,
            # 仅 GsStrConfig 带 details；用 isinstance 守卫替代 getattr 兜底
            "details": config_item.details if isinstance(config_item, GsStrConfig) else None,
        }
        items.append(item_data)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "items": items,
            "count": len(items),
        },
    }


@app.get("/api/ai/mcp-tools-config/{item_key}")
async def get_mcp_tools_config_detail(
    item_key: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定 MCP 工具配置项的详情（含 details 参数映射）

    Args:
        item_key: 配置项键名，如 websearch_mcp_tool_id

    Returns:
        status: 0成功，1失败
        data: 配置项详情
    """
    from gsuid_core.ai_core.mcp.mcp_tools_config import mcp_tools_config

    config_item = mcp_tools_config.get_config(item_key)
    if not config_item:
        return {
            "status": 1,
            "msg": f"配置项 '{item_key}' 不存在",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "key": item_key,
            "title": config_item.title,
            "desc": config_item.desc,
            "data": config_item.data,
            "details": config_item.details if isinstance(config_item, GsStrConfig) else None,
        },
    }


class MCPToolDetailsUpdate(BaseModel):
    """MCP 工具 details 参数映射更新模型"""

    data: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


@app.put("/api/ai/mcp-tools-config/{item_key}")
async def update_mcp_tools_config(
    item_key: str,
    body: MCPToolDetailsUpdate,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新指定 MCP 工具配置项（含 details 参数映射）

    可同时更新工具 ID (data) 和 details 参数映射。

    **details 格式说明**:

    details 字典的 **键** 为 MCP 工具的参数名，**值** 为映射来源:
    - `"params - <内部参数名>"` → 从内部函数的参数中取值
    - 字面量 (字符串 / 数字 / 布尔) → 固定值，每次调用都传入

    示例:
    ```json
    {
        "data": "minimax - web_search",
        "details": {
            "query": "params - query",
            "max_results": "params - max_results",
            "search_model": "custom",
            "max": 6
        }
    }
    ```

    Args:
        item_key: 配置项键名
        body: 更新内容

    Returns:
        status: 0成功，1失败
    """
    from gsuid_core.ai_core.mcp.mcp_tools_config import mcp_tools_config

    config_item = mcp_tools_config.get_config(item_key)
    # data / details 均为 GsStrConfig 字段；非 GsStrConfig 不支持本接口
    if not isinstance(config_item, GsStrConfig):
        return {
            "status": 1,
            "msg": f"配置项 '{item_key}' 不存在或不支持 details 映射",
            "data": None,
        }

    updated_fields = []

    # 更新 data (MCP 工具 ID)
    if body.data is not None:
        config_item.data = body.data
        updated_fields.append("data")

    # 更新 details (参数映射)
    if body.details is not None:
        config_item.details = body.details
        updated_fields.append("details")

    if not updated_fields:
        return {
            "status": 1,
            "msg": "没有提供要更新的字段",
            "data": None,
        }

    # 写回配置文件
    mcp_tools_config.write_config()

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "key": item_key,
            "updated_fields": updated_fields,
            "data": config_item.data,
            "details": config_item.details,
        },
    }
