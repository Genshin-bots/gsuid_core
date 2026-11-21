"""Eval Inbox Turn API（仅 LOCAL_TEST_MODE 挂载）：驱动真实群聊收件箱连发/插话/引用评测。"""

from __future__ import annotations

import time
import uuid
from typing import List, Literal, Optional

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.bot import _Bot
from gsuid_core.webconsole.app_app import app
from gsuid_core.ai_core.configs.task_kinds import TASK_CHAT
from gsuid_core.webconsole._local_test_gate import LOCAL_TEST_MODE, require_local_test
from gsuid_core.webconsole.chat_with_history_api import (
    register_eval_images,
    early_silence_eval_response,
    finalize_eval_turn_response,
)

from ._api_tags import CHAT


class EvalInboxMessage(BaseModel):
    """评测群聊收件箱单条消息：支持连发分片、插话、引用边、附图与硬/软唤醒。"""

    user_id: str = "100000001"
    user_name: str = ""
    text: str = ""
    msg_id: str = ""
    reply_to_msg_id: str = ""
    images: List[str] = []
    at_targets: List[str] = []
    is_tome: Optional[bool] = None
    is_trigger: Optional[bool] = None
    strength: Literal["hard", "soft"] = "hard"


class EvalInboxBurstRequest(BaseModel):
    """评测群聊真实收件箱批次请求（直走 InboxTurn + run_interactive_turn）。"""

    group_id: str
    bot_id: str = "HTTP"
    persona_name: Optional[str] = None
    session_id: Optional[str] = None
    messages: List[EvalInboxMessage] = []
    enable_tools: bool = True
    enable_observer: bool = False
    max_history: int = 50
    clock_at: Optional[str] = None
    skip_memory: bool = False


