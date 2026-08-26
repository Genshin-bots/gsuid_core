"""进行中的 HTTP Agent run，供 cancel / reset / 断连使用。"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Protocol
from dataclasses import dataclass


class CancelableAgent(Protocol):
    _cancel_generation: asyncio.Event


@dataclass
class ActiveRun:
    run_id: str
    key_id: str
    agent_session_id: str
    turn_task: asyncio.Task[object]
    agent: Optional[CancelableAgent] = None


_runs: Dict[str, ActiveRun] = {}
_by_session: Dict[str, List[str]] = {}


def register_run(run: ActiveRun) -> None:
    _runs[run.run_id] = run
    if run.agent_session_id not in _by_session:
        _by_session[run.agent_session_id] = []
    _by_session[run.agent_session_id].append(run.run_id)


def bind_agent(run_id: str, agent: CancelableAgent) -> None:
    if run_id not in _runs:
        return
    _runs[run_id].agent = agent


def get_run(run_id: str) -> ActiveRun | None:
    if run_id not in _runs:
        return None
    return _runs[run_id]


def discard_run(run_id: str) -> None:
    if run_id not in _runs:
        return
    run = _runs[run_id]
    del _runs[run_id]
    if run.agent_session_id in _by_session:
        ids = [i for i in _by_session[run.agent_session_id] if i != run_id]
        if ids:
            _by_session[run.agent_session_id] = ids
        else:
            del _by_session[run.agent_session_id]


def runs_for_session(agent_session_id: str) -> List[ActiveRun]:
    if agent_session_id not in _by_session:
        return []
    out: List[ActiveRun] = []
    for run_id in list(_by_session[agent_session_id]):
        if run_id in _runs:
            out.append(_runs[run_id])
    return out


async def cancel_run(run: ActiveRun) -> None:
    agent = run.agent
    if agent is not None:
        agent._cancel_generation.set()
    run.turn_task.cancel()
    try:
        await run.turn_task
    except (asyncio.CancelledError, Exception):
        pass


async def cancel_session_runs(
    agent_session_id: str,
    *,
    except_run_id: str | None = None,
) -> None:
    for run in runs_for_session(agent_session_id):
        if except_run_id is not None and run.run_id == except_run_id:
            continue
        await cancel_run(run)
        discard_run(run.run_id)


def reset_runtime_for_tests() -> None:
    _runs.clear()
    _by_session.clear()
