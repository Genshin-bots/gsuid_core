"""评测公共模块

把多个评测脚本（LongMemEval、BEAM 官方 ladder 等）共用的 HTTP 调用、LLM 评判、IO 工具
统一收敛到这里，避免在每个 eval 脚本里重复实现。子模块保持功能单一：

- :mod:`eval.common.http_client`  : 异步 HTTP 调用 gsuid_core /api/...
- :mod:`eval.common.judge`        : LLM 评判 + 简单字符串匹配
- :mod:`eval.common.io`           : JSON / JSONL 读写 + 增量更新辅助
- :mod:`eval.common.beam_runner`  : BEAM ingest/probe/judge 共用实现
- :mod:`eval.common.timestamps`   : BEAM payload 时间戳摊开
"""

from .io import (
    dump_json,
    load_json,
    load_jsonl,
    load_eval_data,
    read_existing_ids,
    load_existing_answers,
)
from .judge import (
    kendall_tau_b,
    judge_beam_order,
    parse_align_list,
    judge_beam_single,
    judge_single_answer,
    simple_string_match,
    parse_judge_response,
    official_tau_from_align,
    order_metrics_from_align,
)
from .http_client import (
    DEFAULT_TIMEOUT,
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_API,
    call_send_msg,
    call_batch_observe,
    call_chat_with_history,
    call_clear_user_global,
    call_rebuild_hiergraph,
    extract_text_from_response,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CHAT_API",
    "DEFAULT_TIMEOUT",
    "call_batch_observe",
    "call_chat_with_history",
    "call_clear_user_global",
    "call_rebuild_hiergraph",
    "call_send_msg",
    "extract_text_from_response",
    "dump_json",
    "load_eval_data",
    "load_existing_answers",
    "load_json",
    "load_jsonl",
    "read_existing_ids",
    "judge_beam_order",
    "judge_beam_single",
    "judge_single_answer",
    "kendall_tau_b",
    "official_tau_from_align",
    "order_metrics_from_align",
    "parse_align_list",
    "parse_judge_response",
    "simple_string_match",
]
