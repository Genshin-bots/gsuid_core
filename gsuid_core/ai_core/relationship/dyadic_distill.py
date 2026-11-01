"""双边相处契约提炼：好感 ≥35 且够 15 次新互动后，后台跑一轮 vibe/老梗蒸馏。

只更新 ``userfavorability`` 的 ``interaction_vibe`` / ``shared_callbacks`` /
``last_vibe_distill_count``；失败或 agent 错误回执只推进计数，防止每轮重试。
"""

from __future__ import annotations

import time
import asyncio
from typing import Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event

_TRIGGER_MIN_SCORE = 35
_TRIGGER_MIN_NEW_INTERACTIONS = 15
# 提炼用对话行数与单行长度
_DIALOGUE_LINES = 12
_LINE_MAX = 120
# (user, bot, persona) 级在途去重
_in_flight: set[tuple[str, str, str]] = set()
# 事件循环只弱引用 task，必须在这里持有强引用，否则可能中途被回收
_tasks: set[asyncio.Task[None]] = set()

_PROMPT = """下面是你和「{user_name}」最近的直接对话，以及你之前对你们相处方式的印象。
[之前的印象]
{old_vibe}
[之前记下的共同梗]
{old_callbacks}
[最近对话]
{dialogue}

请用你自己的口吻，更新你对你们相处方式的印象。只输出一个 JSON 对象，不要输出别的内容：
{{"interaction_vibe": "一句第一人称的话，50 字以内，写你和 TA 平时怎么相处",
"shared_callbacks": ["你们之间专属的共同梗或约定，每条 35 字以内，最多 3 条"]}}
只写对话里真的出现过的事，不要编造。不要写成以后说话要遵守的规矩。"""


async def maybe_schedule_dyadic_distill(
    *,
    event: Event,
    bot,
    user_id: str,
    bot_id: str,
    persona_name: str,
    score_after: int,
) -> None:
    """结算后钩子：满足阈值且同键无在途任务时，后台蒸馏一轮。"""
    if not persona_name or score_after < _TRIGGER_MIN_SCORE:
        return
    from gsuid_core.ai_core.database.models import UserFavorability

    row = await UserFavorability.get_user_favorability(user_id, bot_id, persona_name)
    if row is None:
        return
    if row.interaction_count - row.last_vibe_distill_count < _TRIGGER_MIN_NEW_INTERACTIONS:
        return
    key = (user_id, bot_id, persona_name)
    if key in _in_flight:
        return
    _in_flight.add(key)
    task = asyncio.create_task(
        _run_dyadic_distill(event=event, user_id=user_id, bot_id=bot_id, persona_name=persona_name, row=row)
    )
    _tasks.add(task)

    def _done(t: "asyncio.Task[None]") -> None:
        _in_flight.discard(key)
        _tasks.discard(t)

    task.add_done_callback(_done)


def _dialogue_lines(event: Event, user_id: str, persona_name: str) -> list[str]:
    """本会话最近 12 条：本人 user 行 + 本人格 assistant 行（显示为「你」）。

    assistant 行只收 ``metadata.persona_name`` 等于本人格的——共享会话里
    别人格的发言不能算进这条双边契约（未打标的历史行一律不收）。
    """
    from gsuid_core.message_history import get_history_manager

    history = get_history_manager().get_history(event)
    lines: list[str] = []
    for record in reversed(list(history)):
        if record.role == "user" and str(record.user_id) == str(user_id):
            name = record.user_name if isinstance(record.user_name, str) and record.user_name else str(record.user_id)
            lines.append(f"{name}: {record.content[:_LINE_MAX]}")
        elif record.role == "assistant":
            speaker = record.metadata["persona_name"] if "persona_name" in record.metadata else ""
            if speaker != persona_name:
                continue
            lines.append(f"你: {record.content[:_LINE_MAX]}")
        if len(lines) >= _DIALOGUE_LINES:
            break
    lines.reverse()
    return lines


async def _run_dyadic_distill(
    *,
    event: Event,
    user_id: str,
    bot_id: str,
    persona_name: str,
    row,
) -> None:
    from gsuid_core.ai_core.database.models import UserFavorability

    new_count = row.interaction_count
    try:
        from gsuid_core.ai_core.utils import is_agent_error_text, extract_json_from_text
        from gsuid_core.ai_core.gs_agent import create_agent
        from gsuid_core.ai_core.persona.resource import load_persona, extract_compact_persona
        from gsuid_core.ai_core.autonomy.micro_arc import strip_markup
        from gsuid_core.ai_core.interaction_scaffold import is_persistent_style_rule

        card = extract_compact_persona(await load_persona(persona_name))
        dialogue = "\n".join(_dialogue_lines(event, user_id, persona_name)) or "（暂无）"
        user_message = _PROMPT.format(
            user_name=row.user_name or user_id,
            old_vibe=row.interaction_vibe or "（还没有）",
            old_callbacks="；".join(row.shared_callbacks_list) or "（还没有）",
            dialogue=dialogue,
        )
        agent = create_agent(
            system_prompt=card,
            create_by="Dyadic_Distill",
            persona_name=persona_name,
            session_id=f"dyadic_{persona_name}_{user_id}_{int(time.time())}",
            is_subagent=True,
        )
        try:
            agent.bind_budget_scope(event)
            result = await agent.run(user_message, ev=event, return_mode="return", budget_gate=True)
        finally:
            agent._session_logger.close()
        text = result if isinstance(result, str) else ""
        if not text or is_agent_error_text(text):
            await UserFavorability.update_dyadic_contract(
                user_id, bot_id, persona_name, vibe=None, callbacks=None, distill_count=new_count
            )
            return
        data = extract_json_from_text(text)
        vibe: Optional[str] = None
        callbacks: Optional[list[str]] = None
        if isinstance(data, dict):
            raw_vibe = data["interaction_vibe"] if "interaction_vibe" in data else ""
            if isinstance(raw_vibe, str):
                cleaned = strip_markup(raw_vibe)[:50]
                if cleaned and not is_persistent_style_rule(cleaned):
                    vibe = cleaned
            raw_cb = data["shared_callbacks"] if "shared_callbacks" in data else []
            if isinstance(raw_cb, list):
                items: list[str] = []
                for item in raw_cb:
                    if not isinstance(item, str):
                        continue
                    cleaned = strip_markup(item)[:35]
                    if cleaned and not is_persistent_style_rule(cleaned):
                        items.append(cleaned)
                callbacks = items[:3]
        await UserFavorability.update_dyadic_contract(
            user_id, bot_id, persona_name, vibe=vibe, callbacks=callbacks, distill_count=new_count
        )
    except Exception as e:
        # 提炼失败只推进计数：否则会话每轮都排队重试
        logger.debug(t("log.ai.dyadic_distill_fail", e=e))
        await UserFavorability.update_dyadic_contract(
            user_id, bot_id, persona_name, vibe=None, callbacks=None, distill_count=new_count
        )
