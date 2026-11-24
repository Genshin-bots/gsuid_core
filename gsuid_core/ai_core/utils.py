"""通用纯辅助函数及插件契约导出。

业务实现已拆分至 ``protocol_text`` / ``agent_errors`` / ``history_compact`` /
``thinking`` / ``multimodal.payload`` / ``delivery``。
"""

from __future__ import annotations

import re
import json
from typing import Optional

from json_repair import repair_json

from gsuid_core.ai_core.delivery import send_chat_result
from gsuid_core.ai_core.agent_errors import sanitize_error_for_user
from gsuid_core.ai_core.protocol_text import (
    SILENCE_MARKERS,
    _find_json_span,
    is_silence_marker,
)
from gsuid_core.ai_core.multimodal.payload import prepare_media_payload

__all__ = [
    "SILENCE_MARKERS",
    "extract_json_from_text",
    "prepare_media_payload",
    "sanitize_error_for_user",
    "send_chat_result",
]


def extract_json_from_text(raw_text: str) -> dict | list:
    """从模型自由文本里抽取 JSON，容忍 markdown 围栏与前后散文。

    解析策略（先严后宽，避免 ``repair_json`` 改写本就合法的结构）：
    1. 去掉 markdown 代码围栏（```json ... ``` 或裸 ``` ```）；
    2. 用括号配平从散文里切出第一个完整 JSON（剥掉"好的，结果如下："这类寒暄/解释）；
    3. 快路径：对候选串直接 ``json.loads``；
    4. 慢路径：``repair_json`` 兜底（单引号 / 尾随逗号 / 漏引号 / 被截断的尾部等）。
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Empty input text for JSON extraction")

    stripped = raw_text.strip()
    if is_silence_marker(stripped):
        raise ValueError(f"Special marker '{stripped}' is not valid JSON")

    if stripped.startswith("执行出错"):
        raise ValueError(f"Upstream agent returned error message, not JSON: {stripped[:80]}")

    cleaned = re.sub(r"```[a-zA-Z]*\s*", "", raw_text)
    cleaned = cleaned.replace("```", "").strip()
    if not cleaned:
        raise ValueError("JSON extraction yielded empty content after stripping fences")

    span = _find_json_span(cleaned)
    candidates = [c for c in (span, cleaned) if c]

    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue

    for cand in candidates:
        try:
            repaired = repair_json(cand)
        except Exception:  # repair_json 理论上不抛，仍兜底防御
            continue
        if not repaired or not repaired.strip():
            continue
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            continue

    raise ValueError(f"Failed to parse JSON from text: {stripped[:120]!r}")


def _is_master_user(user_id: str) -> bool:
    """判断指定用户是否为机器人主人（读 core 配置 masters，全框架唯一实现）。"""
    from gsuid_core.config import core_config

    masters = core_config.get_config("masters") or []
    return str(user_id) in [str(m) for m in masters]


def _build_relationship_description(
    user_name: Optional[str],
    user_id: str,
) -> str:
    """构建说话者标识（群聊必需）。

    群聊场景下整个群共用一个 session，多人轮流发言。因此说话者描述里
    **必须显式带上用户ID**，否则昵称重复或为"我"这类无意义值时，
    Agent 无法区分到底是谁在说话。

    关系温度（熟/不熟）由 ``assemble_dynamic_context`` 的 ``relationship`` 块统一注入，
    此处不重复，避免同一信息双写浪费 token。主人标记保留（权限正交，不可省略）。
    """
    if user_name and user_name.strip() and user_name.strip() != str(user_id):
        speaker = f"{user_name.strip()}(用户ID:{user_id})"
    else:
        speaker = f"用户ID:{user_id}"

    if _is_master_user(user_id):
        return f"[⚡主人] {speaker}"
    return speaker
