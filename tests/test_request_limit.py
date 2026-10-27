"""思考轮按 create_by 分档，不看调用方传入的 max_iterations。"""

from gsuid_core.ai_core.const import (
    MAIN_TURN_CREATE_BY,
    SHORT_RUN_CREATE_BY,
    TASK_TURN_CREATE_BY,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.agent_run.request_limit import request_limit_for

_UNLISTED = ("LLM", "SubAgent", "PluginSideAgent")


def test_request_limit_follows_create_by() -> None:
    main_limit: int = ai_config.get_config("multi_agent_lenth").data
    task_limit: int = ai_config.get_config("task_max_iterations").data
    for name in MAIN_TURN_CREATE_BY:
        assert request_limit_for(name) == main_limit
    for name in TASK_TURN_CREATE_BY:
        assert request_limit_for(name) == task_limit
    for name in SHORT_RUN_CREATE_BY:
        assert request_limit_for(name) == 5
    for name in _UNLISTED:
        assert request_limit_for(name) == main_limit
