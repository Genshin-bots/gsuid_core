"""思考轮上限只看 create_by。调用方传入的 max_iterations 不再生效。"""

from gsuid_core.ai_core.const import (
    SHORT_RUN_CREATE_BY,
    TASK_TURN_CREATE_BY,
    SHORT_RUN_MAX_ITERATIONS,
)
from gsuid_core.ai_core.configs.ai_config import ai_config


def request_limit_for(create_by: str) -> int:
    if create_by in TASK_TURN_CREATE_BY:
        task_limit: int = ai_config.get_config("task_max_iterations").data
        return task_limit
    if create_by in SHORT_RUN_CREATE_BY:
        return SHORT_RUN_MAX_ITERATIONS
    main_limit: int = ai_config.get_config("multi_agent_lenth").data
    return main_limit
