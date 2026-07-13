"""Agent Mesh Kanban · 周期触发桥（APScheduler ↔ 模板任务树）

设计意图
--------
Kanban 任务树本身是**一次性事件驱动**的——同一棵树跑完即结束，不会复用。
但用户常有"每个开盘日每半小时让你看盘并买卖、30 天后结算"这种**周期性、
跨多日的多步任务**。在过去框架只能退化为两条路：

1. 主人格挂一个 ``add_interval_task``，每次唤醒手动 ``register_kanban_task``——
   重复的能力评估开销、易出现"忘记触发"的认知失误。
2. 把任务塞进 ``state_set`` 的大 JSON 块——没有 Kanban 看板、没有 artifact、
   主人追问时无溯源原文。

本模块在 Kanban 之上加一层"模板根 + 克隆实例"语义：

- 主人格创建带 ``recurring_trigger`` 的根任务一次（即"模板"），框架自动挂上
  APScheduler；
- 每次到点，桥接器调 ``clone_tree_for_fire`` 复制一棵全新实例树并立刻
  ``kick_root`` 推进，实例树跑完即完结；
- 模板永远不被真正调度，只是被克隆的样板；
- 跨实例的持久化（账户/持仓/流水）走 ``record_*`` 工具——模板生命周期与
  数据生命周期解耦。

支持的触发格式
--------------
``recurring_trigger`` 字符串遵循下述简洁约定，避免主人格写 cron 写错：

- ``"interval:<seconds>"``：每隔 N 秒触发一次（N ≥ 60，防过密）；
- ``"cron:<minute> <hour> <day> <month> <day_of_week>"``：标准 5 段 cron 表达式，
  **星期字段按标准 crontab 语义**（0/7=周日、1=周一 … 6=周六），由
  :func:`_normalize_cron_dow` 翻译成 APScheduler 的编号；不写 day_of_week 时按全周计算。

例：``"interval:1800"``（每 30 分钟）、``"cron:30 9 * * 1-5"``
（周一至周五 9:30 触发一次）。
"""

import re
import inspect
from typing import TYPE_CHECKING, Tuple, Callable, Optional

if TYPE_CHECKING:
    from .models import AIAgentTask

from gsuid_core.aps import scheduler
from gsuid_core.i18n import t
from gsuid_core.logger import logger

_JOB_PREFIX = "kanban_recurring_"
# 子任务 not_before 唤醒的 APScheduler job id 前缀；与 recurring 模板共享调度器
# 但 id 命名空间分离，避免误删
_NOT_BEFORE_JOB_PREFIX = "kanban_not_before_"
# 子任务级周期触发的 APScheduler job id 前缀；与根任务级模板隔离
_SUBTASK_RECURRING_JOB_PREFIX = "kanban_subrecurring_"


# 周期触发前置门（recurring gate）：按 agent_profile 注册的业务日历谓词，
# 到点后先问 gate 再克隆/派代理。注册入口与完整语义见 register_recurring_gate。
_RECURRING_GATES: dict[str, Callable[[], object]] = {}


def register_recurring_gate(agent_profile: str, gate: Callable[[], object]) -> None:
    """为某个能力代理画像注册周期触发前置门。

    动机：cron 表达式只能表达"星期几/几点"，表达不了"A 股交易日""美股开盘"这类
    业务日历。没有 gate 时，节假日到点照样克隆实例树 → 派能力代理 → LLM 醒来一句
    "今天不开盘"再睡回去——纯浪费 token。``_fire_template`` / ``_fire_subtask_template``
    在克隆之前先问 gate，返回 False 则本次静默跳过（不克隆、不派代理、不消耗任何
    LLM token），下个 cron 周期再问。gate 抛异常按"放行"处理（fail-open——业务日历
    服务挂了不应让任务永久停摆）。

    之所以按 agent_profile 而不是 task_id 注册：profile 是稳定的能力语义（"这个代理
    只应在交易时段醒来"），跨实例树、跨群、跨重启都成立，且无需给 AIAgentTask 加列。
    注意根任务不挂画像（见 models.AIAgentTask.agent_profile），整树模板的 gate 判定
    由 :func:`_tree_gates_allow` 汇总全体子任务的画像完成。

    Args:
        agent_profile: 能力代理画像 id（如 ``papertrade_decision_agent``）。
        gate: 无参谓词，返回 bool（可为 async）。True=放行本次触发。
    """
    _RECURRING_GATES[agent_profile] = gate
    logger.info(t("📋 [Kanban] 周期触发 gate 已注册：agent_profile={agent_profile}", agent_profile=agent_profile))


