"""AI 关闭时，onnxruntime / fastembed 的导入不得装进真实原生库；playwright 仍可用。"""

from __future__ import annotations

import os
import sys
import textwrap
import subprocess

import pytest

from gsuid_core.ai_core.heavy_import_gate import HeavyImportBlocked, install_ai_off_heavy_gate


def test_ai_off_gate_serves_placeholders_until_call() -> None:
    script = textwrap.dedent(
        """
        from gsuid_core.ai_core.heavy_import_gate import (
            HeavyImportBlocked,
            _BlockedCall,
            _BlockedModule,
            install_ai_off_heavy_gate,
        )

        assert install_ai_off_heavy_gate(force=True)
        import playwright
        from playwright.async_api import async_playwright
        import fastembed
        import onnxruntime

        assert not isinstance(playwright, _BlockedModule)
        assert not isinstance(async_playwright, _BlockedCall)
        assert isinstance(fastembed, _BlockedModule)
        assert isinstance(onnxruntime, _BlockedModule)
        try:
            fastembed.TextEmbedding()
        except HeavyImportBlocked:
            pass
        else:
            raise SystemExit("fastembed call should be blocked")
        print("ok")
        """
    )
    env = os.environ.copy()
    # 子进程固定 UTF-8，避免关闭 AI 时的中文日志把 stderr 读坏。
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


def test_gate_stays_off_when_not_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    """不强制时，AI 仍开启就不装闸。"""
    import gsuid_core.ai_core.heavy_import_gate as gate

    monkeypatch.setattr(gate, "_installed", False)

    class _Switch:
        data = True

    class _Config:
        def get_config(self, name: str) -> _Switch:
            assert name == "enable"
            return _Switch()

    import gsuid_core.ai_core.configs.ai_config as ai_mod

    monkeypatch.setattr(ai_mod, "ai_config", _Config())
    assert install_ai_off_heavy_gate() is False
    assert not any(type(finder).__name__ == "_HeavyGate" for finder in sys.meta_path)


def test_blocked_call_names_the_symbol() -> None:
    from gsuid_core.ai_core.heavy_import_gate import _BlockedCall

    call = _BlockedCall("onnxruntime.InferenceSession")
    try:
        call()
    except HeavyImportBlocked as exc:
        assert "InferenceSession" in str(exc)
    else:
        raise AssertionError("expected HeavyImportBlocked")
