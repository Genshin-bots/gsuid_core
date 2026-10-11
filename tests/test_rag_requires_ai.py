"""AI 关闭时，知识库导入得到空壳，并打一条警告。"""

from __future__ import annotations

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
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout
    combined = completed.stdout + completed.stderr
    assert "不会实际执行导入知识库" in combined
