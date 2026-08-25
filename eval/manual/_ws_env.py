"""eval/manual 脚本共用：token 必填、出图目录在仓库根 test_output/。"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "test_output"
SESSION_LOG_DIR = REPO_ROOT / "data" / "ai_core" / "session_logs"


def require_token() -> str:
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    if not token:
        raise SystemExit("GSUID_LOCAL_TEST_TOKEN is required (no fallback)")
    return token


def ws_url() -> str:
    return f"ws://localhost:8765/ws/Nonebot?token={require_token()}"
