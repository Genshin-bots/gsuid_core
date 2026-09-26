"""统一入口把子基准参数原样转发，不套 LongMem 的默认并发。"""

from eval.run_eval import delegated_command


def test_corpusqa_keeps_tag_and_answers_file() -> None:
    cmd = delegated_command(["run_eval.py", "corpusqa", "judge", "--tag", "orm", "--answers-file", "a.json"])
    assert cmd is not None
    assert cmd[-5:] == ["judge", "--tag", "orm", "--answers-file", "a.json"]
    assert "--concurrency" not in cmd
    assert "--base-url" not in cmd


def test_longmem_is_not_delegated() -> None:
    assert delegated_command(["run_eval.py", "longmem", "probe", "--concurrency", "12"]) is None
