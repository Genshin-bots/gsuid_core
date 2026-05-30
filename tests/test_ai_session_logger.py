"""AISessionLogger 单元测试 —— 会话窗口续写/滚动、格式白名单、standalone 格式一致。

对应方案：plans/ai_session_log_simplification_20260529.md / docs/AI_SESSION_LOGGING.md

可直接运行：``python tests/test_ai_session_logger.py``（无 pytest 也能跑）。
"""

import os
import sys
import json
import time
import tempfile
from pathlib import Path

# logger 会打印含 emoji / 中文的日志，在 GBK 控制台下会触发 UnicodeEncodeError
# 噪声（与测试结果无关）。强制 UTF-8 输出消除噪声。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import gsuid_core.ai_core.session_logger as sl
from gsuid_core.ai_core.session_logger import (
    SESSION_ENTRY_TYPES,
    SESSION_WINDOW_SECONDS,
    AISessionLogger,
)

# _build_data 的固定顶层字段集合（格式契约）
_EXPECTED_FILE_KEYS = {
    "session_id",
    "session_uuid",
    "persona_name",
    "create_by",
    "is_subagent",
    "created_at",
    "updated_at",
    "ended_at",
    "entry_count",
    "entries",
    "linked_agents",
    "linked_agent_count",
}


def _patch_paths(tmp: Path):
    """把 main / subagent 日志目录重定向到临时目录（logger 按模块全局取路径）。"""
    main = tmp / "session_logs"
    sub = main / "subagents"
    main.mkdir(parents=True, exist_ok=True)
    sub.mkdir(parents=True, exist_ok=True)
    sl.AI_SESSION_LOGS_PATH = main
    sl.AI_SUBAGENT_LOGS_PATH = sub
    return main, sub


def test_window_resume_same_file():
    """窗口内：同一 session_id 第二次创建 → 续写同一文件、复用 session_uuid。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_paths(Path(d))
        sid = "Bot::g::u-resume"

        a = AISessionLogger(session_id=sid, create_by="Chat")
        a.log_user_input("hi")
        a.close()

        b = AISessionLogger(session_id=sid, create_by="Chat")
        assert b.session_uuid == a.session_uuid, "窗口内应复用 session_uuid"
        assert b._file_path == a._file_path, "窗口内应续写同一文件"
        types = [e["type"] for e in b.entries]
        assert "session_resumed" in types, "续写应记 session_resumed"
        assert "user_input" in types, "续写应保留历史 entry"
        b.close()


def test_window_rollover_new_file():
    """超窗口：最新文件 updated_at 距今 > 1h → 滚动到新文件、新 session_uuid。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_paths(Path(d))
        sid = "Bot::g::u-rollover"

        a = AISessionLogger(session_id=sid, create_by="Chat")
        a.log_user_input("old session")
        a.close()
        old_uuid = a.session_uuid
        old_path = a._file_path

        # 把磁盘文件 updated_at 改成 2 小时前，模拟会话超时关闭
        data = json.loads(old_path.read_text(encoding="utf-8"))
        data["updated_at"] = time.time() - (SESSION_WINDOW_SECONDS + 3600)
        old_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        c = AISessionLogger(session_id=sid, create_by="Chat")
        assert c.session_uuid != old_uuid, "超窗口应分配新 session_uuid"
        assert c._file_path != old_path, "超窗口应写新文件"
        assert "session_created" in [e["type"] for e in c.entries], "新文件应记 session_created"
        c.close()


def test_subagent_never_resumes():
    """subagent 永不续写：两次创建必是两个独立文件。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_paths(Path(d))
        sid = "sub-x"
        a = AISessionLogger(session_id=sid, create_by="SubAgent", is_subagent=True)
        a.close()
        b = AISessionLogger(session_id=sid, create_by="SubAgent", is_subagent=True)
        assert b.session_uuid != a.session_uuid, "subagent 不应复用 uuid"
        assert b._file_path != a._file_path, "subagent 应各自成文件"
        b.close()


def test_entry_whitelist_warns_but_records():
    """未登记 entry 类型：记 warning 但仍按统一结构落盘（不丢数据、不抛异常）。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_paths(Path(d))
        lg = AISessionLogger(session_id="Bot::g::u-wl", create_by="Chat")
        before = len(lg.entries)
        lg._add_entry("__definitely_not_registered__", {"x": 1})
        assert len(lg.entries) == before + 1, "未知类型也要落盘，不能丢数据"
        last = lg.entries[-1]
        assert set(last.keys()) == {"type", "timestamp", "data"}, "entry 结构必须统一"
        assert last["type"] == "__definitely_not_registered__"
        lg.close()
        # 所有正式 log_* 方法产出的类型都必须在白名单内
        for t in ("user_input", "tool_call", "proactive_emission", "session_created"):
            assert t in SESSION_ENTRY_TYPES


