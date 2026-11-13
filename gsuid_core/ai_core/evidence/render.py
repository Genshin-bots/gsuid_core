"""证据渲染：B 轨 obs 行前缀 + 出站〔依据:…〕标签（§4.6）。

obs 行只进当轮尾部 ToolReturnPart，不回改更早历史（前缀缓存红线）；
出站标签只写绝对时间，最多两条，库中查不到的 obs_id 跳过。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Sequence
from datetime import datetime

from gsuid_core.ai_core.control.lines import EVIDENCE_STALE_AUTH, EVIDENCE_OUTCOME_LABELS
from gsuid_core.ai_core.evidence.store import Observation, EvidenceStore, get_evidence_store

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

_EVIDENCE_TAG_MAX = 2


def _outcome_label(obs: Observation, store: EvidenceStore, bot_id: str) -> str:
    if store.is_invalidated(obs, bot_id):
        return EVIDENCE_STALE_AUTH
    return EVIDENCE_OUTCOME_LABELS[obs.outcome]


def _cred_label(obs: Observation, name_of: Callable[[str], str]) -> str:
    if obs.credential_owner is None:
        return ""
    if obs.credential_owner == "shared":
        return "共享凭据"
    return f"{name_of(obs.credential_owner)}的凭据"


def obs_track_line(
    obs: Observation,
    store: EvidenceStore,
    bot_id: str,
    name_of: Callable[[str], str],
) -> str:
    """``obs ob_xx | 服务名 | 替名查 · 名的凭据 | 结果 | HH:MM:SS``。"""
    who = name_of(obs.requested_by)
    cred = _cred_label(obs, name_of)
    cred_part = f" · {cred}" if cred else ""
    clock = datetime.fromtimestamp(obs.observed_at).strftime("%H:%M:%S")
    return f"obs {obs.obs_id} | {obs.service} | 替{who}查{cred_part} | {_outcome_label(obs, store, bot_id)} | {clock}"


def obs_basis_tag(
    obs: Observation,
    store: EvidenceStore,
    bot_id: str,
    name_of: Callable[[str], str],
) -> str:
    """``〔依据: 替名查 · 名的凭据 · 结果 · HH:MM〕``（绝对时间）。"""
    who = name_of(obs.requested_by)
    cred = _cred_label(obs, name_of)
    cred_part = f" · {cred}" if cred else ""
    clock = datetime.fromtimestamp(obs.observed_at).strftime("%H:%M")
    return f"〔依据: 替{who}查{cred_part} · {_outcome_label(obs, store, bot_id)} · {clock}〕"


def evidence_tags_for_ids(
    obs_ids: tuple[str, ...],
    session_id: str,
    bot_id: str,
    name_of: Callable[[str], str],
) -> list[str]:
    store = get_evidence_store(session_id)
    tags: list[str] = []
    for obs_id in obs_ids:
        if len(tags) >= _EVIDENCE_TAG_MAX:
            break
        obs = store.find(obs_id)
        if obs is not None:
            tags.append(obs_basis_tag(obs, store, bot_id, name_of))
    return tags


def prefix_tool_returns_with_evidence(
    messages: Sequence[ModelMessage],
    obs_ids: list[str],
    session_id: str,
    bot_id: str,
    name_of: Callable[[str], str],
) -> int:
    """把本轮观测按 tool_name 配对前缀进 ToolReturnPart.content（只动本轮尾）。"""
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    if not obs_ids:
        return 0
    store = get_evidence_store(session_id)
    by_tool: dict[str, list[Observation]] = {}
    for obs_id in obs_ids:
        obs = store.find(obs_id)
        if obs is not None:
            by_tool.setdefault(obs.tool_name, []).append(obs)
    if not by_tool:
        return 0
    used: dict[str, int] = {}
    count = 0
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if type(part) is not ToolReturnPart or not isinstance(part.content, str):
                continue
            queue = by_tool[part.tool_name] if part.tool_name in by_tool else []
            idx = used[part.tool_name] if part.tool_name in used else 0
            if idx >= len(queue):
                continue
            used[part.tool_name] = idx + 1
            part.content = obs_track_line(queue[idx], store, bot_id, name_of) + "\n" + part.content
            count += 1
    return count
