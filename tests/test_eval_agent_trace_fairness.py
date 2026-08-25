"""评测扫盘 / 生效工具口径：setup 枪不得盖住打分枪；闸门拒绝不算非法调用。"""

from __future__ import annotations

import json
from pathlib import Path

from eval.agent.runner import _prefer_log, _scan_all_logs
from eval.agent.harness import Trace, ToolCall, score_trace, score_case_passk, parse_session_log


def _doc(*, uid: str, updated_at: float, calls: list[str], text: str, extra_entries: list | None = None) -> dict:
    entries = [
        {"type": "session_created", "data": {"session_id": f"test_{uid}"}},
        {"type": "run_start", "data": {}},
        {"type": "user_input", "data": {"content": text}},
    ]
    for name in calls:
        entries.append({"type": "tool_call", "data": {"tool_name": name, "args": "{}", "tool_call_id": name}})
        ok = "✅ 任务已修改" if name.startswith("modify") else f"✅ {name} ok"
        entries.append({"type": "tool_return", "data": {"tool_name": name, "content": ok}})
    entries.append({"type": "result", "data": {"output": text}})
    entries.append({"type": "run_end", "data": {}})
    if extra_entries:
        entries.extend(extra_entries)
    return {
        "session_id": f"test_{uid}",
        "updated_at": updated_at,
        "created_at": updated_at,
        "entries": entries,
    }


def test_prefer_log_picks_later_complete_segment() -> None:
    setup = _doc(uid="u1", updated_at=100.0, calls=["add_once_task"], text="设好了七点")
    probe = _doc(
        uid="u1", updated_at=200.0, calls=["list_scheduled_tasks", "modify_scheduled_task"], text="改好了六点半"
    )
    chosen, _ = _prefer_log(setup, probe, 200.0)
    names = [e["data"]["tool_name"] for e in chosen["entries"] if e["type"] == "tool_call"]
    assert names == ["list_scheduled_tasks", "modify_scheduled_task"]


def test_scan_all_logs_does_not_keep_setup_over_probe(tmp_path: Path, monkeypatch) -> None:
    import eval.agent.runner as runner

    monkeypatch.setattr(runner, "SESSION_LOG_DIR", tmp_path)
    uid = "eval_c1_modify_after_create_0_abc123"
    setup = _doc(uid=uid, updated_at=10.0, calls=["add_once_task"], text="设好了七点")
    probe = _doc(uid=uid, updated_at=20.0, calls=["modify_scheduled_task"], text="改好了六点半")
    (tmp_path / f"test_{uid}_setup.json").write_text(json.dumps(setup, ensure_ascii=False), encoding="utf-8")
    (tmp_path / f"test_{uid}_probe.json").write_text(json.dumps(probe, ensure_ascii=False), encoding="utf-8")
    docs = _scan_all_logs({uid}, since=0.0)
    assert uid in docs
    tr = parse_session_log(docs[uid])
    assert tr.called_names == ["modify_scheduled_task"]
    assert "add_once_task" not in tr.called_names


def test_parse_session_log_uses_last_run_only() -> None:
    entries = [
        {"type": "run_start", "data": {}},
        {"type": "tool_call", "data": {"tool_name": "add_once_task", "args": "{}"}},
        {"type": "tool_return", "data": {"tool_name": "add_once_task", "content": "✅ 添加任务成功"}},
        {"type": "result", "data": {"output": "设好了"}},
        {"type": "run_start", "data": {}},
        {"type": "tool_call", "data": {"tool_name": "modify_scheduled_task", "args": "{}"}},
        {"type": "tool_return", "data": {"tool_name": "modify_scheduled_task", "content": "✅ 任务已修改"}},
        {"type": "result", "data": {"output": "改好了六点半"}},
    ]
    tr = parse_session_log({"entries": entries})
    assert tr.called_names == ["modify_scheduled_task"]
    assert "改好了" in tr.final_text
    assert "设好了" not in tr.final_text


def test_policy_reject_is_not_effectual() -> None:
    tr = Trace()
    tr.tool_calls = [
        ToolCall(name="add_interval_task", args={}, raw_args="{}"),
        ToolCall(name="modify_scheduled_task", args={}, raw_args="{}"),
    ]
    tr.tool_returns = [
        {"name": "add_interval_task", "content": "本轮是管理已有条目：请用查询/修改/取消，不要新建。"},
        {"name": "modify_scheduled_task", "content": "✅ 任务已修改"},
    ]
    ok, _ = score_trace(
        tr,
        {
            "must_call_any": ["modify_scheduled_task", "list_scheduled_tasks"],
            "must_not_call": ["add_interval_task", "add_once_task"],
        },
    )
    assert ok is True


def test_successful_add_still_fails_must_not_call() -> None:
    tr = Trace()
    tr.tool_calls = [ToolCall(name="add_once_task", args={}, raw_args="{}")]
    tr.tool_returns = [{"name": "add_once_task", "content": "✅ 添加任务成功"}]
    ok, reasons = score_trace(tr, {"must_not_call": ["add_once_task"]})
    assert ok is False
    assert any("illegally_called" in r for r in reasons)


def test_case_group_id_isolates_parallel_runs() -> None:
    from eval.agent.runner import _case_group_id

    case = {"id": "grp_silence_1", "targets": ["group-chat"]}
    a = _case_group_id(case, run_tag="eval_a")
    b = _case_group_id(case, run_tag="eval_b")
    assert a != b
    assert a is not None and a.startswith("eval_grp_grp_silence_1_")
    assert _case_group_id({"id": "priv_hi", "targets": ["persona"]}) is None
    assert _case_group_id({"group_id": "g1"}, run_tag="u9") == "g1_u9"