async def _gate_allows(agent_profile: str) -> bool:
    """查询 gate；未注册 / gate 异常均放行。

    try/except 是对第三方插件回调的边界隔离（fail-open，日历服务挂了不停摆），
    并非类型兜底；不属于 §1.1 禁止的异常吞噬场景。
    """
    gate = _RECURRING_GATES.get(agent_profile or "")
    if gate is None:
        return True
    try:
        result = gate()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)
    except Exception as e:
        logger.warning(
            t(
                "📋 [Kanban] 周期 gate 执行异常（按放行处理）profile={agent_profile}: {e}",
                agent_profile=agent_profile,
                e=e,
            )
        )
        return True


async def _tree_gates_allow(template_root: "AIAgentTask") -> bool:
    """整树模板的前置门判定：任一子任务画像的 gate 拒绝即拦下整树。

    根任务不分配画像（agent_profile 恒为空，见 models.AIAgentTask），直接拿根问
    gate 永远放行、形同虚设；故取全体子任务的画像去重后逐一问。树内子任务通常
    有依赖关系，被 gate 的环节缺席时整树本次运行没有意义，因而"任一拒绝即拦"。
    """
    from .kanban import _query_children

    children = await _query_children(template_root.id)
    profiles = {c.agent_profile for c in children if c.agent_profile}
    if template_root.agent_profile:
        profiles.add(template_root.agent_profile)
    for profile in sorted(profiles):
        if not await _gate_allows(profile):
            logger.info(t("📋 [Kanban] 周期 gate 拒绝：profile={profile}", profile=profile))
            return False
    return True


def _job_id(template_root_id: str) -> str:
    return f"{_JOB_PREFIX}{template_root_id}"


def _not_before_job_id(subtask_id: str) -> str:
    return f"{_NOT_BEFORE_JOB_PREFIX}{subtask_id}"


def _subtask_recurring_job_id(subtask_id: str) -> str:
    return f"{_SUBTASK_RECURRING_JOB_PREFIX}{subtask_id}"


_WEEKDAY_NAME = "mon|tue|wed|thu|fri|sat|sun"
_WEEKDAY_NAME_RE = re.compile(rf"(?:{_WEEKDAY_NAME})(?:-(?:{_WEEKDAY_NAME}))?", re.IGNORECASE)


def _dow_token_bounds(body: str, has_step: bool) -> Tuple[int, int]:
    """解析星期字段里单个 token 的取值区间（标准 cron 编号，含端点）。"""
    text = body.strip()
    if text in ("", "*"):
        return 0, 7
    if "-" in text:
        head, _, tail = text.partition("-")
        return _dow_int(head), _dow_int(tail)
    first = _dow_int(text)
    # crontab 语义："2/2" 等价 "2-6/2"，单值带步长时右端开到周六
    return (first, 6) if has_step else (first, first)


def _dow_int(text: str) -> int:
    try:
        value = int(text.strip())
    except ValueError as e:
        raise ValueError(t("cron 星期字段必须是 0-7 的数字或英文名，收到：{p0}", p0=repr(text))) from e
    if not 0 <= value <= 7:
        raise ValueError(t("cron 星期字段超出 0-7 范围：{p0}", p0=value))
    return value


