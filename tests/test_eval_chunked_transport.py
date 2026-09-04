"""分块续跑只重试传输故障；LongMem mark-fails 不重跑内容 FAIL。"""

from eval.run_eval import _lm_answer_is_infra
from eval.agent._chunked_run import is_transport_fail


def test_transport_fail_is_connection_not_sla() -> None:
    assert is_transport_fail({"fails": ["ConnectError: boom"], "sample": ""})
    assert is_transport_fail({"fails": ["session_log_not_found"], "sample": ""})
    assert not is_transport_fail(
        {
            "fails": ["max_latency: latency=288.0s cap=40s too-late"],
            "sample": "",
            "avg_latency": 288,
            "input_tokens": 12,
            "case_pass": False,
        }
    )
    assert not is_transport_fail(
        {
            "fails": ["silence_judgment: <SILENCE>"],
            "sample": "<SILENCE>",
            "avg_latency": 45,
            "input_tokens": 0,
            "case_pass": False,
        }
    )
    assert not is_transport_fail(
        {
            "fails": ["max_latency: latency=50s cap=40s unfinished"],
            "sample": "",
            "avg_latency": 50,
            "case_pass": False,
        }
    )
    assert not is_transport_fail(
        {
            "fails": ["final_contains_any: markers_hit=[]"],
            "sample": {"delivered": "call the weather api: now"},
            "case_pass": False,
        }
    )
    assert is_transport_fail({"fails": ["RUN_ERROR:api:ConnectError"], "sample": ""})


def test_mark_fails_only_rewrites_infra_answers() -> None:
    assert _lm_answer_is_infra({"status_code": 500, "agent_answer": "x"})
    assert _lm_answer_is_infra({"status_code": 200, "agent_answer": "[ERROR] timeout"})
    assert _lm_answer_is_infra({"status_code": 200, "agent_answer": ""})
    assert not _lm_answer_is_infra({"status_code": 200, "agent_answer": "He lives in Seattle now."})
    assert not _lm_answer_is_infra({"status_code": 200, "agent_answer": "<SILENCE>"})
