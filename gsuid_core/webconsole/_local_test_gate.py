"""本地测试守卫

评测侧 ``/api/chat_with_history`` 与 ``/api/ai/memory/batch_observe`` 默认 404，
仅当进程启动时设了 ``GSUID_LOCAL_TEST_MODE=1``（或 true/yes）才放行；可选
``GSUID_LOCAL_TEST_TOKEN`` + ``X-Local-Test-Token`` 请求头做第二道防线。
env var 在进程启动时读取，运行期改值不会生效（需重启 server 才安全）。
"""

from __future__ import annotations

import os

from fastapi import Header, Request, HTTPException, status

from gsuid_core.webconsole.web_api import require_auth

_LOCAL_TEST_MODE = os.getenv("GSUID_LOCAL_TEST_MODE", "").lower() in ("1", "true", "yes")
LOCAL_TEST_MODE = _LOCAL_TEST_MODE
_LOCAL_TEST_TOKEN = os.getenv("GSUID_LOCAL_TEST_TOKEN", "")


def _local_test_allowed(request: Request) -> bool:
    """local-test 模式是否放行本请求：模式开 + token（若配置）匹配。"""
    if not _LOCAL_TEST_MODE:
        return False
    if _LOCAL_TEST_TOKEN and request.headers.get("X-Local-Test-Token") != _LOCAL_TEST_TOKEN:
        return False
    return True


async def require_local_test(request: Request) -> None:
    """FastAPI Dependency：默认 404，开启 local-test 模式后透传。

    - 未开启：抛 404，避免生产环境被恶意触发评测/回灌端点；
    - 开启但配了 token：校验 ``X-Local-Test-Token`` 头，不匹配仍 404。
    """
    if not _local_test_allowed(request):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def require_auth_or_local_test(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = None,
):
    """放行条件二选一：① local-test 模式已开（token 若配置则需匹配）；② 携带有效
    web 控制台鉴权。供「评测要用、但生产仍需登录」的销毁/重建端点复用，避免评测客
    户端被 ``require_auth`` 挡 401 而静默失败，同时保持生产环境不裸奔。
    """
    if _local_test_allowed(request):
        return None
    return require_auth(authorization, token)
