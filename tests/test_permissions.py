from gsuid_core.models import MessageReceive
from gsuid_core.ai_core.mcp.config_manager import MCPConfig


def test_message_receive_defaults_to_normal_user_pm():
    assert MessageReceive().user_pm == 6


def test_mcp_tool_permission_defaults_to_normal_user_pm():
    config = MCPConfig(name="test", command="test")

    assert config.get_tool_required_pm("missing_tool") == 6
