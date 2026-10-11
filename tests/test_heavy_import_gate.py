"""AI 关闭时，onnxruntime / fastembed / playwright 的导入不得装进真实原生库。"""

from __future__ import annotations

import sys
import textwrap
import subprocess

import pytest

from gsuid_core.ai_core.heavy_import_gate import HeavyImportBlocked, install_ai_off_heavy_gate


def test_ai_off_gate_serves_placeholders_until_call() -> None:
    script = textwrap.dedent(
        """
        import sys
        from gsuid_core.ai_core.heavy_import_gate import HeavyImportBlocked, _BlockedModule, install_ai_off_heavy_gate

        assert install_ai_off_heavy_gate(force=True)
        import playwright
        from playwright.async_api import async_playwright
        import fastembed
        import onnxruntime

        assert isinstance(playwright, _BlockedModule)
        assert isinstance(fastembed, _BlockedModule)
        assert isinstance(onnxruntime, _BlockedModule)
        try:
            async_playwright()
        except HeavyImportBlocked:
            pass
        else:
            raise SystemExit("call should be blocked")
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

    call = _BlockedCall("playwright.async_api.async_playwright")
    try:
        call()
    except HeavyImportBlocked as exc:
        assert "async_playwright" in str(exc)
    else:
        raise AssertionError("expected HeavyImportBlocked")
