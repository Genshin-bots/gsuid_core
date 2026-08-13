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
    "enabled": true,
    "register_as_ai_tools": false,
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
    "enabled": true,
    "register_as_ai_tools": false,
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

import json
from typing import Any
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


@dataclass
class MCPToolDefinition:
    """MCP 工具定义"""

    name: str
    description: str = ""
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPToolDefinition":
        """从字典创建工具定义"""
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            parameters=data.get("parameters", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


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
    """

    name: str
    transport: str = MCP_TRANSPORT_STDIO
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    register_as_ai_tools: bool = False  # 是否将该 MCP 服务器的工具注册为 AI Tools
    tools: list[MCPToolDefinition] = field(default_factory=list)  # 工具列表
    tool_permissions: dict[str, int] = field(default_factory=dict)

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
        if self.tool_permissions:
            result["tool_permissions"] = self.tool_permissions
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPConfig":
        """从字典创建配置"""
        tools = [MCPToolDefinition.from_dict(t) for t in data.get("tools", [])]
        url = data.get("url", "")
        command = data.get("command", "")
        transport = resolve_mcp_transport(
            data.get("transport", ""),
            url=url,
            command=command,
        )
        return cls(
            name=data["name"],
            transport=transport,
            command=command,
            args=data.get("args", []),
            env=data.get("env", {}),
            url=url,
            headers=data.get("headers", {}),
            enabled=data.get("enabled", True),
            register_as_ai_tools=data.get("register_as_ai_tools", False),
            tools=tools,
            tool_permissions=data.get("tool_permissions", {}),
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
        """加载所有配置文件到缓存"""
        self._cache.clear()
        for config_file in self._base_path.glob("*.json"):
            config_id = config_file.stem
            try:
                with open(config_file, "r", encoding="UTF-8") as f:
                    data = json.load(f)
                self._cache[config_id] = MCPConfig.from_dict(data)
            except Exception as e:
                logger.error(i18n_t("log.mcp.mcp_config_load_configuration_file_fail", config_file=config_file, e=e))

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
mcp_config_manager = MCPConfigManager()
