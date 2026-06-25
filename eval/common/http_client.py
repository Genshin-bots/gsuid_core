"""异步 HTTP 客户端：封装 gsuid_core /api/* 调用。

评测脚本一般需要：

- :func:`call_chat_with_history`  —— 一次性传入 history + question 触发 Agent 回复；
- :func:`call_batch_observe`      —— 不走 Agent，仅把 turn 灌入记忆观测队列（BEAM-10M 专用）；
- :func:`call_clear_user_global`  —— 清空 user_global scope 的全部记忆；
- :func:`call_rebuild_hiergraph`  —— 触发分层图重建；
- :func:`call_send_msg`           —— 通过 /api/send_msg 发送私聊（用于 Judge 走 SendMsg 路径）；
- :func:`extract_text_from_response` —— 解析 Agent 返回的多种结构化文本。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx

# ─────────────────────────────────────────────
# 默认值
# ─────────────────────────────────────────────

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_CHAT_API = "/api/chat_with_history"
DEFAULT_SEND_API = "/api/send_msg"
DEFAULT_BATCH_OBSERVE_API = "/api/ai/memory/batch_observe"
DEFAULT_TIMEOUT = 4000.0  # 单次请求超时（秒），长对话可能需要较长时间

# 服务端若设了 GSUID_LOCAL_TEST_TOKEN，gate 端点需带此头；未设则为空、不影响请求
_LOCAL_TEST_TOKEN = os.getenv("GSUID_LOCAL_TEST_TOKEN", "")


def _auth_headers() -> Dict[str, str]:
    """评测请求统一附加的请求头：有 token 才带 ``X-Local-Test-Token``。"""
    return {"X-Local-Test-Token": _LOCAL_TEST_TOKEN} if _LOCAL_TEST_TOKEN else {}


# ─────────────────────────────────────────────
# Chat API
# ─────────────────────────────────────────────


async def call_chat_with_history(
    client: httpx.AsyncClient,
    base_url: str,
    user_id: str,
    message: str,
    history: List[Dict[str, str]],
    persona_name: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    enable_observer: Optional[bool] = None,
    enable_system2: Optional[bool] = None,
    bot_id: str = "HTTP",
    group_id: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 ``/api/chat_with_history`` 接口。

    请求体::

        {
            "user_id": ...,
            "message": ...,
            "history": [...],
            "persona_name": ...,
            "bot_id": ...,
            "group_id": ...,
            "enable_observer": ...,
            "enable_system2": ...,
        }

    返回::

        {"status_code": 200, "data": "...", "memory": "..."}
        {"status_code": -1, "data": None, "error": "timeout"}
    """
    url = f"{base_url}{DEFAULT_CHAT_API}"

    payload: Dict[str, Any] = {
        "user_id": user_id,
        "message": message,
        "history": history,
        "bot_id": bot_id,
        "group_id": group_id,
    }
    if persona_name:
        payload["persona_name"] = persona_name
    if enable_observer is not None:
        payload["enable_observer"] = enable_observer
    if enable_system2 is not None:
        payload["enable_system2"] = enable_system2

    try:
        response = await client.post(url, json=payload, headers=_auth_headers(), timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        print(f"  [WARN] 请求超时 ({timeout}s): {message[:50]}...")
        return {"status_code": -1, "data": None, "error": "timeout"}
    except httpx.HTTPStatusError as e:
        print(f"  [WARN] HTTP 错误 {e.response.status_code}: {message[:50]}...")
        return {"status_code": e.response.status_code, "data": None, "error": str(e)}
    except Exception as e:
        print(f"  [WARN] 请求异常: {e}")
        return {"status_code": -1, "data": None, "error": str(e)}


async def call_send_msg(
    client: httpx.AsyncClient,
    base_url: str,
    text: str,
    user_id: str = "eval_user",
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """调用 ``/api/send_msg`` 接口（用于评判阶段）。

    以私聊模式发送消息。
    """
    url = f"{base_url}{DEFAULT_SEND_API}"

    payload = {
        "bot_id": "eval",
        "bot_self_id": "eval_bot",
        "msg_id": "",
        "user_type": "direct",
        "group_id": None,
        "user_id": user_id,
        "sender": {},
        "user_pm": 3,
        "content": [{"type": "text", "data": text}],
    }

    try:
        response = await client.post(url, json=payload, headers=_auth_headers(), timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        print(f"  [WARN] 请求超时 ({timeout}s): {text[:50]}...")
        return {"status_code": -1, "data": None, "error": "timeout"}
    except httpx.HTTPStatusError as e:
        print(f"  [WARN] HTTP 错误 {e.response.status_code}: {text[:50]}...")
        return {"status_code": e.response.status_code, "data": None, "error": str(e)}
    except Exception as e:
        print(f"  [WARN] 请求异常: {e}")
        return {"status_code": -1, "data": None, "error": str(e)}


# ─────────────────────────────────────────────
# Memory API
# ─────────────────────────────────────────────


async def call_batch_observe(
    client: httpx.AsyncClient,
    base_url: str,
    user_id: str,
    turns: List[Dict[str, str]],
    scope_type: str = "user_global",
    group_id: Optional[str] = None,
    flush: bool = True,
    trigger_rebuild: bool = False,
    bot_self_id: Optional[str] = None,
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """调用 ``POST /api/ai/memory/batch_observe``。

    ``turns`` 每项形如 ``{"role": "user"|"assistant", "content": "...", "timestamp": "ISO8601 可选"}``。

    返回::

        {"status": 0, "msg": "ok", "data": {"observed": N, "dropped": 0, "scope_key": "...", "flush": true}}
        {"status": 1, "msg": "...", "data": None}
    """
    url = f"{base_url}{DEFAULT_BATCH_OBSERVE_API}"
    payload: Dict[str, Any] = {
        "user_id": user_id,
        "scope_type": scope_type,
        "group_id": group_id,
        "turns": turns,
        "flush": flush,
        "trigger_rebuild": trigger_rebuild,
    }
    if bot_self_id:
        payload["bot_self_id"] = bot_self_id

    try:
        response = await client.post(url, json=payload, headers=_auth_headers(), timeout=timeout)
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        return {"status": 1, "msg": f"batch_observe timeout ({timeout}s)", "data": None}
    except httpx.HTTPStatusError as e:
        return {"status": 1, "msg": f"batch_observe HTTP {e.response.status_code}: {str(e)}", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"batch_observe exception: {e}", "data": None}


async def call_clear_user_global(
    client: httpx.AsyncClient,
    base_url: str,
    user_id: str,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """调用 ``DELETE /api/ai/memory/users/{user_id}/global/clear`` 清空 user_global 记忆。"""
    url = f"{base_url}/api/ai/memory/users/{user_id}/global/clear"
    try:
        response = await client.request("DELETE", url, headers=_auth_headers(), timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"status": 1, "msg": f"clear_user_global 异常: {e}", "data": None}


async def call_rebuild_hiergraph(
    client: httpx.AsyncClient,
    base_url: str,
    scope_key: str,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """调用 ``POST /api/ai/memory/hiergraph/rebuild`` 触发分层图重建。

    端点把 ``scope_key`` 当 **query 参数**读取（非 body），故必须用 ``params`` 传。
    """
    url = f"{base_url}/api/ai/memory/hiergraph/rebuild"
    try:
        response = await client.post(url, params={"scope_key": scope_key}, headers=_auth_headers(), timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return {"status": 1, "msg": f"rebuild_hiergraph 异常: {e}", "data": None}


# ─────────────────────────────────────────────
# 响应解析
# ─────────────────────────────────────────────


def extract_text_from_response(response_data: Any) -> str:
    """从 HTTP 响应中提取 Agent 回复文本。

    - ``/api/chat_with_history`` 返回的 ``data`` 直接是字符串；
    - ``/api/send_msg`` 返回的 ``data`` 可能是 MessageSend 格式（含 content 列表）。
    """
    if response_data is None:
        return ""

    if isinstance(response_data, str):
        return response_data.strip()

    if isinstance(response_data, dict):
        content = response_data.get("content")
        if content and isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    data = item.get("data", "")
                    if data:
                        texts.append(str(data))
            return "\n".join(texts).strip()

        if "data" in response_data:
            return extract_text_from_response(response_data["data"])

    if isinstance(response_data, list):
        texts = []
        for item in response_data:
            text = extract_text_from_response(item)
            if text:
                texts.append(text)
        return "\n".join(texts).strip()

    return str(response_data).strip()
