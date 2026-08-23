from eval.agent.harness import parse_judge_verdict


def test_parse_judge_takes_last_token() -> None:
    assert parse_judge_verdict("拒绝=PASS。本次 FAIL") is False
    assert parse_judge_verdict("FAIL 因为没达到 PASS 标准\nPASS") is True


def test_parse_judge_strips_think() -> None:
    assert parse_judge_verdict("<think>应该 FAIL</think>\nPASS") is True


def test_parse_judge_no_verdict() -> None:
    assert parse_judge_verdict("") is None
    assert parse_judge_verdict("<SILENCE>") is None
    assert parse_judge_verdict("唔…看不懂") is None
