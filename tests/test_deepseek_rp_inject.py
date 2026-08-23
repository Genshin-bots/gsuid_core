"""enable_deepseek_rp：marker 必须挂在会话第一条真人 user message 末尾并随 history 持久。"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)

from gsuid_core.ai_core.utils import _relean_user_turn, compact_session_history
from gsuid_core.ai_core.persona.prompts import INNER_OS_MARKER
from gsuid_core.ai_core.agent_run.support import _ensure_inner_os_on_first_user

_ROOT = Path(__file__).resolve().parent.parent
MARKER = INNER_OS_MARKER


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _asst(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


def test_first_turn_appends_to_current_and_lean() -> None:
    current = "[用户发言]\n你好"
    lean = "[用户发言]\n你好"
    new_cur, new_lean, where = _ensure_inner_os_on_first_user(
        [],
        current,
        lean,
        MARKER,
        is_framework=False,
    )
    assert where == "current"
    assert isinstance(new_cur, str) and new_cur.endswith(MARKER)
    assert new_cur.startswith("[用户发言]\n你好")
    assert isinstance(new_lean, str) and new_lean.endswith(MARKER)
    assert MARKER not in current


def test_relean_keeps_marker_when_lean_has_it() -> None:
    full = "[用户发言]\n你好\n\n[历史对话]\n一大段" + MARKER
    lean = "[用户发言]\n你好" + MARKER
    msgs = [_user(full), _asst("在")]
    _relean_user_turn(msgs, lean)
    up = msgs[0].parts[0]
    assert isinstance(up, UserPromptPart)
    assert up.content == full
    assert MARKER in str(up.content)


def test_later_turn_appends_to_current_without_mutating_history() -> None:
    history = [
        _user("[用户发言]\n第一句"),
        _asst("回"),
    ]
    current = "[用户发言]\n第二句"
    lean = "[用户发言]\n第二句"
    new_cur, new_lean, where = _ensure_inner_os_on_first_user(
        history,
        current,
        lean,
        MARKER,
        is_framework=False,
    )
    assert where == "current"
    assert isinstance(new_cur, str) and new_cur.endswith(MARKER)
    assert isinstance(new_lean, str) and new_lean.endswith(MARKER)
    first = history[0].parts[0]
    assert isinstance(first, UserPromptPart)
    assert MARKER not in str(first.content)


def test_legacy_marker_title_is_treated_as_already_present() -> None:
    old = "[用户发言]\n第一句\n\n【角色沉浸要求】在你的思考过程（<arg_key>标签内）中，请遵守以下规则："
    history = [_user(old), _asst("回")]
    current = "[用户发言]\n第二句"
    _, _, where = _ensure_inner_os_on_first_user(
        history,
        current,
        current,
        MARKER,
        is_framework=False,
    )
    assert where == "already"
    first = history[0].parts[0]
    assert isinstance(first, UserPromptPart)
    assert first.content == old


def test_already_present_on_first_history_user_is_idempotent() -> None:
    history = [
        _user("[用户发言]\n第一句" + MARKER),
        _asst("回"),
    ]
    current = "[用户发言]\n第二句"
    _, _, where = _ensure_inner_os_on_first_user(
        history,
        current,
        current,
        MARKER,
        is_framework=False,
    )
    assert where == "already"
    first = history[0].parts[0]
    assert isinstance(first, UserPromptPart) and isinstance(first.content, str)
    assert first.content.count("【角色沉浸要求】") == 1


def test_skips_framework_history_and_injects_first_real_user() -> None:
    history = [
        _user("[框架·任务完成]\n交付"),
        _asst("好"),
        _user("[用户发言]\n真人第一句"),
        _asst("嗯"),
    ]
    current = "[用户发言]\n本轮"
    _, _, where = _ensure_inner_os_on_first_user(
        history,
        current,
        current,
        MARKER,
        is_framework=False,
    )
    assert where == "current"
    fw = history[0].parts[0]
    real = history[2].parts[0]
    assert isinstance(fw, UserPromptPart) and MARKER not in str(fw.content)
    assert isinstance(real, UserPromptPart) and MARKER not in str(real.content)
    assert isinstance(current, str)


def test_framework_current_turn_without_history_user_is_skipped() -> None:
    current = "[框架·任务完成]\n交付"
    new_cur, new_lean, where = _ensure_inner_os_on_first_user(
        [_asst("proactive")],
        current,
        "",
        MARKER,
        is_framework=True,
    )
    assert where == "skipped"
    assert new_cur == current
    assert new_lean == ""


def test_proactive_only_history_injects_into_first_real_current() -> None:
    current = "[用户发言]\n你好"
    lean = "[用户发言]\n你好"
    new_cur, new_lean, where = _ensure_inner_os_on_first_user(
        [_asst("先说一句")],
        current,
        lean,
        MARKER,
        is_framework=False,
    )
    assert where == "current"
    assert isinstance(new_cur, str) and new_cur.endswith(MARKER)
    assert isinstance(new_lean, str) and new_lean.endswith(MARKER)


def test_multimodal_first_turn_appends_marker_as_text_part() -> None:
    current: list = ["[用户发言]\n看这张图", ImageUrl(url="data:image/png;base64,xx")]
    lean: list = ["[用户发言]\n看这张图", ImageUrl(url="data:image/png;base64,xx")]
    new_cur, new_lean, where = _ensure_inner_os_on_first_user(
        [],
        current,
        lean,
        MARKER,
        is_framework=False,
    )
    assert where == "current"
    assert isinstance(new_cur, list) and new_cur[-1] == MARKER
    assert isinstance(new_lean, list) and new_lean[-1] == MARKER
    assert MARKER not in current


def test_marker_text_matches_official_instruct() -> None:
    prompts = (_ROOT / "gsuid_core/ai_core/persona/prompts.py").read_text(encoding="utf-8")
    assert "INNER_OS_MARKER" in prompts
    assert "<arg_key>" not in prompts
    assert "【角色沉浸要求】" in prompts
    assert "（心想：" not in INNER_OS_MARKER
    assert "(内心OS" not in INNER_OS_MARKER


def test_compact_keeps_marker_on_first_user() -> None:
    history: list = [_user("[用户发言]\n第一句" + MARKER), _asst("回")]
    for i in range(20):
        history.extend([_user(f"[用户发言]\n后{i}"), _asst(f"答{i}")])
    out, did = compact_session_history(history, max_history=20, trim_ratio=0.6)
    assert did is True
    first = out[0].parts[0]
    assert isinstance(first, UserPromptPart)
    assert isinstance(first.content, str) and first.content.endswith(MARKER)
    current = "[用户发言]\n本轮"
    _, _, where = _ensure_inner_os_on_first_user(out, current, current, MARKER, is_framework=False)
    assert where == "already"
    assert MARKER not in current


def test_prepare_and_compact_both_pin_first_user() -> None:
    prepare = (_ROOT / "gsuid_core/ai_core/agent_run/prepare.py").read_text(encoding="utf-8")
    tools = (_ROOT / "gsuid_core/ai_core/agent_run/tools.py").read_text(encoding="utf-8")
    registry = (_ROOT / "gsuid_core/ai_core/session_registry.py").read_text(encoding="utf-8")
    fn = prepare.split("async def _run_once_prepare_user_message")[1].split("    def _inject")[0]
    assert "_inject_deepseek_rp_marker" in fn
    assert "not self.history" not in fn
    assert fn.index("scaffold_hints_from_graph") < fn.index("_inject_deepseek_rp_marker")
    assert fn.index("log_user_input") > fn.index("_inject_deepseek_rp_marker")
    compact_at = tools.index("self.extract_history()")
    pin_at = tools.index("self._inject_deepseek_rp_marker(st)")
    assert compact_at < pin_at
    assert "history[-self.MAX_AI_HISTORY_LENGTH :]" not in registry
    assert "compact_session_history" in registry
