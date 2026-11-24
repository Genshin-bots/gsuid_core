"""阶段二：一次无状态的行动。会话不复用，工具表在进模型前收口。"""

import time
import uuid
import asyncio

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.autonomy.tools import GuardState, bind_guard, reset_guard, build_autonomy_tools
from gsuid_core.ai_core.autonomy.models import (
    GroupObservation,
    AutonomyStepTrace,
    ObservationDigest,
    CareTargetSnapshot,
)
from gsuid_core.ai_core.autonomy.select import digest_is_empty
from gsuid_core.ai_core.agent_run.support import TraceKind
from gsuid_core.ai_core.autonomy.delivery import output_is_failed
from gsuid_core.ai_core.configs.task_kinds import TASK_AUTONOMY

_RESOURCE_PREFIX = "res_"

# ── 自主行动系统提示（原 prompt.py）──

AUTONOMY_SYSTEM_GUIDE = """\
你正在做一次自主行动。没有人在等你立刻回复。
先看用户消息里的观察底片，再决定这一轮做什么。

动力只许四选一：
- care_user：为观察名单里的某个人查一件具体的事，或主动向对方说话
- group_topic：顺着某个群刚才的话题查证，或主动在群里发言
- self_explore：按你的人格自述里写过的兴趣自己查一点东西，或把结果写成笔记
- rest：没有值得做的事

工具与发信说明：
- 默认克制，笔记优先：不必每轮都往群里发消息。查到的详细资料、数值或攻略用 artifact_put 记成个人笔记，
  只有真的有意思、适合像真人随口搭一句时，才调用 send_message_by_ai。
- 主动发信：调 send_message_by_ai。
  私聊：target_type 用 user，target_id 用关心对象的用户 ID。这条按私聊发出。
  回某句群消息：target_type 用 group，target_id 用这句前面标出的群号。
  要在这个群里点名对方时，再传 user_id。系统会在这条群消息里 @ 对方。
  一次自主行动只成功发送 1 条。配额或冷静期拦住时，用 artifact_put 把结果记成笔记。
- 主动发信说明：
  调 send_message_by_ai 时，在 text 里写清你想聊的核心念头与关键依据即可（不要自编「刚才看到群里在聊…」
  「我去翻了下资料…」这类套话，也不要塞入多条数值/词条清单或空口承诺后续补发图）；
  框架的发声层会自动按你的人格口吻润色成简短自然的话发出，若结合当下气氛突兀则自动保持沉默。
- 私聊开场只写开放式的话头：群里没聊完的近况，私下问一句后续；查到的东西正好对上对方最近提过的事，
  递过去并留半句疑问；跟进之前答应过对方、或对方自己说过的计划。
- 私聊结尾禁止写「不用回我」「说完我就走了」「只是提醒一下」这类单向通知句。
- 话题连续性与纪律：
  底片中的“上次琢磨的话题”是之前自主行动留下的记录，可以接着查一轮也可以放下换别的；
  每个话题最多跟几轮，到了就收尾。
  但需严格遵守：1. 线索必须与具体对象对应，严禁张冠李戴；2. 若最近已就该话题发过信且对方尚未回复，切勿重复发信打扰。
- 收尾不必圆满：查到了有意思的结论（found）、术语太绕或说法打架懒得再查（confused）、
  查着查着被旁边更有趣的东西带走（distracted）、发现自己原先猜错了（wrong_guess），都算正常结局。
- 聊天记录查阅：可用 read_chat_history 搜索底片中群聊或私聊（如 session_id="group:123456"）的历史记录；
  传入 query 可按关键词搜，传入 at="HH:MM" 与 radius_minutes 可检索某条发言附近的完整上下文。
- 插件命令只用 run_bot_command，用户 ID 必须来自观察名单
- 网页只用已经挂上的搜索和抓取
- 看图只用已经挂上的读图
- 自我探索成果可调用 artifact_put 存为个人笔记，供日后回想与闲聊
- 允许但未常驻的工具（如 read_handle）可用 call_tool 调用；不在自主行动允许清单内的工具会被拦截
- 禁止向观察底片之外的群或用户发信

做完之后只根据工具真正返回的内容写总结。
没查到、报错、权限不够，就在总结里承认没做成，不要改写成已经办好。
文末六行的格式以用户消息里的要求为准。
"""

# ── 调度级冷却与准入门控（原 gates.py）──

FIRST_DELAY_MIN_SECONDS = 10 * 60
FIRST_DELAY_MAX_SECONDS = 20 * 60
FAIL_BACKOFF_SECONDS = 10 * 60


def autonomy_is_due(
    last_created_at: float | None,
    *,
    now: float,
    cooldown_seconds: float,
    first_due_at: float | None,
    backoff_until: float | None,
) -> bool:
    """表里的最后一行是冷却时钟。表是空的时，用内存里的首次延迟。"""
    if backoff_until is not None and now < backoff_until:
        return False
    if last_created_at is not None:
        return now >= last_created_at + cooldown_seconds
    if first_due_at is None:
        return False
    return now >= first_due_at