def test_standalone_proactive_format_matches():
    """log_standalone_proactive 产出的文件结构与活跃 session 完全一致。"""
    with tempfile.TemporaryDirectory() as d:
        main, _ = _patch_paths(Path(d))
        sid = "Bot::g::u-standalone"

        ok = AISessionLogger.log_standalone_proactive(
            session_id=sid,
            source="heartbeat",
            content="主动说一句",
            trigger_reason="mood:无聊",
            generator_log_files=[],
        )
        assert ok is True

        files = list(main.glob(f"{sid.replace(':', '_')}_*.json"))
        assert len(files) == 1, "standalone 应恰好写一个文件"
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert set(data.keys()) == _EXPECTED_FILE_KEYS, "顶层字段必须符合统一契约"
        etypes = [e["type"] for e in data["entries"]]
        assert "proactive_emission" in etypes
        emission = next(e for e in data["entries"] if e["type"] == "proactive_emission")
        assert set(emission["data"].keys()) == {
            "source",
            "content",
            "trigger_reason",
            "generator_log_files",
        }


def test_clean_old_logs_disabled_when_zero():
    """days=0 → 不清理（即使文件很旧）。"""
    with tempfile.TemporaryDirectory() as d:
        main, sub = _patch_paths(Path(d))
        a = main / "a_1_x.json"
        b = sub / "b_1_x.json"
        a.write_text("{}", encoding="utf-8")
        b.write_text("{}", encoding="utf-8")
        old = time.time() - 100 * 86400
        os.utime(a, (old, old))
        os.utime(b, (old, old))
        assert sl.clean_old_session_logs(0) == 0, "0 应不清理"
        assert a.exists() and b.exists(), "0 时旧文件也应保留"


def test_clean_old_logs_removes_old_keeps_recent():
    """days>0 → 删除超过 X 天的文件（main + subagents），保留近期文件。"""
    with tempfile.TemporaryDirectory() as d:
        main, sub = _patch_paths(Path(d))
        old_file = main / "old_1_x.json"
        recent_file = main / "recent_1_x.json"
        sub_old = sub / "subold_1_x.json"
        for f in (old_file, recent_file, sub_old):
            f.write_text("{}", encoding="utf-8")
        old = time.time() - 30 * 86400
        os.utime(old_file, (old, old))
        os.utime(sub_old, (old, old))  # recent_file 保持当前 mtime

        removed = sl.clean_old_session_logs(8)
        assert removed == 2, f"应删除 2 个旧文件，实际 {removed}"
        assert not old_file.exists() and not sub_old.exists()
        assert recent_file.exists(), "8 天内的文件应保留"


def test_system_prompt_logged_once_not_in_session_created():
    """system_prompt 只作为独立 entry 记录，不再塞进 session_created（消除重复）。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_paths(Path(d))
        lg = AISessionLogger(session_id="Bot::g::u-sp", create_by="Chat", system_prompt="你是X")
        created = next(e for e in lg.entries if e["type"] == "session_created")
        assert "system_prompt" not in created["data"], "session_created 不应再带 system_prompt"
        sp = [e for e in lg.entries if e["type"] == "system_prompt"]
        assert len(sp) == 1, f"system_prompt 应恰好一条，实际 {len(sp)}"
        assert sp[0]["data"]["content"] == "你是X"
        lg.close()


def test_no_system_prompt_entry_when_none():
    """未提供 system_prompt 时不产生 system_prompt entry（如 standalone 回退）。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_paths(Path(d))
        lg = AISessionLogger(session_id="Bot::g::u-nosp", create_by="Proactive_heartbeat")
        assert not any(e["type"] == "system_prompt" for e in lg.entries)
        created = next(e for e in lg.entries if e["type"] == "session_created")
        assert "system_prompt" not in created["data"]
        lg.close()


def test_run_markers_have_no_duplicated_payload():
    """run_start / run_end 是纯时间线标记，不再重复 user_input / result 的内容。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_paths(Path(d))
        lg = AISessionLogger(session_id="Bot::g::u-run", create_by="Chat")
        lg.log_run_start()
        lg.log_user_input("【用户发言】你好")
        lg.log_run_end()
        lg.log_result("最终答案", ["tool_a"])
        by = {e["type"]: e["data"] for e in lg.entries}
        assert by["run_start"] == {}, "run_start 应为空标记（无 user_message）"
        assert by["run_end"] == {}, "run_end 应为空标记（无 output）"
        assert by["user_input"]["content"] == "【用户发言】你好"
        assert by["result"]["output"] == "最终答案"
        assert by["result"]["tool_calls"] == ["tool_a"]
        lg.close()


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
