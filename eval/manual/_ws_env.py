"""eval/manual 脚本共用：token 必填、出图目录在仓库根 test_output/。"""

from __future__ import annotations

import os
import socket
import asyncio
from pathlib import Path

import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "test_output"
SESSION_LOG_DIR = REPO_ROOT / "data" / "ai_core" / "session_logs"


class UvicornReadyServer(uvicorn.Server):
    """startup 结束后才 set Event。lifespan 触发时端口还没 bind。"""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await super().startup(sockets=sockets)
        finally:
            self.ready.set()


async def start_uvicorn(config: uvicorn.Config) -> tuple[UvicornReadyServer, asyncio.Task[None]]:
    server = UvicornReadyServer(config)
    task = asyncio.create_task(server.serve())
    await server.ready.wait()
    return server, task


def require_token() -> str:
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    if not token:
        raise SystemExit("GSUID_LOCAL_TEST_TOKEN is required (no fallback)")
    return token


def ws_url() -> str:
    return f"ws://localhost:8765/ws/Nonebot?token={require_token()}"
