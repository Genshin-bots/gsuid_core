"""A rate-limited run must be rejected, not scored.

Regression lock for the round-8 failure: upstream returned HTTP 200 wrapped
refusals ("稍等，这会儿不太方便，稍后再试。"), 700/700 status 200, median answer
16 chars — and the harness accepted the whole thing as a valid 3.1% result.
Two guards now exist and both are asserted here.
"""

from __future__ import annotations

import sys
import json
from pathlib import Path

import pytest

# 仓库根按 __file__ 推导，不写死某台机器的绝对路径（换 checkout 就不成立了）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_recorded_inject_ids_omits_a_body_that_never_sent_them() -> None:
    from eval.common.beam_runner import _recorded_inject_ids

    assert _recorded_inject_ids({}) is None
    assert _recorded_inject_ids({"inject_ids": "e1"}) is None
    assert _recorded_inject_ids({"inject_ids": []}) == []
    assert _recorded_inject_ids({"inject_ids": ["e1", 2]}) == ["e1", "2"]


def test_eo_l2_skips_answers_that_never_recorded_a_pack() -> None:
    from eval.BEAM_official.run_official import _mapped_eo_l2

    gold = ["e1", "e2"]
    assert _mapped_eo_l2({"category": "event_ordering"}, gold) is None
    assert _mapped_eo_l2({"inject_ids": []}, gold) == 0.0
    assert _mapped_eo_l2({"inject_ids": ["e2", "e9"]}, gold) == 0.5


def test_provider_overloaded_catches_chinese_refusal() -> None:
    from eval.common.beam_runner import _provider_overloaded

    assert _provider_overloaded(200, "稍等，这会儿不太方便，稍后再试。")
    assert _provider_overloaded(200, "请稍后重试")
    assert _provider_overloaded(200, "服务繁忙")
    assert _provider_overloaded(200, "I'm overloaded, try again later")
    # 配额耗尽：框架错误前缀裹在 200 里，实测整轮 700 题全是这句
    assert _provider_overloaded(200, "执行出错: 模型套餐用量已达上限")
    assert _provider_overloaded(429, "anything")
    assert not _provider_overloaded(200, "你一共加过 8 样东西：headphones、whiteboard 等。")
    # 短回答里的客套字必须整句才算拒答，否则「稍等我核对」会把整轮判失败。
    assert not _provider_overloaded(200, "你稍等，我把 headphones 和 whiteboard 对完，一共 8 样。")


def test_provider_overloaded_ignores_weak_word_inside_long_answer() -> None:
    """裸 "overloaded" 是常见英文单词，长答卷里出现它不算拒答。

    实测 conv 25 的 preference_following__1（3277 字好答案）里含 "nobody's overloaded"，
    被旧词表当成拒答；guard 要求 bad==0，于是一处误杀直接中断整轮，conv 26-34 全没跑。
    """
    from eval.common.beam_runner import _provider_overloaded

    good = (
        "Here are some approaches for managing your team.\n\n"
        + ("Define each layer cleanly. " * 200)
        + "Nobody's overloaded."
    )
    assert len(good) > 400
    assert not _provider_overloaded(200, good), "长答卷里的普通用词不该被当成拒答"
    # 短文本里的裸 overloaded 仍必须抓到：真过载回复就那么几句
    assert _provider_overloaded(200, "overloaded")
    # 固定搭配不挑长度，长文本里出现也要抓
    assert _provider_overloaded(200, "x" * 900 + " service overloaded")


def test_answers_sane_rejects_degraded_run(tmp_path: Path) -> None:
    from eval.BEAM_official.run_official import _answers_sane

    ok_rows = [{"question_id": f"q{i}", "status_code": 200, "agent_answer": "答案" + "内容详实" * 80} for i in range(6)]
    ok_path = tmp_path / "ok.json"
    ok_path.write_text(json.dumps(ok_rows, ensure_ascii=False), encoding="utf-8")
    assert _answers_sane(str(ok_path), 6), "正常长度的答卷不该被拒"

    bad_rows = [
        {"question_id": f"q{i}", "status_code": 200, "agent_answer": "稍等，这会儿不太方便，稍后再试。"}
        for i in range(6)
    ]
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad_rows, ensure_ascii=False), encoding="utf-8")
    assert not _answers_sane(str(bad_path), 6), "整轮降级必须被拒，不能当成有效数据判分"


def test_answers_sane_rejects_short_answers_even_without_known_phrases(tmp_path: Path) -> None:
    """没枚举到的降级形态也要拦——整轮长度中位数是与话术表无关的兜底信号。"""
    from eval.BEAM_official.run_official import _answers_sane

    rows = [
        {"question_id": f"q{i}", "status_code": 200, "agent_answer": "Port 4000." if i % 2 else "4 days."}
        for i in range(6)
    ]
    # 用 tmp_path 而非系统临时目录 + 固定文件名：固定名会与并行跑互相覆盖，且不回收。
    p = tmp_path / "short.json"
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    assert not _answers_sane(str(p), 6)