@app.post("/api/eval/inbox_turn", include_in_schema=LOCAL_TEST_MODE, summary="评测群聊收件箱批次", tags=CHAT)
async def evalInboxTurn(
    req: EvalInboxBurstRequest,
    _gate: Optional[None] = Depends(require_local_test),
):
    """评测群聊真实收件箱通道：支持连发分片、多说话人插话、引用边与附图。"""
    from gsuid_core.bot import Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.hooks import HookDecision, AgentHookPoint, AgentHookContext, fire_hooks
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.handle_ai import run_interactive_turn
    from gsuid_core.ai_core.inbox.turn import InboxTurn, bind_turn, reset_turn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry
    from gsuid_core.ai_core.turn_pipeline import parse_clock_at, injected_clock_date_label
    from gsuid_core.ai_core.persona.persona import Persona
    from gsuid_core.ai_core.context_assembly import build_session_system_prompt
    from gsuid_core.ai_core.session_registry import get_ai_session_registry
    from gsuid_core.ai_core.interaction_scaffold import is_addressed_to_self

    if not req.messages:
        return {"status_code": -101, "data": None, "error": "messages is required"}

    if req.enable_observer:
        from gsuid_core.ai_core.memory import observe, get_ingestion_worker
        from gsuid_core.ai_core.memory.config import memory_config

        for item in req.messages:
            if not item.text:
                continue
            await observe(
                content=item.text,
                speaker_id=str(item.user_id),
                group_id=req.group_id,
                bot_self_id=req.bot_id,
                observer_blacklist=memory_config.observer_blacklist,
                message_type="group_msg",
                persona_name=req.persona_name or "",
                at_targets=tuple(item.at_targets),
            )
        worker = get_ingestion_worker()
        if worker is not None:
            await worker.flush_all()

    mgr = get_history_manager()
    persona_name = req.persona_name
    seed_ev = Event(
        bot_id=req.bot_id,
        user_id=req.messages[0].user_id,
        group_id=req.group_id,
        user_type="group",
    )
    from_seq = mgr.head_seq(seed_ev)
    pending_entries: list[PendingEntry] = []
    all_img_ids: list[str] = []
    built_events: list[Event] = []
    now_ts = time.time()

    for idx, item in enumerate(req.messages):
        ev = Event(
            bot_id=req.bot_id,
            user_id=item.user_id,
            group_id=req.group_id,
            user_type="group",
        )
        ev.raw_text = item.text
        ev.text = item.text
        ev.msg_id = item.msg_id or f"eval_msg_{uuid.uuid4().hex[:10]}"
        ev.reply_id = item.reply_to_msg_id or None
        addressed = (
            bool(item.is_tome)
            if item.is_tome is not None
            else is_addressed_to_self(item.text, persona_name or "", False)
        )
        ev.is_tome = addressed
        m_imgs = register_eval_images(item.images)
        if m_imgs:
            ev.image_id_list = list(m_imgs)
            ev.image_id = m_imgs[0]
            all_img_ids.extend(m_imgs)
        meta: dict[str, object] = {"msg_id": ev.msg_id, "at_bot": addressed}
        if item.reply_to_msg_id:
            meta["reply_id"] = item.reply_to_msg_id
        if item.at_targets:
            meta["at_list"] = list(item.at_targets)
        rec = mgr.add_message(
            event=ev,
            role="user",
            content=item.text,
            user_name=item.user_name or None,
            metadata=meta,
        )
        built_events.append(ev)
        is_trig = bool(item.is_trigger) if item.is_trigger is not None else (addressed or idx == len(req.messages) - 1)
        if is_trig:
            seq = rec.link.seq if rec.link is not None else mgr.head_seq(ev)
            wake_kind = "mention" if addressed else "followup"
            wake = WakeItem(
                seq=seq,
                user_id=ev.user_id,
                kind=wake_kind,
                strength=item.strength,
                created_at=now_ts,
            )
            pending_entries.append(PendingEntry(item=wake, event=ev, persona_name=persona_name or ""))

    if not pending_entries:
        last_ev = built_events[-1]
        wake = WakeItem(
            seq=mgr.head_seq(last_ev),
            user_id=last_ev.user_id,
            kind="followup",
            strength="soft",
            created_at=now_ts,
        )
        pending_entries.append(PendingEntry(item=wake, event=last_ev, persona_name=persona_name or ""))

    primary_ev = pending_entries[0].event
    if all_img_ids and not primary_ev.image_id_list:
        primary_ev.image_id_list = list(all_img_ids)
        primary_ev.image_id = all_img_ids[0]

    clock = parse_clock_at(req.clock_at)
    reuse_sid = (req.session_id or "").strip()
    agent = get_ai_session_registry().get_ai_session(reuse_sid) if reuse_sid.startswith("eval_sess_") else None
    if agent is None:
        _sys_prompt = "你是一个智能助手，请根据对话历史回答用户的问题。"
        if persona_name and (Persona(persona_name).exists() or persona_name == "智能助手"):
            clock_date = injected_clock_date_label(clock) if clock is not None else None
            _sys_prompt = await build_session_system_prompt(primary_ev, persona_name, clock_date=clock_date)
        sid = reuse_sid if reuse_sid.startswith("eval_sess_") else f"eval_inbox_{req.group_id}_{uuid.uuid4().hex[:8]}"
        agent = create_agent(
            system_prompt=_sys_prompt,
            persona_name=persona_name,
            create_by="Chat",
            max_history=req.max_history,
            task_level="high",
            task_kind=TASK_CHAT,
            session_id=sid,
            dynamic_tools=bool(req.enable_tools),
        )
        if reuse_sid.startswith("eval_sess_"):
            get_ai_session_registry().set_ai_session(reuse_sid, agent)
    agent.turn_clock = clock

    head_seq = mgr.head_seq(primary_ev)
    turn = InboxTurn(
        session_id=agent.session_id,
        persona_name=persona_name or "",
        entries=tuple(pending_entries),
        from_seq=from_seq,
        head_seq=head_seq,
        backfill=True,
    )
    has_hard = any(e.item.strength == "hard" for e in pending_entries)
    query_blob = "\n".join(e.event.raw_text or "" for e in pending_entries if (e.event.raw_text or "").strip())

    _bot = _Bot(req.bot_id)
    bot = Bot(_bot, primary_ev)
    hook_ctx = AgentHookContext(
        point=AgentHookPoint.BEFORE_AI_CHAT,
        ev=primary_ev,
        bot=bot,
        session_id=agent.session_id,
        create_by="Chat",
        query=query_blob,
        persona_name=persona_name,
        skip_memory=bool(req.skip_memory),
        clock_at=clock,
    )

    tok = bind_turn(turn)
    try:
        if await fire_hooks(AgentHookPoint.BEFORE_AI_CHAT, hook_ctx) is not HookDecision.CONTINUE:
            return await early_silence_eval_response(agent, query=query_blob, image_ids=all_img_ids)

        outcome = await run_interactive_turn(
            bot=bot,
            event=primary_ev,
            session=agent,
            query=query_blob,
            hook_ctx=hook_ctx,
            soft_triggered=not has_hard,
            return_mode="return",
            deliver=False,
            settle=True,
            history_context="",
            inbox_turn=turn,
        )
        memory_text = hook_ctx.retrieved["memory"] if "memory" in hook_ctx.retrieved else ""
        data, out = await finalize_eval_turn_response(
            agent,
            silenced_early=outcome.silenced_early,
            is_silence=outcome.is_silence,
            has_result=bool(outcome.result),
            result_text=outcome.result_text,
            memory_text=memory_text,
            image_ids=all_img_ids,
        )
        if data != "<SILENCE>":
            mgr.add_message(event=primary_ev, role="assistant", content=data)
        return out
    finally:
        reset_turn(tok)
