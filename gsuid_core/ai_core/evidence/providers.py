"""真值源 Provider（§4.4）：能实时查到的状态不信历史观测。"""

from __future__ import annotations

from typing import Protocol

from gsuid_core.ai_core.evidence.store import LiveState


class StateProvider(Protocol):
    async def current(self, service: str, bot_id: str, user_id: str) -> LiveState | None: ...


class McpCredentialProvider:
    """MCP 私有服务的凭据状态；查不到行按未绑定。"""

    async def current(self, service: str, bot_id: str, user_id: str) -> LiveState | None:
        from gsuid_core.ai_core.mcp.credential_store import MCPUserCredential

        row = await MCPUserCredential.get_row(service, bot_id, user_id)
        return LiveState(
            service=service,
            user_id=user_id,
            aspect="credential",
            value="bound" if row is not None else "unbound",
        )


_MCP_PRIVATE_SERVICES: set[str] = set()
_PROVIDERS: dict[str, StateProvider] = {}


def register_private_mcp_service(config_id: str) -> None:
    _MCP_PRIVATE_SERVICES.add(config_id)


def is_private_mcp_service(config_id: str) -> bool:
    return config_id in _MCP_PRIVATE_SERVICES


def register_state_provider(service: str, provider: StateProvider) -> None:
    _PROVIDERS[service] = provider


def get_state_provider(service: str) -> StateProvider | None:
    if service in _PROVIDERS:
        return _PROVIDERS[service]
    return McpCredentialProvider() if service in _MCP_PRIVATE_SERVICES else None
