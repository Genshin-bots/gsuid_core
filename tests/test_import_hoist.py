"""提前导入之后：同一对象、导入不环，handler 仍不拉起 Agent 运行时。"""

from __future__ import annotations

import sys
import subprocess


def test_hoisted_callables_are_the_module_objects() -> None:
    from gsuid_core.bot import capture_outbound
    from gsuid_core.ai_core.utils import is_silence_marker
    from gsuid_core.ai_core.handle_ai import (
        record_soft_reply as turn_soft_reply,
        record_gate_outcome as turn_gate_outcome,
        record_gate_silence as turn_gate_silence,
        revoke_fresh_hard_trigger as turn_revoke,
    )
    from gsuid_core.ai_core.hooks.points import HOOK_POINT_SPECS
    from gsuid_core.ai_core.hooks.dispatch import HOOK_POINT_SPECS as dispatch_specs
    from gsuid_core.ai_core.followup_window import (
        record_soft_reply,
        record_gate_outcome,
        record_gate_silence,
        revoke_fresh_hard_trigger,
    )
    from gsuid_core.ai_core.command_aside.runner import is_silence_marker as runner_silence
    from gsuid_core.ai_core.command_aside.capture import capture_outbound as capture_fn

    assert turn_gate_silence is record_gate_silence
    assert turn_gate_outcome is record_gate_outcome
    assert turn_soft_reply is record_soft_reply
    assert turn_revoke is revoke_fresh_hard_trigger
    assert runner_silence is is_silence_marker
    assert dispatch_specs is HOOK_POINT_SPECS
    assert capture_outbound is capture_fn


def test_moved_modules_import_without_cycle() -> None:
    import gsuid_core.bot
    import gsuid_core.handler
    import gsuid_core.ai_core.handle_ai
    import gsuid_core.ai_core.hooks.dispatch
    import gsuid_core.ai_core.followup_window as followup_window
    import gsuid_core.ai_core.heartbeat.decision
    import gsuid_core.ai_core.command_aside.runner
    from gsuid_core.ai_core.command_aside import command_with_aside

    assert gsuid_core.handler.note_hard_trigger is followup_window.note_hard_trigger
    assert command_with_aside is gsuid_core.ai_core.command_aside.runner.command_with_aside


def test_handler_import_does_not_load_agent_runtime() -> None:
    script = """
import sys
import gsuid_core.handler
banned = (
    "gsuid_core.ai_core.handle_ai",
    "gsuid_core.ai_core.gs_agent",
    "gsuid_core.ai_core.command_aside.runner",
    "gsuid_core.ai_core.statistics",
    "gsuid_core.ai_core.startup",
)
loaded = [name for name in banned if name in sys.modules]
if loaded:
    raise SystemExit("loaded " + ",".join(loaded))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
