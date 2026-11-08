"""
MCP Config APIs

提供 MCP 服务器配置管理的 RESTful APIs，包括增删改查、启用/禁用、热重载等。
用户可以通过这些 API 自由添加和管理 MCP 服务器配置。
所有增删改和 toggle 操作会自动触发实时工具注册/注销，无需重启服务。
"""

from typing import Any, Dict, List, Optional

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.secret_mask import unmask_against
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin, require_admin_header
from gsuid_core.ai_core.mcp.startup import (
    unregister_mcp_server,
    register_all_mcp_tools,
    register_single_mcp_server,
    register_single_from_catalog,
)
from gsuid_core.ai_core.mcp.transport import MCP_TRANSPORT_STDIO, resolve_mcp_transport
from gsuid_core.ai_core.mcp.mcp_presets import MCP_PRESETS
from gsuid_core.ai_core.mcp.config_manager import (
    MCPConfig,
    MCPToolDefinition,
    parse_market_meta,
    mcp_config_manager,
    parse_mcp_json_server,
)
from gsuid_core.utils.plugins_config.models import GsStrConfig

from ._api_tags import MCP_CONFIG


class MCPToolDefinitionModel(BaseModel):
    """MCP 工具定义模型"""

    name: str
    description: str = ""
    parameters: Dict[str, Any] = {}
    exposure: Optional[str] = None
    mutating: Optional[bool] = None
    sensitive_args: Optional[bool] = None
    credential_params: Optional[List[str]] = None
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
    transport: str = MCP_TRANSPORT_STDIO
    command: str = ""
    args: List[str] = []
    env: Dict[str, str] = {}
    url: str = ""
    headers: Dict[str, str] = {}
    enabled: bool = True
    register_as_ai_tools: bool = True
    tools: List[MCPToolDefinitionModel] = []
    tool_permissions: Dict[str, int] = {}
    description: str = ""
    market_meta: Optional[Dict[str, Any]] = None
    user_env_keys: Optional[List[str]] = None
    public_ack: bool = False
    allow_autonomous_private: bool = False
    bind_help: str = ""
    bind_url: str = ""
    bind_alias: Optional[List[str]] = None
    cwd: str = ""


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
    description: Optional[str] = None
    market_meta: Optional[Dict[str, Any]] = None
    user_env_keys: Optional[List[str]] = None
    public_ack: Optional[bool] = None
    allow_autonomous_private: Optional[bool] = None
    bind_help: Optional[str] = None
    bind_url: Optional[str] = None
    bind_alias: Optional[List[str]] = None
    cwd: Optional[str] = None


@app.get("/api/ai/mcp/list", summary="获取 MCP 配置列表", tags=MCP_CONFIG)
async def get_mcp_configs_list(
    _: Dict[str, Any] = Depends(require_admin_header),
) -> Dict[str, Any]:
    """
    获取所有 MCP 配置列表

    Returns:
        status: 0成功
        data: MCP 配置列表
    """
    configs = [_config_response_dict(config_id, config) for config_id, config in mcp_config_manager.list_config_items()]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "configs": configs,
            "count": len(configs),
        },
    }