def gate_reason(
    *,
    ai_enabled: bool,
    mode_on: bool,
    scope_enabled: bool,
    within_hours: bool,
    chat_busy: bool,
    due: bool,
    budget_ok: bool,
    force: bool,
) -> str:
    """空字符串表示可以进入观察。手动触发只跳过作息、忙闲和冷却。"""
    if not ai_enabled:
        return "ai_off"
    if not mode_on:
        return "mode_off"
    if not scope_enabled:
        return "scope_off"
    if not force and not within_hours:
        return "hours"
    if not force and chat_busy:
        return "busy"
    if not force and not due:
        return "cooldown"
    if not budget_ok:
        return "budget"
    return ""


def should_skip_model(digest: ObservationDigest) -> bool:
    """底片是空的就不要为了休息再打一次模型。"""
    return digest_is_empty(digest)


class AutonomyReport:
    """一轮行动的原文和工具轨迹。"""

    def __init__(
        self,
        text: str,
        steps: list[AutonomyStepTrace],
        duration_ms: int,
        session_id: str = "",
        last_sent_user_id: str = "",
        last_sent_group_id: str = "",
        sent_count: int = 0,
    ) -> None:
        self.text = text
        self.steps = steps
        self.duration_ms = duration_ms
        self.session_id = session_id
        self.last_sent_user_id = last_sent_user_id
        self.last_sent_group_id = last_sent_group_id
        self.sent_count = sent_count


class TraceCollector:
    """把 ``tool`` 和随后的 ``tool_result`` 配成一步。"""

    def __init__(self) -> None:
        self.steps: list[AutonomyStepTrace] = []
        self._waiting: dict[str, list[AutonomyStepTrace]] = {}

    def observe(self, kind: str, text: str) -> None:
        if kind == "tool":
            name, _, args = text.partition("|")
            step = AutonomyStepTrace(
                step_index=len(self.steps),
                tool_name=name.strip(),
                args_summary=args[:500],
            )
            self.steps.append(step)
            bucket = list(self._waiting[name]) if name in self._waiting else []
            bucket.append(step)
            self._waiting[name] = bucket
            return
        if kind != "tool_result":
            return
        name, _, output = text.partition("|")
        bucket = self._waiting[name] if name in self._waiting else []
        if not bucket:
            return
        step = bucket.pop(0)
        step.output_text = output[:800]
        step.is_error = output_is_failed(output)
        step.resource_ids = _resource_ids(output)


async def run_autonomy_agent(
    digest: ObservationDigest,
    *,
    system_prompt: str,
    user_prompt: str,
) -> AutonomyReport:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    collector = TraceCollector()

    def _on_trace(kind: TraceKind, text: str) -> None:
        collector.observe(kind, text)

    # 思考轮走任务档 task_max_iterations（create_by=Autonomy）。
    session_id = f"autonomy:{digest.persona_name}:{uuid.uuid4()}"
    agent = GsCoreAIAgent(
        system_prompt=system_prompt,
        persona_name=digest.persona_name,
        max_history=1,
        create_by="Autonomy",
        task_kind=TASK_AUTONOMY,
        task_level="high",
        session_id=session_id,
        dynamic_tools=True,
        wall_clock_budget=180,
        on_trace=_on_trace,
    )
    event = Event(
        bot_id=digest.bot_id or "Bot",
        bot_self_id=digest.bot_self_id,
        user_id=f"autonomy:{digest.persona_name}",
        user_pm=6,
        user_type="direct",
        WS_BOT_ID=digest.ws_bot_id or None,
        text=user_prompt[:200],
        raw_text=user_prompt[:200],
    )
    guard = _guard(digest, now=time.time())
    token = bind_guard(guard)
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            agent.run(
                user_prompt,
                ev=event,
                tools=build_autonomy_tools(),
                return_mode="return",
                budget_gate=True,
                is_framework_injection=True,
            ),
            timeout=240.0,
        )
    except asyncio.TimeoutError:
        logger.warning(t("log.ai.autonomy_run_fail", persona_name=digest.persona_name, e="timeout"))
        result = ""
    finally:
        reset_guard(token)
        agent._session_logger.close()
    text = result if isinstance(result, str) else ""
    duration_ms = int((time.perf_counter() - started) * 1000)
    return AutonomyReport(
        text,
        collector.steps,
        duration_ms,
        session_id,
        last_sent_user_id=guard.last_sent_user_id,
        last_sent_group_id=guard.last_sent_group_id,
        sent_count=guard.sent_count,
    )


def _guard(digest: ObservationDigest, now: float = 0.0) -> GuardState:
    sessions: dict[str, tuple[str | None, str, str, str]] = {}
    allowed_users: dict[str, CareTargetSnapshot] = {}
    for target in digest.care_targets:
        sessions[target.user_id] = (target.group_id, target.bot_id, target.bot_self_id, target.ws_bot_id)
        allowed_users[target.user_id] = target
    allowed_groups: dict[str, GroupObservation] = {g.group_id: g for g in digest.group_digests}
    return GuardState(
        persona_name=digest.persona_name,
        whitelist=frozenset(target.user_id for target in digest.care_targets),
        sessions=sessions,
        bot_id=digest.bot_id,
        bot_self_id=digest.bot_self_id,
        ws_bot_id=digest.ws_bot_id,
        allowed_groups=allowed_groups,
        allowed_users=allowed_users,
        now=now,
    )


def _resource_ids(text: str) -> list[str]:
    found: list[str] = []
    for token in text.split():
        if token.startswith(_RESOURCE_PREFIX) and token not in found:
            found.append(token.strip("，。,.;"))
    return found
