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
    assert _is_transient_judge_failure(200, "PASS") is False


def test_passed_is_derived_from_rubric_not_taken_from_the_judge() -> None:
    """裁判说 PASS 但 rubric 没全中时必须判 FAIL。

    旧实现直接采信 parsed["passed"]，而提示词里还有「或语义一致」的逃生门。
    实测 1470 条判分里有 67 条是这种幽灵 PASS，summarization 一域就占 9 条——
    那一域的分数有一半对不上任何一条全中的 rubric。
    """
    from eval.common.judge import parse_beam_judge_response

    rubric = ["r1", "r2", "r3"]
    ghost = '{"rubric_scores": [1, 0, 1], "passed": true, "reason": "核心事实一致"}'
    assert parse_beam_judge_response(ghost, rubric)["passed"] is False

    honest = '{"rubric_scores": [1, 0, 1], "passed": false, "reason": "缺 r2"}'
    assert parse_beam_judge_response(honest, rubric)["passed"] is False

    all_hit = '{"rubric_scores": [1, 1, 1], "passed": false, "reason": "整体写得一般"}'
    assert parse_beam_judge_response(all_hit, rubric)["passed"] is True


def test_passed_is_false_when_rubric_is_empty() -> None:
    from eval.common.judge import parse_beam_judge_response

    empty = '{"rubric_scores": [], "passed": true, "reason": "看起来没问题"}'
    assert parse_beam_judge_response(empty, [])["passed"] is False


def test_beam_prompt_has_no_semantic_escape_hatch() -> None:
    from eval.common.judge import _BEAM_JUDGE_PROMPT

    assert "rubric 检查点全部命中，**或**" not in _BEAM_JUDGE_PROMPT
    assert "不接受" in _BEAM_JUDGE_PROMPT


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


def test_beam_judge_uses_as_judge_and_retries_ooc() -> None:
    from eval.common.judge import _is_transient_judge_failure

    start = _JUDGE_SRC.find("async def judge_beam_single")
    chunk = _JUDGE_SRC[start : start + 2500]
    assert "as_judge=True" in chunk
    assert "_JUDGE_MAX_RETRIES" in chunk
    assert _is_transient_judge_failure(200, "这个不太想说呢。") is True
    assert _is_transient_judge_failure(200, '{"passed": true, "rubric_scores": [1]}') is False


def test_kendall_tau_b_perfect_and_reversed() -> None:
    from eval.common.judge import kendall_tau_b, parse_align_list, order_metrics_from_align

    assert kendall_tau_b([1, 2, 3], [1, 2, 3]) == 1.0
    assert kendall_tau_b([1, 2, 3], [3, 2, 1]) == -1.0
    assert kendall_tau_b([1], [1]) is None
    cov, tau = order_metrics_from_align([1, 2, 3])
    assert cov == 1.0
    assert tau == 1.0
    cov_partial, tau_partial = order_metrics_from_align([1, None, 2])
    assert cov_partial == 2 / 3
    assert tau_partial is None
    assert parse_align_list([1, "2", None, 0, True], 5) == [1, 2, None, None, None]
    from eval.common.judge import official_tau_from_align

    assert official_tau_from_align([1, 2, 3]) == 1.0
    assert official_tau_from_align([1, None, 3]) == 0.0
    assert official_tau_from_align([3, 2, 1]) == -1.0
    from eval.common.judge import parse_eq_pair_lines, should_double_judge, align_from_eq_matrix

    mat = parse_eq_pair_lines("PAIR 1-1 YES\nPAIR 1-2 NO\nPAIR 2-2 YES", 2, 2)
    assert mat[0][0] is True
    assert mat[1][1] is True
    assert align_from_eq_matrix(mat) == [1, 2]
    assert should_double_judge("beam_off_100k_17__event_ordering__0") is True
    assert should_double_judge("beam_off_100k_2__event_ordering__0") is False
    from eval.common.judge import majority_align, pair_line_stats, split_agent_items

    assert split_agent_items("开场废话\n1. 2024-03-15 · gist\n2. 2024-04-01 · later\n谢谢") == [
        "1. 2024-03-15 · gist",
        "2. 2024-04-01 · later",
    ]
    assert majority_align([1, None, 3], [1, 2, None]) == [1, None, 3]
    n_pair, n_yes = pair_line_stats("PAIR 1-1 YES\nPAIR 1-2 NO\nnoise")
    assert n_pair == 2
    assert n_yes == 1


