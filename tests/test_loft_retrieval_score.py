"""截断的 passage id 仍算召回。不导入会扫描进程的评测入口。"""

from eval.loft.score import retrieval_pass


def test_truncated_pid_json_still_counts_as_retrieved() -> None:
    ok, rec, _detail = retrieval_pass([["9433958", 1]], 'The answer is: ["9433958"')
    assert ok
    assert rec == 1.0
