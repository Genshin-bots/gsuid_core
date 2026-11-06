"""
MCP 配置管理器模块

管理用户自定义的 MCP 服务器配置，支持增删改查。
每个 MCP 配置以独立 JSON 文件存储在 data/ai_core/mcp_configs/ 目录下。

支持三种传输方式:
1. stdio — 通过命令行启动本地 MCP 服务器（command + args + env）
2. sse — 通过旧版 HTTP/SSE 连接远程 MCP 服务器（url + headers）
3. streamable_http — 通过 Streamable HTTP 连接远程 MCP 服务器（url + headers）

配置文件格式 (JSON) — stdio 示例:
{
    "name": "MiniMax",
    "transport": "stdio",
    "command": "uvx",
    "args": ["minimax-coding-plan-mcp"],
    "env": {"MINIMAX_API_KEY": "your_key"},
    "schema_version": 2,
    "enabled": true,
    "register_as_ai_tools": true,
    "tools": [
        {
            "name": "web_search",
            "description": "Web search tool",
            "parameters": {
                "query": {"type": "string", "required": true},
                "max_results": {"type": "integer", "required": false}
            }
        }
    ]
}

配置文件格式 (JSON) — Streamable HTTP 示例:
{
    "name": "Example MCP",
    "transport": "streamable_http",
    "url": "https://example.com/mcp",
    "headers": {"Authorization": "Bearer your_access_secret"},
    "schema_version": 2,
    "enabled": true,
    "register_as_ai_tools": true,
    "tools": [
        {
            "name": "zhihu_search",
            "description": "知乎站内搜索",
            "parameters": {
                "query": {"type": "string", "required": true},
                "count": {"type": "integer", "required": false}
            }
        }
    ]
}
"""

import re
import json
from typing import Any, Literal, Optional, TypedDict
from pathlib import Path
from dataclasses import field, dataclass

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import MCP_CONFIGS_PATH
from gsuid_core.ai_core.mcp.transport import (
    MCP_TRANSPORT_STDIO,
    is_http_mcp_transport,
    resolve_mcp_transport,
)

# 配置 ID 格式分隔符
MCP_TOOL_ID_SEPARATOR = " - "

# 配置 schema 版本：v2 起 register_as_ai_tools 生效，并新增描述 / 市场元数据 / 生成画像字段。
# v3 起：工具分级字段（exposure/mutating/sensitive_args/credential_params）+
# 服务级 user_env_keys/public_ack/allow_autonomous_private/bind_help/bind_alias。
MCP_CONFIG_SCHEMA_VERSION = 3


class MarketMeta(TypedDict, total=False):
    """市场来源元数据：随配置落盘，全部可选。"""

    source: Literal["modelscope", "preset"]
    qualified_name: str
    chinese_name: str
    categories: list[str]
    logo_url: str
    variant_label: str
    source_url: str
    requires_checkout: bool


class GeneratedProfile(TypedDict):
    """LLM 生成的中文介绍（落盘在 MCP 配置内）。"""

    description: str
    keywords: list[str]
    source_hash: str
    model: str
    generated_at: int


class MCPServerDraft(TypedDict, total=False):
    """一份 mcpServers JSON / 市场变体规范化后的安装草稿。"""

    name: str
    transport: str
    command: str
    args: list[str]
    env: dict[str, str]
    url: str
    headers: dict[str, str]