def test_align_uses_max_cardinality_not_greedy_first_fit() -> None:
    """贪心 first-fit 会因为「前排抢走、后排无路」而丢掉本可命中的行。

    R1 有 A1/A2 两个候选却先抢 A1，A2 因此被 R2 占死，R1 再也换不到 A2；
    正确解是让 R1 让出 A1 去拿 A2、R3 接手 A1，三条全中。贪心只能匹配 2 条。
    """
    from eval.common.judge import align_from_eq_matrix

    mat = [
        [True, True, False],  # R1: A1 或 A2
        [False, True, True],  # R2: A2 或 A3
        [True, False, False],  # R3: 只认 A1
    ]
    align = align_from_eq_matrix(mat)
    matched = [a for a in align if a is not None]
    assert len(matched) == 3, f"最大基数应为 3，贪心只会给 2；实际 {align}"
    assert sorted(a for a in matched if a is not None) == [1, 2, 3], f"一对一被破坏: {align}"


def test_align_recovers_edges_greedy_discarded() -> None:
    """BEAM 100K conv=1 EO 实测形态：yes=4/25 却只对齐 3 行，多余命中被一对一挤掉。

    这里构造「两行争同一列 + 一行争同一列」的组合，最大匹配应把四行全部接住。
    """
    from eval.common.judge import align_from_eq_matrix

    mat = [
        [False, False, True, False, False],  # R1 -> A3
        [False, False, False, False, True],  # R2 -> A5
        [False, False, False, False, True],  # R3 -> A5（R2 的同列竞争者）
        [False, False, True, False, False],  # R4 -> A3（R1 的同列竞争者）
        [False, False, False, True, False],  # R5 -> A4
    ]
    align = align_from_eq_matrix(mat)
    # 可用列只有 A3/A4/A5 三列，最大匹配就是 3；关键是不得把 A4 也漏掉
    assert align[4] == 4
    assert len([a for a in align if a is not None]) == 3


def test_align_keeps_unmatched_rows_none() -> None:
    """无任何 YES 的行仍是 None；空矩阵不炸。"""
    from eval.common.judge import align_from_eq_matrix

    assert align_from_eq_matrix([]) == []
    mat = [
        [True, False],
        [False, False],
    ]
    assert align_from_eq_matrix(mat) == [1, None]


def test_parse_beam_order_keeps_align_and_metrics() -> None:
    from eval.common.judge import attach_order_metrics, parse_beam_judge_response

    rubric = ["a", "b", "c"]
    parsed = parse_beam_judge_response(
        '{"align": [2, 1, 3], "rubric_scores": [1, 1, 1], "passed": true, "reason": "ok"}',
        rubric,
    )
    out = attach_order_metrics(parsed, rubric)
    assert out["align"] == [2, 1, 3]
    assert out["coverage"] == 1.0
    assert out["tau"] is not None
    assert out["passed"] is False
    ordered = attach_order_metrics(
        parse_beam_judge_response(
            '{"align": [1, 2, 3], "rubric_scores": [1, 1, 1], "passed": false, "reason": "ok"}',
            rubric,
        ),
        rubric,
    )
    assert ordered["passed"] is True
    assert ordered["tau"] == 1.0
    align_only = attach_order_metrics(
        parse_beam_judge_response(
            '{"align": [1, 2, 3], "rubric_scores": [1, 1, 0], "passed": true, "reason": "ok"}',
            rubric,
        ),
        rubric,
    )
    assert align_only["passed"] is False
