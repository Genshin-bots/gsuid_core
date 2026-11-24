"""``gscore.evidence``：证据块（§4.6）。

列本会话最近 30 分钟内的工具观测（≤6 条，新的在前），按「替本轮
待回应者 / 替别人」分组，并附实时凭据状态行；当轮 user 侧，入史前剥除。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Sequence
from datetime import datetime

from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook
from gsuid_core.ai_core.kits.base import AgentKit
from gsuid_core.ai_core.kits.registry import register_agent_kit

if TYPE_CHECKING:
    from gsuid_core.ai_core.evidence.store import Observation, EvidenceStore
    from gsuid_core.message_history.manager import MessageRecord

_OBS_WINDOW_S = 30 * 60
_OBS_LIMIT = 6


def _fmt_rel(now: float, ts: float) -> str:
    delta = max(0, int(now - ts))
    clock = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    if delta < 60:
        return f"{delta} 秒前"
    if delta < 3600:
        return f"{delta // 60} 分 {delta % 60} 秒前（{clock}）" if delta % 60 else f"{delta // 60} 分钟前（{clock}）"
    return f"{delta // 3600} 小时前（{clock}）"


def _fmt_obs_line(obs: Observation, store: EvidenceStore, bot_id: str, now: float, nicks: dict[str, str]) -> str:
    from gsuid_core.ai_core.evidence.render import _cred_label, _outcome_label

    def _name(uid: str) -> str:
        return nicks[uid] if uid in nicks else uid

    who = _name(obs.requested_by)
    outcome = _outcome_label(obs, store, bot_id)
    cred = _cred_label(obs, _name)
    cred_part = f" · {cred}" if cred else ""
    rel = _fmt_rel(now, obs.observed_at)
    head = f"· {obs.obs_id} {obs.service} | 替{who}查{cred_part}"
    return f"{head} | {outcome}：{obs.summary} | {rel}"


async def render_evidence_block(
    session_id: str,
    bot_id: str,
    target_uids: list[str],
    records: Sequence[MessageRecord],
) -> str:
    from gsuid_core.ai_core.control.lines import (
        EVIDENCE_LIVE_HEAD,
        EVIDENCE_BLOCK_HEAD,
        EVIDENCE_FOR_OTHERS_HEAD,
        EVIDENCE_FOR_TARGETS_HEAD,
    )
    from gsuid_core.ai_core.evidence.store import get_evidence_store
    from gsuid_core.ai_core.evidence.providers import get_state_provider

    store = get_evidence_store(session_id)
    now = time.time()
    obs_list = store.recent(limit=_OBS_LIMIT, within_s=_OBS_WINDOW_S, now=now)
    if not obs_list:
        return ""

    nicks = {rec.user_id: (rec.user_name or rec.user_id) for rec in records if rec.user_id and rec.role == "user"}
    target_set = set(target_uids)
    for_targets = [o for o in obs_list if o.requested_by in target_set]
    for_others = [o for o in obs_list if o.requested_by not in target_set]

    pieces = [EVIDENCE_BLOCK_HEAD]
    if for_targets:
        pieces.append(EVIDENCE_FOR_TARGETS_HEAD)
        pieces.extend(_fmt_obs_line(o, store, bot_id, now, nicks) for o in for_targets)
    if for_others:
        pieces.append(EVIDENCE_FOR_OTHERS_HEAD)
        pieces.extend(_fmt_obs_line(o, store, bot_id, now, nicks) for o in for_others)

    # 实时状态：列内观测涉及的 MCP 服务 × 本轮目标用户，各查一次
    services = sorted({o.service for o in obs_list})
    live_parts: list[str] = []
    for service in services:
        provider = get_state_provider(service)
        if provider is None:
            continue
        checks = [uid for uid in dict.fromkeys(target_uids + [o.requested_by for o in obs_list])]
        pairs: list[str] = []
        for uid in checks:
            state = await provider.current(service, bot_id, uid)
            if state is not None:
                who = nicks[uid] if uid in nicks else uid
                pairs.append(f"{who}={'已绑定' if state.value == 'bound' else '未绑定'}")
        if pairs:
            live_parts.append(f"{service}凭据 " + "，".join(pairs))
    if live_parts:
        pieces.append(EVIDENCE_LIVE_HEAD + "；".join(live_parts))
    return "\n".join(pieces)


class EvidenceKit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=185, kit_id=self.kit_id)(self.inject)

    async def inject(self, ctx: AgentHookContext) -> None:
        from gsuid_core.message_history import get_history_manager
        from gsuid_core.ai_core.inbox.turn import current_turn

        turn = current_turn()
        if turn is None or ctx.ev is None:
            return
        records = get_history_manager().get_history(ctx.ev)
        targets = [e.item.user_id for e in turn.entries]
        text = await render_evidence_block(turn.session_id, ctx.ev.bot_id, targets, records)
        if text:
            ctx.set_context_block("evidence", text)


KIT = register_agent_kit(EvidenceKit(kit_id="gscore.evidence", slot="evidence", display_name="证据块"))
