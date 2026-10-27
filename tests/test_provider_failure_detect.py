"""测试 Provider 故障模式判断：确保中英文限流/超时/连接错误能被准确识别。"""

import pytest

from gsuid_core.ai_core.const import ERROR_QUOTA_EXHAUSTED
from gsuid_core.ai_core.utils import ERROR_TIMEOUT_TEXT
from gsuid_core.ai_core.configs.provider_router import looks_like_provider_failure


@pytest.mark.parametrize(
    "err_text",
    [
        "Request timed out.",
        "Connection timed out after 30 seconds",
        "read timeout",
        "HTTP 429 Too Many Requests",
        "Rate limit reached for model",
        "502 Bad Gateway",
        "503 Service Unavailable",
        f"执行出错: {ERROR_TIMEOUT_TEXT}",
        "执行出错: 请求超时",
        "网络连接超时",
        f"执行出错: {ERROR_QUOTA_EXHAUSTED}",
        "账户额度已用尽",
        "模型用量上限已触发",
    ],
)
def test_looks_like_provider_failure_positive(err_text: str) -> None:
    assert looks_like_provider_failure(err_text) is True


@pytest.mark.parametrize(
    "err_text",
    [
        "执行出错: 用户输入不合法",
        "执行出错: 内容审核拦截",
        "JSONDecodeError: Expecting value",
        "ValueError: invalid literal for int()",
        "成功",
    ],
)
def test_looks_like_provider_failure_negative(err_text: str) -> None:
    assert looks_like_provider_failure(err_text) is False