def _normalize_cron_dow(dow: str) -> str:
    """把标准 crontab 的星期编号翻译成 APScheduler 的星期编号。

    标准 crontab 是 0/7=周日、1=周一 … 6=周六；APScheduler 的 ``CronTrigger`` 却是
    0=周一 … 6=周日。不翻译的话，主人格按常识写下的 ``1-5``（本意周一至周五）会被
    APScheduler 读成周二至周六——周一永远不触发、周六白白唤醒一次。本函数把整个字段
    展开成显式的 APScheduler 编号列表，避免范围/步长在两套编号间错位。
    """
    text = (dow or "").strip()
    if not text or text == "*":
        return "*"
    if re.search(r"[A-Za-z]", text):
        # mon-fri 这类英文名两套编号语义一致，校验拼写后原样透传
        for token in text.split(","):
            if not _WEEKDAY_NAME_RE.fullmatch(token.strip()):
                raise ValueError(t("cron 星期字段的英文名非法：{p0}", p0=repr(token)))
        return text

    aps_days: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        body, sep, step_text = token.partition("/")
        try:
            step = int(step_text) if sep else 1
        except ValueError as e:
            raise ValueError(t("cron 星期字段步长必须是整数：{p0}", p0=repr(token))) from e
        if step < 1:
            raise ValueError(t("cron 星期字段步长必须 ≥ 1：{p0}", p0=repr(token)))

        first, last = _dow_token_bounds(body, bool(sep))
        span = list(range(first, last + 1)) if first <= last else [*range(first, 8), *range(0, last + 1)]
        for std in span[::step]:
            # 先把 7 折回 0（同为周日），再按 周一=0 的 APScheduler 编号平移
            aps_days.add((std % 7 + 6) % 7)

    if len(aps_days) == 7:
        return "*"
    return ",".join(str(d) for d in sorted(aps_days))


def parse_trigger_spec(spec: str) -> Tuple[str, dict]:
    """把 recurring_trigger 字符串解析为 APScheduler 的 trigger_type + 参数 dict。

    cron 的星期字段按标准 crontab 语义解释（见 :func:`_normalize_cron_dow`），
    输出的 ``day_of_week`` 已翻译为 APScheduler 编号，可直接喂给 ``add_job``。

    Returns:
        (trigger_type, kwargs)：trigger_type ∈ {"interval", "cron"}

    Raises:
        ValueError: 格式非法
    """
    text = (spec or "").strip()
    if text.startswith("interval:"):
        rest = text[len("interval:") :].strip()
        try:
            seconds = int(rest)
        except ValueError as e:
            raise ValueError(t("interval 必须是整数秒：{e}", e=e)) from e
        if seconds < 60:
            raise ValueError(t("interval 最小 60 秒（防过密触发）"))
        return "interval", {"seconds": seconds}
    if text.startswith("cron:"):
        rest = text[len("cron:") :].strip()
        parts = re.split(r"\s+", rest)
        if len(parts) == 5:
            minute, hour, day, month, dow = parts
            return "cron", {
                "minute": minute,
                "hour": hour,
                "day": day,
                "month": month,
                "day_of_week": _normalize_cron_dow(dow),
            }
        if len(parts) == 4:
            # 兼容省略 day_of_week
            minute, hour, day, month = parts
            return "cron", {
                "minute": minute,
                "hour": hour,
                "day": day,
                "month": month,
            }
        raise ValueError(t("cron 表达式需要 4 或 5 段：'<minute> <hour> <day> <month> [day_of_week]'"))
    raise ValueError(t("recurring_trigger 必须以 'interval:' 或 'cron:' 开头，收到：{spec}", spec=repr(spec)))


