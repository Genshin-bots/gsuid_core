"""LongMem judge 解析：首行必须是独立 PASS/FAIL，不能靠前缀。"""

from pathlib import Path

from eval.common.judge import parse_judge_response

_JUDGE_SRC = Path(__file__).resolve().parent.parent.joinpath("eval", "common", "judge.py").read_text(encoding="utf-8")


def test_parse_judge_first_line_is_full_token() -> None:
    assert parse_judge_response("PASS")["correct"] is True
    assert parse_judge_response("FAIL")["correct"] is False
    assert parse_judge_response("pass")["correct"] is True
    assert parse_judge_response("FAIL\n数字对了但日期错了")["correct"] is False


def test_parse_judge_does_not_eat_prefix_words() -> None:
    password = parse_judge_response("PASSWORD")
    assert password["correct"] is False
    assert "无法解析" in str(password["reason"])
    passed = parse_judge_response("PASSED")
    assert passed["correct"] is False
    assert "无法解析" in str(passed["reason"])
    failure = parse_judge_response("FAILURE")
    assert failure["correct"] is False
    assert "无法解析" in str(failure["reason"])


def test_parse_judge_json_fallback() -> None:
    ok = parse_judge_response('{"correct": true, "reason": "语义一致"}')
    assert ok["correct"] is True
    assert "语义一致" in str(ok["reason"])
    bad = parse_judge_response('{"correct": false, "reason": "缺专名"}')
    assert bad["correct"] is False


def test_judge_prompt_matches_as_judge_pass_fail() -> None:
    assert '"correct": true/false' not in _JUDGE_SRC
    assert "只输出单独一行：PASS 或 FAIL" in _JUDGE_SRC
