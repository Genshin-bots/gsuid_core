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


def test_silence_is_transient_judge_failure() -> None:
    from eval.common.judge import _is_transient_judge_failure

    assert _is_transient_judge_failure(200, "<SILENCE>") is True
    assert _is_transient_judge_failure(200, "SILENCE") is True
    assert _is_transient_judge_failure(200, "PASS") is False


def test_judge_silence_is_not_gold_string_pass() -> None:
    src = Path(__file__).resolve().parent.parent.joinpath("eval", "common", "judge.py").read_text(encoding="utf-8")
    assert "gold string in answer" not in src
    from eval.common.judge import simple_string_match

    assert simple_string_match("Sound effects", "27. **Sound effects** (e.g., ambient)") is True
    assert simple_string_match("Manolo García", "Marina Rossell was the example") is False
    assert simple_string_match(3, "You attended 3 workshops") is True
    assert simple_string_match(3, "13 workshops") is False
    assert simple_string_match(5, "15 days") is False
    assert simple_string_match(15, "5 days in NYC only") is False


def test_as_judge_allows_more_than_one_iteration() -> None:
    src = (
        Path(__file__)
        .resolve()
        .parent.parent.joinpath("gsuid_core", "webconsole", "chat_with_history_api.py")
        .read_text(encoding="utf-8")
    )
    assert "max_iterations=4 if req.as_judge" in src