async def _fire_template(template_root_id: str) -> None:
    """APScheduler 回调：到点触发一次模板的克隆 + 调度。

    防御性逻辑：
    - 模板已 disarmed → 不触发并尝试取消 job；
    - 模板已过期（``recurring_until`` 已过） → disarm + 取消 job；
    - 任何异常都不向 APScheduler 抛，避免任务被框架自动暂停。
    """
    try:
        from datetime import datetime

        from .kanban import (
            disarm_template,
            clone_tree_for_fire,
        )
        from .models import AIAgentTask
        from .kanban_executor import kick_root

        template = await AIAgentTask.get_by_id(template_root_id)
        if template is None:
            logger.warning(
                t("📋 [Kanban] 周期触发：模板 {template_root_id} 不存在，取消 job", template_root_id=template_root_id)
            )
            unschedule_template(template_root_id)
            return
        if template.recurring_status != "armed":
            logger.info(
                t(
                    "📋 [Kanban] 周期触发：模板 {template_root_id} 状态={p0}，跳过",
                    template_root_id=template_root_id,
                    p0=template.recurring_status,
                )
            )
            unschedule_template(template_root_id)
            return
        if template.recurring_until is not None and template.recurring_until < datetime.now():
            logger.info(
                t(
                    "📋 [Kanban] 周期触发：模板 {template_root_id} 已过期，自动 disarm",
                    template_root_id=template_root_id,
                )
            )
            await disarm_template(template_root_id)
            unschedule_template(template_root_id)
            return

        # 前置门：按树内子任务画像汇总问 gate（根不挂画像），业务日历不满足
        # （如非交易时段）时静默跳过本次，不克隆、不派任何 LLM
        if not await _tree_gates_allow(template):
            logger.info(
                t(
                    "📋 [Kanban] 周期触发：模板 {template_root_id} 被 gate 拦截，本次跳过",
                    template_root_id=template_root_id,
                )
            )
            return

        instance_root, _ = await clone_tree_for_fire(template)
        await kick_root(instance_root.id)
    except Exception as e:
        logger.exception(
            t("📋 [Kanban] 周期触发异常 template={template_root_id}: {e}", template_root_id=template_root_id, e=e)
        )


def schedule_template(
    template_root_id: str,
    trigger_spec: str,
    *,
    end_date: Optional[str] = None,
) -> bool:
    """把模板挂到 APScheduler；已存在则替换。

    Args:
        template_root_id: 模板根任务 id；将作为 job_id 后缀。
        trigger_spec: recurring_trigger 字符串。
        end_date: ISO 时间字符串，用作 APScheduler 的 end_date（防止过期还触发）。

    Returns:
        True=成功挂上；False=trigger 解析失败或调度器异常。
    """
    try:
        trigger_type, kwargs = parse_trigger_spec(trigger_spec)
    except ValueError as e:
        logger.error(
            t("📋 [Kanban] 周期模板挂载失败 template={template_root_id}: {e}", template_root_id=template_root_id, e=e)
        )
        return False

    if end_date:
        kwargs["end_date"] = end_date

    job_id = _job_id(template_root_id)
    try:
        scheduler.add_job(
            func=_fire_template,
            trigger=trigger_type,
            id=job_id,
            name=f"kanban_recurring/{template_root_id}",
            replace_existing=True,
            args=[template_root_id],
            **kwargs,
        )
    except Exception as e:
        logger.exception(
            t(
                "📋 [Kanban] APScheduler add_job 失败 template={template_root_id}: {e}",
                template_root_id=template_root_id,
                e=e,
            )
        )
        return False
    logger.info(
        t(
            "📋 [Kanban] 周期模板已挂载 template={template_root_id} trigger={trigger_type} kwargs={kwargs}",
            template_root_id=template_root_id,
            trigger_type=trigger_type,
            kwargs=kwargs,
        )
    )
    return True


