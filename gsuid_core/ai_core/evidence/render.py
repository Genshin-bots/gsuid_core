"""证据渲染：出站〔依据:…〕标签（§4.6）。

出站标签只写绝对时间，最多两条，库中查不到的 obs_id 跳过；不再事后改写已入缓存的 ToolReturnPart。
"""

from __future__ import annotations

from typing import Callable
from datetime import datetime

from gsuid_core.ai_core.control.lines import EVIDENCE_STALE_AUTH, EVIDENCE_OUTCOME_LABELS
from gsuid_core.ai_core.evidence.store import Observation, EvidenceStore, get_evidence_store

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
