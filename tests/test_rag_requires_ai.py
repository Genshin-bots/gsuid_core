"""AI 关闭时，知识库导入得到空壳，并打一条警告。"""

from __future__ import annotations

import os
import sys
import textwrap
import subprocess


def test_knowledge_import_is_a_shell_when_ai_disabled() -> None:
    script = textwrap.dedent(
        """
        import asyncio

        from gsuid_core.ai_core.configs.ai_config import ai_config

        ai_config.get_config("enable").data = False
        from gsuid_core.ai_core.rag import query_knowledge
        import gsuid_core.ai_core.rag.knowledge as knowledge

        assert knowledge.__file__ == "<ai-off-rag>"
        assert "fastembed" not in __import__("sys").modules
        points = asyncio.run(query_knowledge(query="x", limit=1))
        assert points == []
        from gsuid_core.ai_core.rag.qmodels import Filter
        assert Filter is not None
        print("ok")
        """
    )
    env = os.environ.copy()
    # 子进程固定 UTF-8，否则中文警告在 GBK 控制台下无法按 utf-8 读回。
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout
    combined = completed.stdout + completed.stderr
    assert "不会实际执行导入知识库" in combined
