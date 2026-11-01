"""微弧（micro-arc）：自主行动单话题的有限跟进与收尾。

每轮 run 产出一个 footer（TOPIC/OUTCOME/FACT/TAKE），同一话题的活跃弧
只跟进 ``arc_persistence`` 轮，到点强制收尾；ready_to_share 的弧可被
后续私聊/群聊自然带出，带出即 consolidated。
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

from gsuid_core.ai_core.persona.homeostasis import persona_scope

ArcOrigin = Literal["care_user", "group_topic", "self_explore"]
ArcStage = Literal["seed", "attempt", "ready_to_share", "consolidated", "abandoned"]
ArcOutcome = Literal["found", "confused", "distracted", "wrong_guess", "pending", ""]
_FINAL_OUTCOMES = ("found", "confused", "distracted", "wrong_guess")
_ACTIVE_STAGES = ("seed", "attempt")
_ARC_KEY = "micro_arcs"
_EXPIRE_SECONDS = 86400.0
_CLOSE_AGE = 86400.0
_TOPIC_MAX = 24
_TAKE_MAX = 80

_TAG_BLOCK = re.compile(r"<([a-zA-Z_][\w:-]*)[^>]*>.*?</\1>", re.DOTALL)
_LONE_TAG = re.compile(r"<[^>\n]{1,200}>")
_WS = re.compile(r"\s+")


class MicroArcDict(TypedDict):
    arc_id: str
    topic_key: str
    stage: str
    origin: str
    intent: str
    hop_count: int
    outcome: str
    fact: str
    take: str
    target_group_id: str
    target_user_id: str
    created_at: float
    updated_at: float
    expires_at: float


def _normalize_body(text: str) -> str:
    """剥空白/标点、转小写，不截断。"""
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def normalize_topic_key(text: str) -> str:
    """话题键：剥空白/标点、转小写、截 24 字符。"""
    return _normalize_body(text)[:_TOPIC_MAX]


def topic_mentioned(topic_key: str, text: str) -> bool:
    """话题键是否出现在消息正文里（正文归一后不截断，长消息尾部的词也能命中）。"""
    return bool(topic_key) and topic_key in _normalize_body(text)


def strip_markup(text: str) -> str:
    """剥包裹标签块（如 <search_results>…</search_results>）与残留的 lone tag。"""
    body = _TAG_BLOCK.sub(" ", text or "")
    body = _LONE_TAG.sub(" ", body)
    return _WS.sub(" ", body).strip()


def active_arc(arcs: list[MicroArcDict]) -> MicroArcDict | None:
    for arc in arcs:
        if arc["stage"] in _ACTIVE_STAGES:
            return arc
    return None


def advance_arc(
    arcs: list[MicroArcDict],
    *,
    topic_key: str,
    intent: str,
    origin: str,
    outcome: str,
    fact: str,
    take: str,
    target_group_id: str,
    target_user_id: str,
    max_hops: int,
    now: float,
    arc_id: str,
) -> tuple[list[MicroArcDict], MicroArcDict | None]:
    """推进一次弧。返回 (更新后的列表, 本话题弧)；topic_key 为空则原样返回。"""
    key = normalize_topic_key(topic_key)
    if not key:
        return arcs, None
    fact = strip_markup(fact)[:200]
    take = strip_markup(take)[:_TAKE_MAX]
    arc = next((a for a in arcs if a["stage"] in _ACTIVE_STAGES and a["topic_key"] == key), None)
    if arc is not None:
        arc["hop_count"] += 1
    else:
        for stale in arcs:
            if stale["stage"] in _ACTIVE_STAGES:
                stale["stage"] = "abandoned"
                stale["updated_at"] = now
        arc = MicroArcDict(
            arc_id=arc_id,
            topic_key=key,
            stage="attempt",
            origin=origin,
            intent=intent[:200],
            hop_count=1,
            outcome="pending",
            fact="",
            take="",
            target_group_id=target_group_id,
            target_user_id=target_user_id,
            created_at=now,
            updated_at=now,
            expires_at=0.0,
        )
        arcs.append(arc)
    if outcome in _FINAL_OUTCOMES:
        arc["outcome"] = outcome
        if fact or take:
            arc["stage"] = "ready_to_share"
            arc["expires_at"] = now + _EXPIRE_SECONDS
            arc["fact"] = fact
            arc["take"] = take
        else:
            arc["stage"] = "abandoned"
    elif arc["hop_count"] >= max_hops:
        # 到轮强制收尾：有事实按 confused 收尾，没有就放弃
        if fact:
            arc["outcome"] = "confused"
            arc["stage"] = "ready_to_share"
            arc["expires_at"] = now + _EXPIRE_SECONDS
            arc["fact"] = fact
            arc["take"] = take
        else:
            arc["stage"] = "abandoned"
    arc["updated_at"] = now
    return arcs, arc


def prune_arcs(arcs: list[MicroArcDict], now: float) -> list[MicroArcDict]:
    """过期 ready 丢弃；收尾弧留 24h；ready ≤3 条（新者先留）、活跃 ≤1 条。"""
    kept: list[MicroArcDict] = []
    for arc in arcs:
        if arc["stage"] == "ready_to_share" and arc["expires_at"] and arc["expires_at"] < now:
            continue
        if arc["stage"] in ("consolidated", "abandoned") and now - arc["updated_at"] >= _CLOSE_AGE:
            continue
        kept.append(arc)
    ready = [a for a in kept if a["stage"] == "ready_to_share"]
    ready.sort(key=lambda a: a["updated_at"], reverse=True)
    overflow = set(id(a) for a in ready[3:])
    active = [a for a in kept if a["stage"] in _ACTIVE_STAGES]
    active.sort(key=lambda a: a["updated_at"], reverse=True)
    overflow.update(id(a) for a in active[1:])
    return [a for a in kept if id(a) not in overflow]


def _arc_belongs_to(arc: MicroArcDict, group_id: str, user_id: str) -> bool:
    """群弧只看 group_id（关心对象固定但场合是群）；私聊弧只看 user_id。"""
    if group_id:
        return arc["target_group_id"] == group_id
    return not arc["target_group_id"] and arc["target_user_id"] == user_id


def ready_arcs_for(arcs: list[MicroArcDict], group_id: str, user_id: str, now: float) -> list[MicroArcDict]:
    return [
        a
        for a in arcs
        if a["stage"] == "ready_to_share" and a["expires_at"] >= now and _arc_belongs_to(a, group_id, user_id)
    ]


def consolidate_for_target(arcs: list[MicroArcDict], group_id: str, user_id: str) -> None:
    """该目标最新的 ready 弧标记为已带出。"""
    candidates = [a for a in arcs if a["stage"] == "ready_to_share" and _arc_belongs_to(a, group_id, user_id)]
    if not candidates:
        return
    newest = max(candidates, key=lambda a: a["updated_at"])
    newest["stage"] = "consolidated"


async def load_micro_arcs(persona_name: str) -> list[MicroArcDict]:
    from gsuid_core.ai_core.state_store import state_get_value

    raw = await state_get_value(persona_scope(persona_name), _ARC_KEY)
    arcs: list[MicroArcDict] = []
    if not isinstance(raw, list):
        return arcs
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("topic_key"), str):
            arcs.append(
                {
                    "arc_id": str(item.get("arc_id") or ""),
                    "topic_key": item["topic_key"],
                    "stage": str(item.get("stage") or "attempt"),
                    "origin": str(item.get("origin") or "self_explore"),
                    "intent": str(item.get("intent") or ""),
                    "hop_count": int(item.get("hop_count") or 0),
                    "outcome": str(item.get("outcome") or "pending"),
                    "fact": str(item.get("fact") or ""),
                    "take": str(item.get("take") or ""),
                    "target_group_id": str(item.get("target_group_id") or ""),
                    "target_user_id": str(item.get("target_user_id") or ""),
                    "created_at": float(item.get("created_at") or 0.0),
                    "updated_at": float(item.get("updated_at") or 0.0),
                    "expires_at": float(item.get("expires_at") or 0.0),
                }
            )
    return arcs


async def save_micro_arcs(persona_name: str, arcs: list[MicroArcDict]) -> None:
    from gsuid_core.ai_core.state_store import state_set_value

    await state_set_value(persona_scope(persona_name), _ARC_KEY, arcs)