def parse_market_meta(raw: Any) -> MarketMeta:
    """校验 market_meta：非 dict 或字段类型不对即丢弃，不中断读取。"""
    meta: MarketMeta = {}
    if not isinstance(raw, dict):
        return meta
    source = raw.get("source")
    if source in ("modelscope", "preset"):
        meta["source"] = source
    for key in ("qualified_name", "chinese_name", "logo_url", "variant_label"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            meta[key] = value.strip()
    categories = raw.get("categories")
    if isinstance(categories, list):
        meta["categories"] = [c.strip() for c in categories if isinstance(c, str) and c.strip()]
    source_url = raw.get("source_url")
    if isinstance(source_url, str) and source_url.strip():
        meta["source_url"] = source_url.strip()
    if raw.get("requires_checkout") is True:
        meta["requires_checkout"] = True
    return meta


def parse_generated_profile(raw: Any) -> Optional[GeneratedProfile]:
    """校验落盘的 generated_profile；结构残缺即视为 None。"""
    if not isinstance(raw, dict):
        return None
    description = raw.get("description")
    source_hash = raw.get("source_hash")
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(source_hash, str) or not source_hash:
        return None
    keywords_raw = raw.get("keywords")
    keywords = [k for k in keywords_raw if isinstance(k, str) and k.strip()] if isinstance(keywords_raw, list) else []
    model = raw.get("model")
    generated_at = raw.get("generated_at")
    return GeneratedProfile(
        description=description.strip(),
        keywords=keywords,
        source_hash=source_hash,
        model=model if isinstance(model, str) else "",
        generated_at=generated_at if isinstance(generated_at, int) else 0,
    )


def parse_mcp_json_server(name: str, raw: Any) -> Optional[MCPServerDraft]:
    """把单个 mcpServers JSON 节点规范化为安装草稿；结构不合法返回 None。"""
    if not isinstance(raw, dict):
        return None
    env_raw = raw.get("env")
    args_raw = raw.get("args")
    headers_raw = raw.get("headers", raw.get("headersTemplate"))
    url_raw = raw.get("url", raw.get("sseUrl", ""))
    command_raw = raw.get("command", "")
    transport_raw = raw.get("transport", raw.get("type", ""))

    url = url_raw if isinstance(url_raw, str) else ""
    command = command_raw if isinstance(command_raw, str) else ""

    draft: MCPServerDraft = {
        "name": name,
        "transport": resolve_mcp_transport(
            transport_raw if isinstance(transport_raw, str) else "", url=url, command=command
        ),
        "command": command,
        "args": [],
        "env": {},
        "url": url,
        "headers": {},
    }
    if isinstance(args_raw, list):
        draft["args"] = [a for a in args_raw if isinstance(a, str)]
    if isinstance(env_raw, dict):
        draft["env"] = {k: v for k, v in env_raw.items() if isinstance(k, str) and isinstance(v, str)}
    if isinstance(headers_raw, dict):
        draft["headers"] = {k: v for k, v in headers_raw.items() if isinstance(k, str) and isinstance(v, str)}
    return draft


@dataclass
class MCPToolDefinition:
    """MCP 工具定义

    exposure: ``private``=发起人本人凭据（未绑→BIND_GUIDE）；``public``=共享凭据。
    mutating: 改账号状态 → 执行前走 tool_call_gate 用户确认码。
    sensitive_args: 入参含地址/坐标/手机号 → 参数不记日志、args_repr 掩码。
    credential_params: 调用参数本身是凭据 → 从 LLM 可见签名剔除，调用时注入。
    """

    name: str
    description: str = ""
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    exposure: Literal["private", "public"] = "private"
    mutating: bool = False
    sensitive_args: bool = False
    credential_params: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPToolDefinition":
        """从字典创建工具定义"""
        exposure_raw = data.get("exposure")
        cred_params_raw = data.get("credential_params")
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            parameters=data.get("parameters", {}),
            exposure="public" if exposure_raw == "public" else "private",
            mutating=bool(data.get("mutating", False)),
            sensitive_args=bool(data.get("sensitive_args", False)),
            credential_params=[p for p in cred_params_raw if isinstance(p, str)]
            if isinstance(cred_params_raw, list)
            else [],
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        result: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "exposure": self.exposure,
        }
        if self.mutating:
            result["mutating"] = True
        if self.sensitive_args:
            result["sensitive_args"] = True
        if self.credential_params:
            result["credential_params"] = list(self.credential_params)
        return result