def test_strip_framework_user_leaks() -> None:
    from gsuid_core.ai_core.utils import strip_framework_user_leaks

    leaked = (
        "（这条是内部通道，不向用户解释。）上一条已经够短了。\n"
        "⚠️ 已达最大思考轮数，未能在限定步数内完成本任务。\n"
        "中间产物（如已写入的文件 / artifact）已留在工作区，未回传以避免刷屏。"
    )
    out = strip_framework_user_leaks(leaked)
    assert "内部通道" not in out
    assert "最大思考轮数" not in out
    assert "上一条已经够短了" in out
    env = '<control kind="correction">观察：x</control>\n改好了。'
    assert strip_framework_user_leaks(env) == "改好了。"


def test_usage_limit_return_payload_scopes_silence() -> None:
    from gsuid_core.ai_core.gs_agent import usage_limit_return_payload
    from gsuid_core.ai_core.capability_agents.runner import CAPABILITY_AGENT_ERROR_PREFIX

    assert (
        usage_limit_return_payload(create_by="TEST", is_subagent=False, delegated_render=False, image_sent=False)
        == "<SILENCE>"
    )
    cap = usage_limit_return_payload(
        create_by="CapabilityAgent", is_subagent=False, delegated_render=False, image_sent=False
    )
    assert cap.startswith(CAPABILITY_AGENT_ERROR_PREFIX)
    assert "<SILENCE>" not in cap
    silent_render = usage_limit_return_payload(
        create_by="CapabilityAgent", is_subagent=True, delegated_render=True, image_sent=False
    )
    assert silent_render == "<SILENCE>"


def test_pin_reject_is_not_effectual_write() -> None:
    from gsuid_core.ai_core.gs_agent import _tool_return_is_effectual_write

    assert _tool_return_is_effectual_write("add_interval_task", "✅ 添加任务成功", failed=False)
    assert not _tool_return_is_effectual_write(
        "add_interval_task",
        "本轮是管理已有条目：请用查询/修改/取消，不要新建。",
        failed=False,
    )
    assert not _tool_return_is_effectual_write("list_scheduled_tasks", "1. 喝水", failed=False)


def test_max_latency_does_not_fail_completed_overshoot() -> None:
    tr = Trace(final_text="早呀", returned_text="早呀", latency=43.6)
    ok, _ = score_trace(tr, {"max_latency": 40})
    assert ok is True
    hung = Trace(final_text="早呀", returned_text="早呀", latency=200.0)
    ok3, why = score_trace(hung, {"max_latency": 40})
    assert ok3 is False
    assert any("hang_bar" in x for x in why)


def test_judge_prompt_puts_tool_returns_before_reply() -> None:
    from eval.agent.harness import format_judge_prompt, _judge_tool_evidence

    tr = Trace(final_text="改好了。明天9点响。", returned_text="改好了。明天9点响。")
    tr.tool_calls = [ToolCall(name="modify_scheduled_task", args={}, raw_args="{}")]
    tr.tool_returns = [{"name": "modify_scheduled_task", "content": "✅ 任务已修改"}]
    ev = _judge_tool_evidence(tr)
    assert "modify_scheduled_task" in ev
    assert "任务已修改" in ev
    prompt = format_judge_prompt(
        "没调工具就说改好了=FAIL。调了modify=PASS",
        tr.content_text,
        tools_line=ev,
    )
    fact_at = prompt.find("框架记录的事实")
    reply_at = prompt.find("改好了。明天9点响。")
    assert 0 <= fact_at < reply_at
    assert "不得只凭回复出现" in prompt


def test_verbal_trap_judge_sees_effectual_modify() -> None:
    tr = Trace(final_text="改好了。明天9点响。", returned_text="改好了。明天9点响。")
    tr.tool_calls = [
        ToolCall(name="list_scheduled_tasks", args={}, raw_args="{}"),
        ToolCall(name="modify_scheduled_task", args={}, raw_args="{}"),
    ]
    tr.tool_returns = [
        {"name": "list_scheduled_tasks", "content": "1. 喝水 每天8点"},
        {"name": "modify_scheduled_task", "content": "✅ 任务已修改"},
    ]
    seen: list[str] = []

    def _judge(prompt: str) -> bool:
        seen.append(prompt)
        from eval.agent.harness import parse_judge_verdict

        if "modify_scheduled_task" in prompt and "任务已修改" in prompt:
            return True
        return bool(parse_judge_verdict("FAIL"))

    ok, _ = score_trace(
        tr,
        {
            "must_call_any": ["modify_scheduled_task", "list_scheduled_tasks"],
            "judge": {"rubric": "没调工具就说改好了=FAIL。调了modify=PASS"},
        },
        judge=_judge,
    )
    assert ok is True
    assert seen and "生效工具" in seen[0]


def test_score_case_passk_is_thread_safe() -> None:
    from concurrent.futures import ThreadPoolExecutor

    tr = Trace(final_text="ok", returned_text="ok")

    def _once(_: int) -> dict:
        return score_case_passk([tr], {"final_contains_any": ["ok"]})

    with ThreadPoolExecutor(max_workers=8) as pool:
        outs = list(pool.map(_once, range(16)))
    assert all(o["case_pass"] for o in outs)