def test_answers_sane_rejects_long_quota_error_envelope(tmp_path: Path) -> None:
    """配额错误带上重试说明就会超过长度下限，只靠中位数会收下整轮垃圾。

    2026-10-04 实测：conv 24 被拒是靠 16 字符低于 200 的长度信号，纯属侥幸。
    词表必须复用 _provider_overloaded，不能只靠长度。
    """
    from eval.BEAM_official.run_official import _answers_sane

    envelope = "执行出错: 模型套餐用量已达上限。请在 24 小时后重试，或联系管理员升级套餐。" + "详情见控制台。" * 40
    assert len(envelope) > 200
    rows = [{"question_id": f"q{i}", "status_code": 200, "agent_answer": envelope} for i in range(6)]
    p = tmp_path / "quota.json"
    p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    assert not _answers_sane(str(p), 6), "长配额错误也必须被拒，不能只靠长度下限"


def test_preflight_does_not_misreport_healthy_core_as_gate_closed() -> None:
    """core 默认关闭 openapi schema，旧预检永远把正常服务误报成「gate 没开」。

    真实 core 上 GET /api/chat_with_history 返 405（路由在、只有 POST）、
    /openapi.json 返 404。旧逻辑查 openapi，于是每轮都打假告警，
    排查时会据此怀疑服务没起对，方向直接跑偏。
    """
    from eval.common.beam_runner import _preflight_verdict

    healthy = _preflight_verdict(405, 200)
    assert healthy == "服务在线且 local-test gate 已开"
    assert "gate 未开" not in healthy
    assert "未注册" not in healthy


def test_preflight_separates_missing_route_from_closed_gate() -> None:
    """服务没起（路由 404）与 gate 没开（端点存在但 404）是两回事，不能混报。"""
    from eval.common.beam_runner import _preflight_verdict

    no_route = _preflight_verdict(404, None)
    assert "未注册" in no_route
    assert "gate 未开" not in no_route

    gate_closed = _preflight_verdict(405, 404)
    assert "gate 未开" in gate_closed
    assert "未注册" not in gate_closed


def test_force_utf8_stdio_survives_unencodable_answer_text() -> None:
    """答案含 GBK 编不了的字符时，打印不能把整轮跑崩。

    2026-10-03 实测：一条含 ² 的答案在 beam_runner 的预览打印处抛
    UnicodeEncodeError，70 题的 A/B 臂跑到一半全废。
    """
    import io

    from eval.common.beam_runner import force_utf8_stdio

    buf = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = buf
        sys.stderr = buf
        force_utf8_stdio()
        print("面积约 12 m²（1㎡）🎉")
        # 必须在还原前断言 buf：finally 先跑的话读到的是真正的 stdout，断言恒真。
        assert buf.encoding.lower().replace("-", "") == "utf8", f"没换到 utf8：{buf.encoding}"
        assert buf.errors == "replace", f"还要能替换掉编不了的字符，实得 {buf.errors}"
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def test_run_lock_refuses_a_second_live_writer(tmp_path: Path) -> None:
    """结果目录是单写者资源：cron 派生 4 个会话同时跑批，必须第二个被拒。

    2026-10-03 实测：4 个会话并发写 results/1m，judge_*.json 互相覆盖，
    conv 34 被 trash 掉，数字看起来仍然合理差点被采信。
    """
    from eval.common import run_lock

    first = run_lock.acquire(tmp_path, "1m")
    assert first is not None
    try:
        assert run_lock.acquire(tmp_path, "1m") is None, "活锁必须挡住第二个写者"
    finally:
        run_lock.release(first)
    again = run_lock.acquire(tmp_path, "1m")
    assert again is not None, "释放后必须能重新拿锁"
    run_lock.release(again)


def test_run_lock_takes_over_stale_and_recycled_pid(tmp_path: Path) -> None:
    """没人握着句柄时，留下的 pid 文件不能堵死下一次跑批。"""
    from eval.common import run_lock

    lock = tmp_path / ".1m.lock"
    # pid 不存在 -> 陈旧
    lock.write_text(json.dumps({"pid": 999999, "created_at": 1.0, "acquired_at": 1.0}), encoding="utf-8")
    holder = run_lock.acquire(tmp_path, "1m")
    assert holder is not None, "死进程留下的锁必须能被接管"
    run_lock.release(holder)

    # pid 活着但 create_time 对不上 -> pid 回收
    import psutil

    lock.write_text(json.dumps({"pid": psutil.Process().pid, "created_at": 1.0, "acquired_at": 1.0}), encoding="utf-8")
    holder2 = run_lock.acquire(tmp_path, "1m")
    assert holder2 is not None, "pid 回收后的锁必须被当成陈旧"
    run_lock.release(holder2)


def test_scale_writer_holds_ladder_lock_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """all/report 必须抢同一把天梯锁。只锁档目录时，半份总分会被收成完整成绩。"""
    import eval.BEAM_official.run_official as ro

    monkeypatch.setattr(ro, "_ROOT", str(tmp_path))
    spec = ro.SCALES["100k"]
    with ro.results_writer(spec):
        assert ro.run_lock.acquire(tmp_path, "ladder") is None
        assert ro.run_lock.acquire(Path(ro._out_dir(spec)), spec.key) is None
    again = ro.run_lock.acquire(tmp_path, "ladder")
    assert again is not None
    ro.run_lock.release(again)


def test_run_lock_ignores_corrupt_lock_file(tmp_path: Path) -> None:
    """锁文件被截断/写坏时按陈旧处理，不能永久堵死跑批。"""
    from eval.common import run_lock

    (tmp_path / ".1m.lock").write_text("{not json", encoding="utf-8")
    holder = run_lock.acquire(tmp_path, "1m")
    assert holder is not None
    run_lock.release(holder)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