@app.get("/api/ai/mcp/presets", summary="获取 MCP 预设配置", tags=MCP_CONFIG)
async def get_mcp_presets(
    _: Dict[str, Any] = Depends(require_auth),
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
MCP_STATIC_ROUTES = frozenset({"list", "presets", "discover", "import", "reload", "market", "tools", "bind-candidates"})


def _config_response_dict(config_id: str, config: MCPConfig) -> Dict[str, Any]:
    """配置响应字典：附 profile_status 与本地 logo 代理 URL，并预取 logo。"""
    from gsuid_core.webconsole.mcp_market_api import (
        logo_proxy_url,
        ensure_logo_cached,
        check_stdio_runtime,
    )
    from gsuid_core.ai_core.mcp.config_manager import suggest_shared_env_keys
    from gsuid_core.ai_core.mcp.profile_generator import get_profile_status

    data = config.to_dict()
    data["config_id"] = config_id
    data["profile_status"] = get_profile_status(config_id)
    # 「建议共享」UI 徽章：疑似配置值的 env key 打标，帮管理员取消绑定勾选
    data["suggest_shared_env_keys"] = suggest_shared_env_keys(config.env)
    if config.get_transport() == MCP_TRANSPORT_STDIO:
        data["runtime_check"] = check_stdio_runtime(config.command)
    logo = config.market_meta.get("logo_url", "")
    if logo:
        data["logo_url"] = logo_proxy_url(logo)
        ensure_logo_cached(logo)
    return data


# 注意：必须注册在 /{config_id} 之前，否则 bind-candidates 会被当成配置 ID 截获
@app.get("/api/ai/mcp/bind-candidates", summary="获取可自动绑定的主人候选", tags=MCP_CONFIG)
async def get_bind_candidates(
    _: Dict[str, Any] = Depends(require_admin_header),
) -> Dict[str, Any]:
    """列出 Subscribe 表「主人用户」全部行（多主人/多平台各一行，不去重）。

    每行 = 真实 master × bot 平台对，附 CoreUser 昵称。管理员勾选后
    POST ``/{config_id}/bind`` 用共享 token 为这些身份落 ``admin_install`` 绑定行。
    """
    from sqlmodel import col, select

    from gsuid_core.utils.database.models import Subscribe
    from gsuid_core.utils.database.base_models import AsyncSession, with_session

    @with_session
    async def _load(_: Any, session: AsyncSession) -> List[Dict[str, Any]]:
        stmt = select(Subscribe).where(col(Subscribe.task_name) == "主人用户")
        result = await session.execute(stmt)
        rows: List[Dict[str, Any]] = []
        for sub in result.scalars().all():
            if not isinstance(sub, Subscribe) or not sub.user_id or not sub.bot_id:
                continue
            rows.append(
                {
                    "bot_id": sub.bot_id,
                    "user_id": sub.user_id,
                    "bot_self_id": sub.bot_self_id or "",
                    "user_name": "",
                }
            )
        return rows

    candidates = await _load(None)

    # 补昵称（best-effort：CoreUser 表按 user_id 查 name）
    try:
        from gsuid_core.utils.database.models import CoreUser

        user_ids = [c["user_id"] for c in candidates]
        if user_ids:
            names = await CoreUser.get_names_by_ids(user_ids)
            for c in candidates:
                if isinstance(names, dict):
                    c["user_name"] = names.get(c["user_id"], "")
    except Exception:
        pass

    return {"status": 0, "msg": "ok", "data": {"candidates": candidates, "count": len(candidates)}}


@app.get("/api/ai/mcp/{config_id}", summary="获取 MCP 配置详情", tags=MCP_CONFIG)
async def get_mcp_config_detail(
    config_id: str,
    _: Dict[str, Any] = Depends(require_admin_header),
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

    return {
        "status": 0,
        "msg": "ok",
        "data": _config_response_dict(config_id, config),
    }


@app.post("/api/ai/mcp", summary="创建 MCP 配置", tags=MCP_CONFIG)
async def create_mcp_config(
    body: MCPConfigCreate,
    _: Dict[str, Any] = Depends(require_admin),
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
    if mcp_config_manager.name_in_use(body.name):
        return {
            "status": 1,
            "msg": f"已存在同名启用的 MCP 服务 '{body.name}'，请改名",
            "data": None,
        }

    resolved_transport = resolve_mcp_transport(
        body.transport,
        url=body.url,
        command=body.command,
    )
    market_meta = parse_market_meta(body.market_meta)
    args = list(body.args)
    # 源码检出型：先自动克隆源码到工作目录再建配置，失败则整单拒绝（装了也跑不起来）
    if resolved_transport == MCP_TRANSPORT_STDIO and market_meta.get("requires_checkout"):
        from gsuid_core.ai_core.mcp.client import (
            rewrite_checkout_args,
            repair_checkout_script,
            ensure_mcp_source_checkout,
        )

        source_url = market_meta.get("source_url", "")
        if not source_url:
            return {
                "status": 1,
                "msg": "该安装方式需要克隆源码仓库运行，但上游未提供仓库地址，无法自动安装",
                "data": None,
            }
        workdir, checkout_err = await ensure_mcp_source_checkout(body.name, source_url, body.cwd)
        if checkout_err:
            return {
                "status": 1,
                "msg": f"源码自动克隆失败：{checkout_err}",
                "data": None,
            }
        repair_note = repair_checkout_script(args, workdir)
        if repair_note:
            logger.info(t("log.mcp.checkout_repaired", p0=body.name, p1=repair_note))
        args = rewrite_checkout_args(args, workdir)

    tools = []
    for tool in body.tools:
        # 优先使用 input_schema 自动转换为 parameters
        if tool.input_schema and not tool.parameters:
            parameters = _convert_input_schema_to_parameters(tool.input_schema)
        else:
            parameters = tool.parameters

        # exposure 缺省规则：有可绑定凭据（env/headers）→ private；否则 public
        default_exposure = "private" if (body.env or body.headers) else "public"
        tools.append(
            MCPToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=parameters,
                exposure=tool.exposure if tool.exposure in ("private", "public") else default_exposure,
                mutating=bool(tool.mutating),
                sensitive_args=bool(tool.sensitive_args),
                credential_params=[p for p in (tool.credential_params or []) if isinstance(p, str)],
            )
        )

    # user_env_keys 缺省 = 全部 env key（fail-closed：宁可多问用户几个值）
    user_env_keys = body.user_env_keys if body.user_env_keys is not None else list(body.env)
    config = MCPConfig(
        name=body.name,
        transport=resolved_transport,
        command=body.command,
        args=args,
        env=body.env,
        url=body.url,
        headers=body.headers,
        enabled=body.enabled,
        register_as_ai_tools=body.register_as_ai_tools,
        tools=tools,
        tool_permissions=body.tool_permissions,
        description=body.description,
        market_meta=market_meta,
        user_env_keys=user_env_keys,
        public_ack=body.public_ack,
        allow_autonomous_private=body.allow_autonomous_private,
        bind_help=body.bind_help,
        bind_url=body.bind_url,
        bind_alias=[a for a in (body.bind_alias or []) if isinstance(a, str)],
        cwd=body.cwd,
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


@app.put("/api/ai/mcp/{config_id}", summary="更新 MCP 配置", tags=MCP_CONFIG)
async def update_mcp_config(
    config_id: str,
    body: MCPConfigUpdate,
    _: Dict[str, Any] = Depends(require_admin),
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
    existing = mcp_config_manager.get_config(config_id)
    if existing is not None:
        updates = unmask_against(updates, existing.to_dict())

    if not updates:
        return {
            "status": 1,
            "msg": "没有提供要更新的字段",
            "data": None,
        }

    new_name = updates.get("name")
    if isinstance(new_name, str) and mcp_config_manager.name_in_use(new_name, exclude_config_id=config_id):
        return {
            "status": 1,
            "msg": f"已存在同名启用的 MCP 服务 '{new_name}'，请改名",
            "data": None,
        }

    # tools 提交时转成 MCPToolDefinition 可反序列化 dict（含分级字段）
    raw_tools = updates.get("tools")
    if isinstance(raw_tools, list) and existing is not None:
        existing_flags = {t.name: t for t in existing.tools}
        new_tools: List[Dict[str, Any]] = []
        for item in raw_tools:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "")
            old = existing_flags.get(name)
            params = item.get("parameters") or {}
            if not params and isinstance(item.get("input_schema"), dict):
                params = _convert_input_schema_to_parameters(item["input_schema"])
            exposure = item.get("exposure")
            if exposure not in ("private", "public"):
                exposure = old.exposure if old is not None else "private"
            # private→public 首次拨档必须 public_ack=True，否则拒绝整个更新
            if old is not None and old.exposure == "private" and exposure == "public":
                acked = bool(updates.get("public_ack")) or existing.public_ack
                if not acked:
                    return {
                        "status": 1,
                        "msg": (
                            f"工具 '{name}' 首次从私有改为公有需要确认风险提示"
                            "（共享凭据/账号态输出/隐私配额/服务条款），请勾选确认后重试"
                        ),
                        "data": None,
                    }
                updates["public_ack"] = True
            new_tools.append(
                {
                    "name": name,
                    "description": item.get("description", ""),
                    "parameters": params,
                    "exposure": exposure,
                    "mutating": bool(item.get("mutating", old.mutating if old is not None else False)),
                    "sensitive_args": bool(
                        item.get("sensitive_args", old.sensitive_args if old is not None else False)
                    ),
                    "credential_params": [
                        p
                        for p in item.get(
                            "credential_params",
                            old.credential_params if old is not None else [],
                        )
                        if isinstance(p, str)
                    ],
                }
            )
        updates["tools"] = new_tools

    success, msg = mcp_config_manager.update_config(config_id, updates)
    if not success:
        return {
            "status": 1,
            "msg": msg,
            "data": None,
        }

    # env/headers 轮换 → 重同步 admin_install 行 + 清全部 scope 缓存
    if existing is not None and ("env" in updates or "headers" in updates):
        from gsuid_core.ai_core.mcp.credential_store import (
            MCPUserCredential,
        )

        new_config = mcp_config_manager.get_config(config_id)
        if new_config is not None:
            rows = await MCPUserCredential.list_rows_for_config(config_id)
            shared_creds = {k: v for k, v in new_config.env.items() if k in new_config.user_env_keys}
            for row in rows:
                if row.bound_via == "admin_install":
                    await MCPUserCredential.upsert_row(
                        config_id,
                        row.bot_id,
                        row.user_id,
                        shared_creds,
                        bound_via="admin_install",
                        bound_at=row.bound_at,
                        last_ok_at=row.last_ok_at,
                    )
        from gsuid_core.ai_core.mcp.startup import _drop_config_clients

        _drop_config_clients(config_id)

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


@app.delete("/api/ai/mcp/{config_id}", summary="删除 MCP 配置", tags=MCP_CONFIG)
async def delete_mcp_config(
    config_id: str,
    _: Dict[str, Any] = Depends(require_admin),
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

    from gsuid_core.ai_core.mcp.node_profile import mcp_node_id
    from gsuid_core.ai_core.capability_agents.persistence import delete_mcp_overlay

    delete_mcp_overlay(mcp_node_id(config_id))

    # 级联删除该服务全部用户凭据行（残留 = 孤儿凭据）
    from gsuid_core.ai_core.mcp.credential_store import MCPUserCredential

    deleted_creds = await MCPUserCredential.delete_rows_for_config(config_id)

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
            "deleted_credential_count": deleted_creds,
        },
    }


@app.post("/api/ai/mcp/{config_id}/toggle", summary="切换启用/禁用状态", tags=MCP_CONFIG)
async def toggle_mcp_config(
    config_id: str,
    _: Dict[str, Any] = Depends(require_admin),
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


@app.post("/api/ai/mcp/reload", summary="热重载所有配置", tags=MCP_CONFIG)
async def reload_mcp_configs(
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """
    热重载所有 MCP 配置，并按已保存的工具清单重新注册。

    不连接 MCP 服务器。清单为空的服务器本次不注册。
    控制台对单个服务器执行注册时才会连接。

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


@app.get("/api/ai/mcp/{config_id}/tools", summary="从已配置服务器发现工具", tags=MCP_CONFIG)
async def discover_mcp_tools(
    config_id: str,
    _: Dict[str, Any] = Depends(require_auth),
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
    from gsuid_core.ai_core.mcp.client import ensure_checkout_for_config
    from gsuid_core.ai_core.mcp.credential_store import render_client_kwargs

    config = mcp_config_manager.get_config(config_id)
    if not config:
        return {
            "status": 1,
            "msg": f"MCP 配置 '{config_id}' 不存在",
            "data": None,
        }

    # 源码检出型：探测前确保源码已克隆（失败不拦截，由连接错误兜底反馈）
    await ensure_checkout_for_config(config)

    try:
        kwargs = render_client_kwargs(config, None)
        client = MCPClient(
            name=config.name,
            transport=config.get_transport(),
            command=kwargs["command"],
            args=kwargs["args"],
            env=kwargs["env"],
            url=kwargs["url"],
            headers=kwargs["headers"],
            cwd=config.cwd,
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
        logger.warning(t("log.mcp.discover_tools_failed", p0=config.name, e=e))
        return {
            "status": 1,
            "msg": f"连接 MCP 服务器失败: {e}",
            "data": None,
        }


class MCPDiscoverRequest(BaseModel):
    """MCP 临时配置（仅用于发现工具，不保存）"""

    name: str
    transport: str = MCP_TRANSPORT_STDIO
    command: str = ""
    args: List[str] = []
    env: Dict[str, str] = {}
    url: str = ""
    headers: Dict[str, str] = {}
    cwd: str = ""
    source_url: str = ""
    requires_checkout: bool = False


@app.post("/api/ai/mcp/tools/discover", summary="从临时配置发现工具", tags=MCP_CONFIG)
async def discover_tools_from_temp_config(
    body: MCPDiscoverRequest,
    _: Dict[str, Any] = Depends(require_admin),
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
    from gsuid_core.ai_core.mcp.client import (
        rewrite_checkout_args,
        repair_checkout_script,
        ensure_mcp_source_checkout,
    )

    args = list(body.args)
    resolved_transport = resolve_mcp_transport(
        body.transport,
        url=body.url,
        command=body.command,
    )
    # 源码检出型：探测前自动克隆源码到工作目录，并改写目录旗标/占位参数
    if resolved_transport == MCP_TRANSPORT_STDIO and body.requires_checkout:
        if not body.source_url:
            return {
                "status": 1,
                "msg": "该安装方式需要克隆源码仓库运行，但缺少仓库地址",
                "data": None,
            }
        workdir, checkout_err = await ensure_mcp_source_checkout(body.name, body.source_url, body.cwd)
        if checkout_err:
            return {
                "status": 1,
                "msg": f"源码自动克隆失败：{checkout_err}",
                "data": None,
            }
        repair_note = repair_checkout_script(args, workdir)
        if repair_note:
            logger.info(t("log.mcp.checkout_repaired", p0=body.name, p1=repair_note))
        args = rewrite_checkout_args(args, workdir)

    try:
        client = MCPClient(
            name=body.name,
            transport=resolved_transport,
            command=body.command,
            args=args,
            env=body.env,
            url=body.url,
            headers=body.headers,
            cwd=body.cwd,
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
        logger.warning(t("log.mcp.discover_tools_failed", p0=body.name, e=e))
        return {
            "status": 1,
            "msg": f"连接 MCP 服务器失败: {e}",
            "data": None,
        }


@app.post("/api/ai/mcp/{config_id}/generate-profile", summary="生成中文介绍", tags=MCP_CONFIG)
async def generate_mcp_profile(
    config_id: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """手动触发该服务的中文介绍 LLM 补全（强制，忽略已有画像）。

    Returns:
        status: 0成功，1失败
        data: profile_status（pending 表示已排入后台生成）
    """
    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return {"status": 1, "msg": f"MCP 配置 '{config_id}' 不存在", "data": None}
    if not config.enabled or not config.register_as_ai_tools:
        return {"status": 1, "msg": "该服务未启用 AI 工具注册，无法生成介绍", "data": None}

    from gsuid_core.ai_core.mcp.profile_generator import (
        get_profile_status,
        schedule_profile_generation,
    )

    scheduled = schedule_profile_generation(config_id, force=True)
    return {
        "status": 0,
        "msg": "ok",
        "data": {"profile_status": get_profile_status(config_id), "scheduled": scheduled},
    }


@app.post("/api/ai/mcp/{config_id}/adopt-profile", summary="采纳生成的中文介绍", tags=MCP_CONFIG)
async def adopt_mcp_profile(
    config_id: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """把 generated_profile.description 写入用户 description，并清掉生成画像。"""
    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return {"status": 1, "msg": f"MCP 配置 '{config_id}' 不存在", "data": None}
    profile = config.generated_profile
    if profile is None:
        return {"status": 1, "msg": "该服务没有可采纳的生成介绍", "data": None}

    success, msg = mcp_config_manager.update_config(
        config_id,
        {"description": profile["description"], "generated_profile": None},
    )
    if not success:
        return {"status": 1, "msg": msg, "data": None}

    config2 = mcp_config_manager.get_config(config_id)
    if config2 is not None:
        from gsuid_core.ai_core.agent_node import get_node, register_agent_node
        from gsuid_core.ai_core.mcp.node_profile import mcp_node_id, build_mcp_node

        existing = get_node(mcp_node_id(config_id))
        if existing is not None:
            register_agent_node(build_mcp_node(config_id, config2, list(existing.tool_names)))

    return {
        "status": 0,
        "msg": "ok",
        "data": _config_response_dict(config_id, config2) if config2 is not None else None,
    }


class MCPImportRequest(BaseModel):
    """MCP 导入请求模型"""

    json_config: str  # 粘贴的 JSON 配置


@app.post("/api/ai/mcp/tools/import", summary="从 JSON 配置导入 MCP 服务器", tags=MCP_CONFIG)
async def import_mcp_from_json(
    body: MCPImportRequest,
    _: Dict[str, Any] = Depends(require_admin),
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
        draft = parse_mcp_json_server(server_name, server_config)
        if draft is None:
            return {
                "status": 1,
                "msg": "mcpServers 中首个服务器结构不合法",
                "data": None,
            }

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
        if mcp_config_manager.name_in_use(server_name):
            return {
                "status": 1,
                "msg": f"已存在同名启用的 MCP 服务 '{server_name}'，请改名",
                "data": None,
            }

        tools = []
        try:
            from gsuid_core.ai_core.mcp import MCPClient

            client = MCPClient(
                name=server_name,
                transport=draft.get("transport", MCP_TRANSPORT_STDIO),
                command=draft.get("command", ""),
                args=draft.get("args", []),
                env=draft.get("env", {}),
                url=draft.get("url", ""),
                headers=draft.get("headers", {}),
                cwd=draft.get("cwd", ""),
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
            transport=draft.get("transport", MCP_TRANSPORT_STDIO),
            command=draft.get("command", ""),
            args=draft.get("args", []),
            env=draft.get("env", {}),
            url=draft.get("url", ""),
            headers=draft.get("headers", {}),
            enabled=True,
            register_as_ai_tools=True,
            tools=tools,
        )

        success, msg = mcp_config_manager.create_config(config_id, mcp_config)
        if not success:
            return {
                "status": 1,
                "msg": f"创建配置失败: {msg}",
                "data": None,
            }

        # 清单已在手：按目录注册 + 建节点，不再二次连接服务器。
        tool_count, register_msg = await register_single_from_catalog(config_id)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "config_id": config_id,
                "name": server_name,
                "tools_count": len(tools),
                "tool_names": [t.name for t in tools],
                "registered_count": tool_count,
                "register_msg": register_msg,
            },
        }

    return {
        "status": 1,
        "msg": "不支持的 JSON 格式，请确保包含 mcpServers 字段",
        "data": None,
    }


# --- MCP 工具配置 (mcp_tools_config) — details 参数映射 API ---


@app.get("/api/ai/mcp-tools-config/list", summary="获取 MCP 工具配置列表", tags=MCP_CONFIG)
async def get_mcp_tools_config_list(
    _: Dict[str, Any] = Depends(require_auth),
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


@app.get("/api/ai/mcp-tools-config/{item_key}", summary="获取指定 MCP 工具配置详情", tags=MCP_CONFIG)
async def get_mcp_tools_config_detail(
    item_key: str,
    _: Dict[str, Any] = Depends(require_auth),
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


@app.put("/api/ai/mcp-tools-config/{item_key}", summary="更新 MCP 工具配置（含 details 参数映射）", tags=MCP_CONFIG)
async def update_mcp_tools_config(
    item_key: str,
    body: MCPToolDetailsUpdate,
    _: Dict[str, Any] = Depends(require_admin),
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


# --- MCP 用户凭据绑定管理 ---


class MCPBindPair(BaseModel):
    """管理员为订阅表主人批量安装绑定的目标身份对。"""

    bot_id: str
    user_id: str


class MCPBindRequest(BaseModel):
    pairs: List[MCPBindPair]


@app.get("/api/ai/mcp/{config_id}/bindings", summary="获取服务的用户绑定列表", tags=MCP_CONFIG)
async def list_mcp_bindings(
    config_id: str,
    _: Dict[str, Any] = Depends(require_admin_header),
) -> Dict[str, Any]:
    """返回该服务全部用户绑定行（凭据只露掩码尾号）。"""
    from gsuid_core.ai_core.mcp.credential_store import MCPUserCredential, masked_summary

    rows = await MCPUserCredential.list_rows_for_config(config_id)
    items = [
        {
            "bot_id": r.bot_id,
            "user_id": r.user_id,
            "bound_via": r.bound_via,
            "status": r.status,
            "bound_at": r.bound_at,
            "last_ok_at": r.last_ok_at,
            "masked_creds": masked_summary(r),
        }
        for r in rows
    ]
    return {"status": 0, "msg": "ok", "data": {"bindings": items, "count": len(items)}}


@app.post("/api/ai/mcp/{config_id}/bind", summary="为主人批量安装绑定", tags=MCP_CONFIG)
async def admin_bind_mcp(
    config_id: str,
    body: MCPBindRequest,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """用共享 token 值为勾选的主人身份落 ``admin_install`` 绑定行。

    值取自 config.env 的 user_env_keys 快照（装机已验活，不重复验）；
    无 user_env_keys 的服务拒绝（没有可绑凭据）。
    """
    import time

    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return {"status": 1, "msg": f"MCP 配置 '{config_id}' 不存在", "data": None}
    if not config.user_env_keys:
        return {"status": 1, "msg": "该服务没有可用户绑定的凭据项", "data": None}

    shared_creds = {k: v for k, v in config.env.items() if k in config.user_env_keys}
    if not shared_creds:
        return {"status": 1, "msg": "共享配置中找不到凭据值，无法安装绑定", "data": None}

    from gsuid_core.ai_core.mcp.startup import drop_user_client_cache
    from gsuid_core.ai_core.mcp.credential_store import MCPUserCredential

    now = time.time()
    bound = 0
    for pair in body.pairs:
        await MCPUserCredential.upsert_row(
            config_id,
            pair.bot_id,
            pair.user_id,
            dict(shared_creds),
            bound_via="admin_install",
            bound_at=now,
            last_ok_at=now,
        )
        drop_user_client_cache(config_id, pair.bot_id, pair.user_id)
        bound += 1

    return {"status": 0, "msg": "ok", "data": {"bound_count": bound}}


@app.delete("/api/ai/mcp/{config_id}/bindings/{bot_id}/{user_id}", summary="吊销一条用户绑定", tags=MCP_CONFIG)
async def revoke_mcp_binding(
    config_id: str,
    bot_id: str,
    user_id: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """删除一条用户绑定并清对应 @user: 客户端缓存。"""
    from gsuid_core.ai_core.mcp.startup import drop_user_client_cache
    from gsuid_core.ai_core.mcp.credential_store import MCPUserCredential

    ok = await MCPUserCredential.delete_row(config_id, bot_id, user_id)
    drop_user_client_cache(config_id, bot_id, user_id)
    if not ok:
        return {"status": 1, "msg": "该用户在此服务下没有绑定", "data": None}
    return {"status": 0, "msg": "ok", "data": {"revoked": True}}