def unschedule_template(template_root_id: str) -> bool:
    """从 APScheduler 上摘掉模板的 job；不影响数据库里的 recurring_status。"""
    job_id = _job_id(template_root_id)
    try:
        scheduler.remove_job(job_id)
        return True
    except Exception as e:
        logger.debug(t("📋 [Kanban] 摘除周期 job 跳过（可能本就不存在）: {e}", e=e))
        return False


async def restore_armed_templates() -> int:
    """启动时把所有 ``recurring_status='armed'`` 的模板重新挂回 APScheduler。"""
    from .kanban import list_armed_templates

    templates = await list_armed_templates()
    restored = 0
    for tpl in templates:
        if not tpl.recurring_trigger:
            continue
        end_date_iso = tpl.recurring_until.isoformat() if tpl.recurring_until else None
        if schedule_template(tpl.id, tpl.recurring_trigger, end_date=end_date_iso):
            restored += 1
    if templates:
        logger.info(
            t(
                "📋 [Kanban] 启动期周期模板恢复：候选 {p0} 个，挂载成功 {restored} 个",
                p0=len(templates),
                restored=restored,
            )
        )
    return restored


# ─────────────────────────────────────────────────────────────────────
# 子任务级 not_before 唤醒
# ─────────────────────────────────────────────────────────────────────


async def _fire_not_before(subtask_id: str, root_task_id: str) -> None:
    """APScheduler 单次回调：子任务 not_before 到点，触发一次 kick_root。

    本回调只负责"叫醒"——是否真的派活由 ``get_ready_child_tasks`` 再判定一次
    （依赖未满足 / 已被人工 paused 等情况会继续被过滤掉）。
    """
    try:
        from .models import AIAgentTask
        from .kanban_executor import kick_root

        sub = await AIAgentTask.get_by_id(subtask_id)
        if sub is None:
            logger.debug(t("📋 [Kanban] not_before 唤醒：子任务 {subtask_id} 不存在，跳过", subtask_id=subtask_id))
            return
        if sub.status != "pending":
            logger.debug(
                t(
                    "📋 [Kanban] not_before 唤醒：子任务 {subtask_id} 状态={p0}，已不需要再叫醒",
                    subtask_id=subtask_id,
                    p0=sub.status,
                )
            )
            return
        logger.info(
            t(
                "📋 [Kanban] not_before 到点 → kick_root subtask={subtask_id} root={root_task_id}",
                subtask_id=subtask_id,
                root_task_id=root_task_id,
            )
        )
        await kick_root(root_task_id)
    except Exception as e:
        logger.exception(t("📋 [Kanban] not_before 唤醒异常 subtask={subtask_id}: {e}", subtask_id=subtask_id, e=e))


def schedule_not_before_wakeup(
    subtask_id: str,
    root_task_id: str,
    not_before,
) -> bool:
    """给一个子任务的 ``not_before`` 时间点挂一个 APScheduler 单次 date job。

    到点后调 ``_fire_not_before`` 触发 ``kick_root(root_task_id)``，
    随后 ``get_ready_child_tasks`` 自然把这个子任务放进 ready 集合。

    Args:
        subtask_id: 子任务 id（用作 job id 后缀）。
        root_task_id: 所属根任务 id，到点 kick。
        not_before: 绝对时间（``datetime``）。

    Returns:
        True 挂载成功；False 时间已过或调度器异常（已过期时不挂，但调用方仍应让
        ``get_ready_child_tasks`` 立刻把它当成"可派出"）。
    """
    from datetime import datetime

    if not_before is None:
        return False
    now = datetime.now()
    if not_before <= now:
        return False
    job_id = _not_before_job_id(subtask_id)
    try:
        scheduler.add_job(
            func=_fire_not_before,
            trigger="date",
            run_date=not_before,
            id=job_id,
            name=f"kanban_not_before/{subtask_id}",
            replace_existing=True,
            args=[subtask_id, root_task_id],
        )
    except Exception as e:
        logger.exception(
            t(
                "📋 [Kanban] 子任务 not_before APScheduler add_job 失败 subtask={subtask_id}: {e}",
                subtask_id=subtask_id,
                e=e,
            )
        )
        return False
    logger.info(
        t(
            "📋 [Kanban] not_before 已挂载 subtask={subtask_id} root={root_task_id} fire_at={p0}",
            subtask_id=subtask_id,
            root_task_id=root_task_id,
            p0=not_before.isoformat(),
        )
    )
    return True