def input_schema_to_parameters(input_schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 MCP list_tools 返回的 JSON Schema 转成 MCPToolDefinition.parameters 扁平格式。

    保留 type / description(含 title 兜底) / required / enum / default / anyOf / items 等字段；
    上游返回不可信，逐字段 isinstance 校验。
    """
    properties_raw = input_schema.get("properties")
    required_raw = input_schema.get("required")
    required = {n for n in required_raw if isinstance(n, str)} if isinstance(required_raw, list) else set()
    parameters: dict[str, dict[str, Any]] = {}
    if not isinstance(properties_raw, dict):
        return parameters
    for param_name, param_schema in properties_raw.items():
        if not isinstance(param_name, str) or not isinstance(param_schema, dict):
            continue
        param_def: dict[str, Any] = {}
        type_raw = param_schema.get("type")
        if isinstance(type_raw, str) and type_raw:
            param_def["type"] = type_raw
        desc = param_schema.get("description") or param_schema.get("title")
        if isinstance(desc, str) and desc:
            param_def["description"] = desc
        if param_name in required:
            param_def["required"] = True
        for extra_key in ("enum", "default", "anyOf", "allOf", "oneOf", "items"):
            if extra_key in param_schema:
                param_def[extra_key] = param_schema[extra_key]
        parameters[param_name] = param_def
    return parameters


@dataclass
class MCPConfig:
    """MCP 服务器配置数据类

    支持三种传输方式:
    - stdio: 通过 command + args + env 启动本地进程
    - sse: 通过 url + headers 连接远程 SSE 服务器（旧传输）
    - streamable_http: 通过 url + headers 连接远程 Streamable HTTP 服务器

    Attributes:
        name: MCP 服务器名称
        transport: 传输方式，stdio / sse / streamable_http，默认 "stdio"
            若未显式设置，会根据 url / command 字段自动推断:
            - URL 路径以 /sse 结尾 → sse
            - 其它 http(s) URL → streamable_http
            - 否则 → stdio
        command: 启动命令（stdio 模式必填）
        args: 命令参数（stdio 模式）
        env: 环境变量（stdio 模式）
        url: 远程服务器 URL（sse / streamable_http 模式必填）
        headers: HTTP 请求头（远程模式，如 Authorization 等）
        enabled: 是否启用
        register_as_ai_tools: 是否将该 MCP 服务器的工具注册为 AI Tools
        tools: 工具列表
        tool_permissions: 工具权限配置，格式为 {tool_name: required_pm}
            值为最低权限等级（pm），与 Event.user_pm 对比：
            - 0: 仅 master 用户
            - 1: superuser 及以上
            - 2: 群主及以上
            - 3: 群管理员及以上
            - 6: 所有用户（默认）
            例如 {"send_email": 0, "query_data": 6}
        description: 用户/市场侧的功能描述（写 when_to_use 的备选素材，不进检索）
        market_meta: 市场安装时带回的来源元数据
        generated_profile: LLM 生成的中文介绍（可直接用于路由文案）
        user_env_keys: 可按用户绑定的 env key 列表（管理员确认值，默认全勾）
        public_ack: 首次 private→public 拨档已确认
        allow_autonomous_private: 无人执行体可否以共享凭据跑私有工具（mutating 仍拒）
        bind_help: 管理员补充的 token 获取指引（BIND_GUIDE 素材）
        bind_url: 凭据获取地址（BIND_GUIDE 中单独一行展示）
        bind_alias: 管理员补充的服务别名列表（mcp绑定 服务名解析用）
        cwd: stdio 子进程工作目录（源码检出型服务指向 clone 的仓库目录）
    """

    name: str
    transport: str = MCP_TRANSPORT_STDIO
    command: str = ""
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    register_as_ai_tools: bool = False  # 是否将该 MCP 服务器的工具注册为 AI Tools
    tools: list[MCPToolDefinition] = field(default_factory=list)  # 工具列表
    tool_permissions: dict[str, int] = field(default_factory=dict)
    description: str = ""
    market_meta: MarketMeta = field(default_factory=MarketMeta)
    generated_profile: Optional[GeneratedProfile] = None
    user_env_keys: list[str] = field(default_factory=list)
    public_ack: bool = False
    allow_autonomous_private: bool = False
    bind_help: str = ""
    bind_url: str = ""
    bind_alias: list[str] = field(default_factory=list)

    def get_transport(self) -> str:
        """获取有效的传输方式（规范化 + 必要时按 url / command 推断）。"""
        return resolve_mcp_transport(
            self.transport,
            url=self.url,
            command=self.command,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        effective_transport = self.get_transport()
        result: dict[str, Any] = {
            "schema_version": MCP_CONFIG_SCHEMA_VERSION,
            "name": self.name,
            "transport": effective_transport,
            "enabled": self.enabled,
            "register_as_ai_tools": self.register_as_ai_tools,
            "tools": [t.to_dict() for t in self.tools],
        }
        if is_http_mcp_transport(effective_transport):
            result["url"] = self.url
            if self.headers:
                result["headers"] = self.headers
            if self.command:
                result["command"] = self.command
            if self.args:
                result["args"] = self.args
            if self.env:
                result["env"] = self.env
        else:
            result["command"] = self.command
            if self.args:
                result["args"] = self.args
            if self.env:
                result["env"] = self.env
            if self.url:
                result["url"] = self.url
            if self.headers:
                result["headers"] = self.headers
            if self.cwd:
                result["cwd"] = self.cwd
        if self.tool_permissions:
            result["tool_permissions"] = self.tool_permissions
        if self.description:
            result["description"] = self.description
        if self.market_meta:
            result["market_meta"] = dict(self.market_meta)
        if self.generated_profile is not None:
            result["generated_profile"] = dict(self.generated_profile)
        if self.user_env_keys:
            result["user_env_keys"] = list(self.user_env_keys)
        if self.public_ack:
            result["public_ack"] = True
        if self.allow_autonomous_private:
            result["allow_autonomous_private"] = True
        if self.bind_help:
            result["bind_help"] = self.bind_help
        if self.bind_url:
            result["bind_url"] = self.bind_url
        if self.bind_alias:
            result["bind_alias"] = list(self.bind_alias)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPConfig":
        """从字典创建配置"""
        tools_raw = data.get("tools")
        tools = (
            [MCPToolDefinition.from_dict(t) for t in tools_raw if isinstance(t, dict)]
            if isinstance(tools_raw, list)
            else []
        )
        url_raw = data.get("url")
        url = url_raw if isinstance(url_raw, str) else ""
        command_raw = data.get("command")
        command = command_raw if isinstance(command_raw, str) else ""
        transport_raw = data.get("transport")
        transport = resolve_mcp_transport(
            transport_raw if isinstance(transport_raw, str) else "",
            url=url,
            command=command,
        )
        args_raw = data.get("args")
        env_raw = data.get("env")
        headers_raw = data.get("headers")
        permissions_raw = data.get("tool_permissions")
        enabled_raw = data.get("enabled")
        register_raw = data.get("register_as_ai_tools")
        description_raw = data.get("description")
        bind_help_raw = data.get("bind_help")
        bind_url_raw = data.get("bind_url")
        cwd_raw = data.get("cwd")
        return cls(
            name=data["name"],
            transport=transport,
            command=command,
            args=[a for a in args_raw if isinstance(a, str)] if isinstance(args_raw, list) else [],
            env={k: v for k, v in env_raw.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(env_raw, dict)
            else {},
            url=url,
            headers={k: v for k, v in headers_raw.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(headers_raw, dict)
            else {},
            enabled=enabled_raw if isinstance(enabled_raw, bool) else True,
            register_as_ai_tools=register_raw if isinstance(register_raw, bool) else False,
            tools=tools,
            tool_permissions={k: v for k, v in permissions_raw.items() if isinstance(k, str) and isinstance(v, int)}
            if isinstance(permissions_raw, dict)
            else {},
            description=description_raw if isinstance(description_raw, str) else "",
            market_meta=parse_market_meta(data.get("market_meta")),
            generated_profile=parse_generated_profile(data.get("generated_profile")),
            user_env_keys=[k for k in data.get("user_env_keys", []) if isinstance(k, str)]
            if isinstance(data.get("user_env_keys"), list)
            else [],
            public_ack=bool(data.get("public_ack", False)),
            allow_autonomous_private=bool(data.get("allow_autonomous_private", False)),
            bind_help=bind_help_raw if isinstance(bind_help_raw, str) else "",
            bind_url=bind_url_raw if isinstance(bind_url_raw, str) else "",
            cwd=cwd_raw if isinstance(cwd_raw, str) else "",
            bind_alias=[a for a in data.get("bind_alias", []) if isinstance(a, str)]
            if isinstance(data.get("bind_alias"), list)
            else [],
        )

    def get_tool_required_pm(self, tool_name: str) -> int:
        """获取指定工具所需的最低权限等级

        Args:
            tool_name: 工具名称

        Returns:
            权限等级 (0=master, 1=superuser, 2=群主, 3=群管理员, 6=普通用户)
            默认返回 6（所有人可用）
        """
        return self.tool_permissions.get(tool_name, 6)


def parse_mcp_tool_id(mcp_tool_id: str) -> tuple[str, str]:
    """
    解析 MCP 工具 ID

    格式: "{mcp_id}{separator}{tool_name}"
    例如: "minimax - web_search"

    Args:
        mcp_tool_id: MCP 工具 ID

    Returns:
        (mcp_id, tool_name) 元组

    Raises:
        ValueError: 格式错误时抛出
    """
    if MCP_TOOL_ID_SEPARATOR not in mcp_tool_id:
        raise ValueError(
            i18n_t(
                "无效的 MCP 工具 ID 格式: '{mcp_tool_id}'，期望格式为 '{{mcp_id}}{MCP_TOOL_ID_SEPARATOR}{{tool_name}}'",
                mcp_tool_id=mcp_tool_id,
                MCP_TOOL_ID_SEPARATOR=MCP_TOOL_ID_SEPARATOR,
            )
        )

    parts = mcp_tool_id.split(MCP_TOOL_ID_SEPARATOR, 1)
    return parts[0], parts[1]


def format_mcp_tool_id(mcp_id: str, tool_name: str) -> str:
    """
    格式化 MCP 工具 ID

    Args:
        mcp_id: MCP 配置 ID
        tool_name: 工具名称

    Returns:
        格式化的 MCP 工具 ID
    """
    return f"{mcp_id}{MCP_TOOL_ID_SEPARATOR}{tool_name}"


class MCPConfigManager:
    """
    MCP 配置管理器

    管理 data/ai_core/mcp_configs/ 目录下的 MCP 服务器配置文件。
    每个配置文件对应一个 MCP 服务器，文件名为 {config_id}.json。
    """

    def __init__(self) -> None:
        self._base_path: Path = MCP_CONFIGS_PATH
        self._cache: dict[str, MCPConfig] = {}
        self._load_all()

    def _get_config_path(self, config_id: str) -> Path:
        """获取配置文件的完整路径"""
        return self._base_path / f"{config_id}.json"

    def _load_all(self) -> None:
        """加载所有配置文件到缓存；旧 schema 自动迁移后写回。"""
        self._cache.clear()
        migrated = 0
        for config_file in self._base_path.glob("*.json"):
            config_id = config_file.stem
            try:
                with open(config_file, "r", encoding="UTF-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    continue
                version = data.get("schema_version")
                if not isinstance(version, int) or version < MCP_CONFIG_SCHEMA_VERSION:
                    # v1 的 register_as_ai_tools 从未生效，迁移时统一翻为 True。
                    data["register_as_ai_tools"] = True
                    if not isinstance(data.get("user_env_keys"), list):
                        # v2→v3 fail-closed：env 全部默认可用户绑定；有绑定项则全工具私有。
                        env_keys = [k for k in data.get("env", {}) if isinstance(k, str)]
                        data["user_env_keys"] = env_keys
                        has_cred = bool(env_keys) or bool(data.get("headers"))
                        if has_cred:
                            for tool in data.get("tools", []):
                                if isinstance(tool, dict) and "exposure" not in tool:
                                    tool["exposure"] = "private"
                        else:
                            for tool in data.get("tools", []):
                                if isinstance(tool, dict) and "exposure" not in tool:
                                    tool["exposure"] = "public"
                    data["schema_version"] = MCP_CONFIG_SCHEMA_VERSION
                    with open(config_file, "w", encoding="UTF-8") as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                    migrated += 1
                self._cache[config_id] = MCPConfig.from_dict(data)
            except Exception as e:
                logger.error(i18n_t("log.mcp.mcp_config_load_configuration_file_fail", config_file=config_file, e=e))
        if migrated:
            logger.warning(i18n_t("log.mcp.mcp_config_schema_migrated_count", p0=migrated))

    def name_in_use(self, name: str, exclude_config_id: str | None = None) -> bool:
        """同名（忽略大小写）是否已被启用配置占用。"""
        lowered = name.strip().lower()
        for cid, cfg in self._cache.items():
            if cid == exclude_config_id or not cfg.enabled:
                continue
            if cfg.name.strip().lower() == lowered:
                return True
        return False

    def list_configs(self) -> list[dict[str, Any]]:
        """
        列出所有 MCP 配置

        Returns:
            配置列表，每个元素包含 config_id 和配置详情
        """
        result: list[dict[str, Any]] = []
        for config_id, config in self._cache.items():
            item = config.to_dict()
            item["config_id"] = config_id
            result.append(item)
        return result

    def list_config_items(self) -> list[tuple[str, "MCPConfig"]]:
        """返回 (config_id, MCPConfig) 全量列表。"""
        return list(self._cache.items())

    def get_config(self, config_id: str) -> MCPConfig | None:
        """
        获取指定的 MCP 配置

        Args:
            config_id: 配置 ID（文件名不含扩展名）

        Returns:
            MCPConfig 实例，不存在则返回 None
        """
        return self._cache.get(config_id)

    def get_enabled_configs(self) -> list[tuple[str, MCPConfig]]:
        """
        获取所有启用的 MCP 配置

        Returns:
            (config_id, MCPConfig) 列表
        """
        return [(cid, cfg) for cid, cfg in self._cache.items() if cfg.enabled]

    def get_tool_definition(
        self,
        config_id: str,
        tool_name: str,
    ) -> MCPToolDefinition | None:
        """
        获取指定工具的定义

        Args:
            config_id: MCP 配置 ID
            tool_name: 工具名称

        Returns:
            MCPToolDefinition 实例，不存在则返回 None
        """
        config = self._cache.get(config_id)
        if not config:
            return None
        for tool in config.tools:
            if tool.name == tool_name:
                return tool
        return None

    def get_tool_by_mcp_tool_id(self, mcp_tool_id: str) -> tuple[MCPConfig, MCPToolDefinition] | None:
        """
        根据 MCP 工具 ID 获取对应的 MCP 配置和工具定义

        Args:
            mcp_tool_id: MCP 工具 ID，格式为 "{mcp_id} - {tool_name}"

        Returns:
            (MCPConfig, MCPToolDefinition) 元组，不存在则返回 None
        """
        try:
            mcp_id, tool_name = parse_mcp_tool_id(mcp_tool_id)
        except ValueError:
            return None

        config = self._cache.get(mcp_id)
        if not config:
            return None

        for tool in config.tools:
            if tool.name == tool_name:
                return config, tool

        return None

    def list_all_tools(self) -> list[dict[str, Any]]:
        """
        列出所有 MCP 配置中的所有工具

        Returns:
            工具列表，每项包含 config_id, tool_name, 工具详情
        """
        result: list[dict[str, Any]] = []
        for config_id, config in self._cache.items():
            for tool in config.tools:
                result.append(
                    {
                        "mcp_tool_id": format_mcp_tool_id(config_id, tool.name),
                        "config_id": config_id,
                        "tool_name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                )
        return result

    def create_config(self, config_id: str, config: MCPConfig) -> tuple[bool, str]:
        """
        创建新的 MCP 配置

        Args:
            config_id: 配置 ID（文件名不含扩展名）
            config: MCP 配置对象

        Returns:
            (是否成功, 消息)
        """
        if config_id in self._cache:
            return False, f"配置 '{config_id}' 已存在"

        config_path = self._get_config_path(config_id)
        try:
            with open(config_path, "w", encoding="UTF-8") as f:
                json.dump(config.to_dict(), f, indent=4, ensure_ascii=False)
            self._cache[config_id] = config
            logger.info(i18n_t("log.mcp.mcp_config_configuration_id_create", config_id=config_id))
            return True, "ok"
        except Exception as e:
            logger.error(i18n_t("log.mcp.mcp_config_create_configuration_fail", config_id=config_id, e=e))
            return False, str(e)

    def update_config(self, config_id: str, updates: dict[str, Any]) -> tuple[bool, str]:
        """
        更新 MCP 配置

        Args:
            config_id: 配置 ID
            updates: 要更新的字段字典

        Returns:
            (是否成功, 消息)
        """
        if config_id not in self._cache:
            return False, f"配置 '{config_id}' 不存在"

        current = self._cache[config_id]
        # to_dict 会省略空的 url/headers，切传输时必须把可写字段补全
        current_dict = current.to_dict()
        current_dict["command"] = current.command
        current_dict["args"] = list(current.args)
        current_dict["env"] = dict(current.env)
        current_dict["url"] = current.url
        current_dict["headers"] = dict(current.headers)
        current_dict["tool_permissions"] = dict(current.tool_permissions)

        for key, value in updates.items():
            if key == "config_id":
                continue
            current_dict[key] = value

        try:
            updated_config = MCPConfig.from_dict(current_dict)
            config_path = self._get_config_path(config_id)
            with open(config_path, "w", encoding="UTF-8") as f:
                json.dump(updated_config.to_dict(), f, indent=4, ensure_ascii=False)
            self._cache[config_id] = updated_config
            logger.info(i18n_t("log.mcp.mcp_config_configuration_id_update", config_id=config_id))
            return True, "ok"
        except Exception as e:
            logger.error(i18n_t("log.mcp.mcp_config_update_configuration_fail", config_id=config_id, e=e))
            return False, str(e)

    def delete_config(self, config_id: str) -> tuple[bool, str]:
        """
        删除 MCP 配置

        Args:
            config_id: 配置 ID

        Returns:
            (是否成功, 消息)
        """
        if config_id not in self._cache:
            return False, f"配置 '{config_id}' 不存在"

        config_path = self._get_config_path(config_id)
        try:
            config_path.unlink()
            del self._cache[config_id]
            logger.info(i18n_t("log.mcp.mcp_config_configuration_id_delete", config_id=config_id))
            return True, "ok"
        except Exception as e:
            logger.error(i18n_t("log.mcp.mcp_config_delete_configuration_fail", config_id=config_id, e=e))
            return False, str(e)

    def reload(self) -> None:
        """重新加载所有配置文件"""
        self._load_all()
        logger.info(i18n_t("log.mcp.mcp_config_reload_configurations_total_ok", p0=len(self._cache)))


# 全局单例
# 启发式词表（只产「建议」不做裁决）：疑似共享配置的 env key 打标，帮管理员
# 快速认出哪些该取消「可用户绑定」勾选。词表猜错不影响安全——默认全勾。
_SECRET_NAME_RE = re.compile(
    r"key|token|secret|password|credential|auth|api_?key|app_?key|access",
    re.IGNORECASE,
)
_SHARED_HINT_RE = re.compile(
    r"host|url|endpoint|mode|region|domain|base|prefix|suffix|dir|path",
    re.IGNORECASE,
)


def suggest_shared_env_keys(env: dict[str, str]) -> list[str]:
    """返回「疑似共享配置」的 env key 列表（UI 打标用）。

    判定：key 名像配置值（HOST/URL/MODE/ENDPOINT 等）且不是密钥词表命中，
    或值形如裸 URL/主机名且不含占位符。只供管理员快速取消勾选——
    ``user_env_keys`` 落库默认是全量 env key。
    """
    shared: list[str] = []
    for key in env:
        if _SECRET_NAME_RE.search(key):
            continue
        if _SHARED_HINT_RE.search(key):
            shared.append(key)
    return shared


mcp_config_manager = MCPConfigManager()