def unschedule_not_before_wakeup(subtask_id: str) -> bool:
    """摘掉某子任务的 not_before job（如重派 / 整树终结时）。"""
    job_id = _not_before_job_id(subtask_id)
    try:
        scheduler.remove_job(job_id)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# 子任务级 recurring 触发桥
# ─────────────────────────────────────────────────────────────────────


async def _fire_subtask_template(subtask_id: str, root_task_id: str) -> None:
    """APScheduler 回调：周期子任务模板到点 → 克隆一个执行实例子任务 + kick_root。

    防御性逻辑同根任务级模板：模板已 disarmed / 过期 / 根任务已终结 → 不触发并
    尝试摘除 job。
    """
    try:
        from .kanban import clone_subtask_for_fire, disarm_subtask_template
        from .models import AIAgentTask
        from .kanban_executor import kick_root

        sub = await AIAgentTask.get_by_id(subtask_id)
        if sub is None:
            logger.warning(t("📋 [Kanban] 周期子任务触发：模板 {subtask_id} 不存在，取消 job", subtask_id=subtask_id))
            unschedule_subtask_template(subtask_id)
            return
        if sub.recurring_status != "armed":
            logger.info(
                t(
                    "📋 [Kanban] 周期子任务触发：模板 {subtask_id} 状态={p0}，跳过",
                    subtask_id=subtask_id,
                    p0=sub.recurring_status,
                )
            )
            unschedule_subtask_template(subtask_id)
            return
        # 模板自身过期 → disarm
        from datetime import datetime

        if sub.recurring_until is not None and sub.recurring_until < datetime.now():
            logger.info(t("📋 [Kanban] 周期子任务 {subtask_id} 已过期，自动 disarm", subtask_id=subtask_id))
            await disarm_subtask_template(subtask_id)
            return

        # 前置门：业务日历不满足（如非交易时段/节假日）时静默跳过本次触发，
        # 不克隆实例、不派能力代理；下个 cron 周期再判
        if not await _gate_allows(sub.agent_profile):
            logger.info(
                t(
                    "📋 [Kanban] 周期子任务触发：{subtask_id} 被 gate 拦截（profile={p0}），本次跳过",
                    subtask_id=subtask_id,
                    p0=sub.agent_profile,
                )
            )
            return

        logger.info(
            t(
                "📋 [Kanban] 周期子任务开火 subtask={subtask_id} profile={p0}",
                subtask_id=subtask_id,
                p0=sub.agent_profile,
            )
        )
        instance = await clone_subtask_for_fire(sub)
        if instance is None:
            return
        await kick_root(root_task_id)
    except Exception as e:
        logger.exception(t("📋 [Kanban] 周期子任务触发异常 subtask={subtask_id}: {e}", subtask_id=subtask_id, e=e))


def schedule_subtask_template(
    subtask_id: str,
    root_task_id: str,
    trigger_spec: str,
    *,
    end_date: Optional[str] = None,
) -> bool:
    """把周期子任务模板挂到 APScheduler；已存在则替换。

    Args:
        subtask_id: 模板子任务 id（用作 job_id 后缀）。
        root_task_id: 所属根任务 id，每次 fire 时 kick。
        trigger_spec: recurring_trigger 字符串（interval:N 或 cron:m h dom mon dow）。
        end_date: ISO 时间字符串，用作 APScheduler 的 end_date。

    Returns:
        True=挂载成功；False=trigger 解析失败或调度器异常（调用方应转 disarmed）。
    """
    try:
        trigger_type, kwargs = parse_trigger_spec(trigger_spec)
    except ValueError as e:
        logger.error(t("📋 [Kanban] 周期子任务挂载失败 subtask={subtask_id}: {e}", subtask_id=subtask_id, e=e))
        return False

    if end_date:
        kwargs["end_date"] = end_date

    job_id = _subtask_recurring_job_id(subtask_id)
    try:
        scheduler.add_job(
            func=_fire_subtask_template,
            trigger=trigger_type,
            id=job_id,
            name=f"kanban_subrecurring/{subtask_id}",
            replace_existing=True,
            args=[subtask_id, root_task_id],
            **kwargs,
        )
    except Exception as e:
        logger.exception(
            t("📋 [Kanban] APScheduler add_job 失败 subtask={subtask_id}: {e}", subtask_id=subtask_id, e=e)
        )
        return False
    logger.info(
        t(
            "📋 [Kanban] 周期子任务已挂载 subtask={subtask_id} root={root_task_id}"
            " trigger={trigger_type} kwargs={kwargs}",
            subtask_id=subtask_id,
            root_task_id=root_task_id,
            trigger_type=trigger_type,
            kwargs=kwargs,
        )
    )
    return True


def unschedule_subtask_template(subtask_id: str) -> bool:
    """从 APScheduler 上摘掉周期子任务的 job；不影响数据库里 recurring_status。"""
    job_id = _subtask_recurring_job_id(subtask_id)
    try:
        scheduler.remove_job(job_id)
        return True
    except Exception as e:
        logger.debug(t("📋 [Kanban] 摘除周期子任务 job 跳过（可能本就不存在）: {e}", e=e))
        return False


async def restore_armed_subtask_templates() -> int:
    """启动期把所有 ``recurring_status='armed'`` 的周期子任务模板重新挂回 APScheduler。"""
    from .kanban import list_armed_subtask_templates

    templates = await list_armed_subtask_templates()
    restored = 0
    for tpl in templates:
        if not tpl.recurring_trigger or not tpl.root_task_id:
            continue
        end_date_iso = tpl.recurring_until.isoformat() if tpl.recurring_until else None
        if schedule_subtask_template(tpl.id, tpl.root_task_id, tpl.recurring_trigger, end_date=end_date_iso):
            restored += 1
    if templates:
        logger.info(
            t(
                "📋 [Kanban] 启动期周期子任务恢复：候选 {p0} 个，挂载成功 {restored} 个",
                p0=len(templates),
                restored=restored,
            )
        )
    return restored


async def restore_pending_not_before_wakeups() -> int:
    """启动时把所有 pending 且 ``not_before > now`` 的子任务重新挂回 APScheduler。

    启动崩溃恢复路径：进程挂掉时 APScheduler 内存表丢了，但数据库里 not_before
    还在；不补这一步会让"未到点的子任务"永远等不到唤醒。
    """
    from datetime import datetime

    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker

    from .models import AIAgentTask

    now = datetime.now()
    async with async_maker() as session:
        stmt = (
            select(AIAgentTask)
            .where(col(AIAgentTask.node_kind) == "subtask")
            .where(col(AIAgentTask.status) == "pending")
            .where(col(AIAgentTask.not_before).is_not(None))
            .where(col(AIAgentTask.not_before) > now)
        )
        result = await session.execute(stmt)
        subs = list(result.scalars().all())

    restored = 0
    for s in subs:
        if s.not_before is None or not s.root_task_id:
            continue
        if schedule_not_before_wakeup(s.id, s.root_task_id, s.not_before):
            restored += 1
    if subs:
        logger.info(
            t(
                "📋 [Kanban] 启动期 not_before 唤醒恢复：候选 {p0} 个，挂载 {restored} 个",
                p0=len(subs),
                restored=restored,
            )
        )
    return restored
