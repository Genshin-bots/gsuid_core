"""Agent Mesh Kanban · LLM 工具集（暴露给主人格）。

工具列表：

- ``evaluate_agent_mesh_capability``：能力评估前置（必须先于 register_kanban_task）
- ``register_kanban_task``        ：注册任务树（强校验最近一次评估 covered=true）
- ``respawn_subtask``             ：复活 failed 子任务（最多 N 次后强制审批）
- ``fail_task_tree``              ：明确终结整树
- 子任务审批转达已统一到 ``respond_approval``（buildin_tools/approval_tools.py）
- ``artifact_put`` / ``artifact_get`` / ``artifact_list``：Artifact Hub 工具
- ``artifact_get_recent``         ：取根任务最近一份 artifact 原文（追问溯源）

设计原则：
- 无 UUID：任务引用走自然语言句柄；artifact 是显式 ``res_xxx`` 句柄。
- 权限：默认 owner / master 可操作；artifact 跨 root_task_id 严格隔离。
"""

import re
import json
import time
from typing import Any, Dict, List, Optional

from pydantic import Field, BaseModel
from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

from . import kanban
from .models import AIAgentTask, AIAgentArtifact
from .runtime import get_plan_context
from .resolver import resolve_task_ref
from .workspace import put_artifact
from ..capability_agents.evaluator import _FUZZY_MIN_OVERLAP

_CAP = "长期任务编排"

# register_kanban_task 循环防护：per-owner 时间戳列表（最近一次调用清理）
_REGISTER_KANBAN_RECENT: Dict[str, List[float]] = {}
_REGISTER_KANBAN_WINDOW_SEC = 60.0
# 上限从 3 提到 5：每分钟 5 次仍能拦住"模型卡住反复重试"的死循环，但允许主人格
# 在「子任务级 recurring 没合上的过渡期」一次性创建 2-3 棵相关任务树（如旧版"三棵
# 树"模式临时存在的兼容场景）。同 evaluation 命中且语义合法的连续 register 不再
# 误判为循环（见 ``_REGISTER_KANBAN_SAME_EVAL_EXEMPT``）。
_REGISTER_KANBAN_LIMIT_IN_WINDOW = 5
# "同 evaluation 短窗内的合法连续 register" 豁免：当一次 evaluate 已经成功命中、
# 当前 register 的 goal 跟最近一次 evaluate 重叠率超过阈值（沿用 evaluator 的
# _FUZZY_MIN_OVERLAP）时，本次调用**不计入** rate-limit 窗口；否则计入。
# 用于解决"主人格按 evaluator 输出顺序串行建多棵相关树时被无辜限流"的常见错杀。
_REGISTER_KANBAN_SAME_EVAL_EXEMPT = True
# same-eval 放宽上限（不再无限豁免）：合法多树够用，但 register→fail→register 失败环
# 实测 9~11 次必被此上限钉死。修复前 same-eval 会跳过上限且不记时间戳→永久旁路（见 README B2）。
_REGISTER_KANBAN_SAME_EVAL_LIMIT = 8

# 限流诊断：每次 register 返回拒绝时把"原因短码"塞进 owner 的最近原因栈，
# 触发硬限流时按高频原因给出对症诊断（之前文案硬编码"recurring_trigger 反复传 null"
# 误导主人格——实测 17ed4f85 真正原因是 evaluator 解析失败）。
# 短码集合：
#   "eval_miss"       - 找不到匹配的近期 evaluate（含模糊匹配失败）
#   "eval_failed"     - 最近 evaluate covered=false
#   "dup_active_root" - 同主题活跃根任务已存在
#   "recurring_miss"  - goal 含周期意图但 recurring_trigger=None 且无 not_before
#   "bad_args"        - 子任务字段非法（agent_profile 未注册 / not_before 非 ISO）
_REGISTER_KANBAN_REJECT_REASONS: Dict[str, List[str]] = {}
_REGISTER_KANBAN_REASON_KEEP = 6  # 每 owner 最多保留最近 N 条原因短码

# 周期意图关键词：goal 含这些字眼但 recurring_trigger=None 时回警告
# 不强制阻塞——主人 / 一次性"30 天后给我总账"也合法（一次性兜底）
# 关键词是**域无关**的——既匹配通用日常表述（每天 / 每周一），也匹配各行业
# 习惯说法（每开盘 / 每节课 / 每班次）；新增其它领域常见词只需追加 alt。
_RECURRING_HINTS_RE = re.compile(
    r"(每天|每日|每周|每月|每隔|每开盘|每节|每班|每场|每轮|"
    r"每[一二三四五六七八九十0-9]+(分钟|小时|天|周)|"
    r"持续\s*\d+\s*(天|周|月)|每.{0,6}次|周期|定时|recurring|每.{0,4}触发|每.{0,4}执行|"
    r"cron)",
    re.IGNORECASE,
)


def _record_register_reject(owner_id: str, reason_code: str) -> None:
    """把本次拒绝原因短码塞进 owner 的栈，保留最近 N 条。供限流诊断使用。"""
    stack = _REGISTER_KANBAN_REJECT_REASONS.setdefault(owner_id, [])
    stack.append(reason_code)
    while len(stack) > _REGISTER_KANBAN_REASON_KEEP:
        stack.pop(0)


def _diagnose_register_loop(owner_id: str) -> str:
    """按 owner 最近的拒绝原因高频项给出对症诊断文案。

    没记录到原因时退回到通用提示。**不要**硬编码任何单一原因——主人格信你这条诊断
    决定下一步行动，错诊会把人格带进死循环（实测 17ed4f85 evaluator 输出多 JSON
    被误诊为 recurring_trigger 缺失）。
    """
    stack = _REGISTER_KANBAN_REJECT_REASONS.get(owner_id, [])
    if not stack:
        return (
            "**停下来检查 args**：先看最近一次工具返回的拒绝文本（不是限流文本），"
            "里面会写明 evaluator / args / 周期 / 重复哪个问题；按该指示修正后再试。"
        )
    # 按出现次数计数，取 top
    from collections import Counter

    counts = Counter(stack)
    top_code, _ = counts.most_common(1)[0]
    if top_code == "eval_miss":
        return (
            "**最近多次拒绝原因 = 找不到匹配的 evaluate**：要么是评估过期了"
            "（TTL 15 分钟），要么是 goal 跟评估时用的描述差别太大。请重新"
            "`evaluate_agent_mesh_capability(user_goal=...)`，**user_goal 用一句"
            "覆盖整棵任务树意图的话**（不是子任务描述），再 register。"
        )
    if top_code == "eval_failed":
        return (
            "**最近多次拒绝原因 = evaluator 判 covered=false**：框架确实缺该域能力。"
            "请如实告诉主人缺什么、建议装哪个插件，**不要**继续 register 同一棵树。"
        )
    if top_code == "dup_active_root":
        return (
            "**最近多次拒绝原因 = 同主题活跃根任务已存在**：请改用 `respawn_subtask` "
            "修参数，或先 `fail_task_tree` 终结旧树再重建。"
        )
    if top_code == "recurring_miss":
        return (
            "**最近多次拒绝原因 = goal 含周期意图但没传 recurring_trigger**：周期"
            '任务必须传字符串如 `recurring_trigger="cron:0,30 9-14 * * 1-5"`，'
            "或给每个子任务加 `not_before` ISO 时间。一次性立即执行则显式传 "
            "`confirm_one_shot=True` 跳过校验。"
        )
    if top_code == "bad_args":
        return (
            "**最近多次拒绝原因 = 子任务字段非法**：检查每个子任务的 `agent_profile` "
            "是否在 evaluator 返回的 available_profiles 里、`not_before` 是否标准 ISO 时间。"
        )
    return "**停下来检查 args**：按上一次工具拒绝文本里的具体原因修正后再试。"


class KanbanSubtaskSpec(BaseModel):
    """register_kanban_task 入参——单个子任务描述。"""

    description: str = Field(..., description="子任务一句话描述")
    agent_profile: str = Field(..., description="必须是已注册的 profile_id")
    depends_on: List[int] = Field(
        default_factory=list,
        description="依赖的兄弟子任务下标（0-based，引用本数组中位置）",
    )
    params_hint: Optional[Dict[str, Any]] = Field(None, description="补充参数，调度时拼进任务文本")
    not_before: Optional[str] = Field(
        None,
        description=(
            "子任务最早可派出的绝对时间（ISO 字符串，如 '2026-05-26T09:30:00'）；"
            "在此之前框架不会调度，到点后下一次 kick_root 会自然拉起。用于一次性"
            "「等到某个绝对时刻再跑」语义（如等业务时段开始 / 等用户回家 / 等 N 小时后）；"
            "多次重复触发请用本字段 `recurring_trigger`（**子任务级周期**——一棵树里"
            "既有一次性 init 子任务也有周期触发子任务），不要在子任务上叠 not_before "
            "模拟周期。"
        ),
    )
    recurring_trigger: Optional[str] = Field(
        None,
        description=(
            "**子任务级周期触发**：让本子任务变成"
            "「持续 armed、到点开火、每次开火克隆一个执行实例子任务跑一遍」的"
            "**周期模板子任务**。格式同根任务级 recurring_trigger："
            "`interval:<秒>`（最少 60 秒）或 `cron:<分> <时> <日> <月> <周>`（5 段）。"
            "\n\n配合 ``recurring_until`` 可设过期时间。**核心用法**：把"
            "「持久化状态 + 周期更新 + 最终汇总」类任务做成一棵树：①一次性 init 子任务"
            "（depends_on=[]，立即跑）→ ②周期触发子任务（depends_on=[0]，init 完成后"
            "armed 等到点 cron 触发）→ ③一次性 final 子任务（depends_on=[]，加"
            "`not_before` 设结算时刻；**不要 depends_on 周期子任务**——周期子任务"
            "永远 armed 不 completed，下游死锁）。一棵树包完整生命周期，告别旧版"
            "「三棵树」拆解的笨拙折中。"
        ),
    )
    recurring_until: Optional[str] = Field(
        None,
        description=(
            "子任务级周期触发的过期时间（ISO 字符串，如 '2026-06-24T15:30:00'）。"
            "到点后自动 disarm，不再 fire。仅当本子任务带 `recurring_trigger` 时有意义；"
            "不传则永远 armed 直到主人手动 disarm 或整树 cancel。"
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# 能力评估
# ─────────────────────────────────────────────────────────────────────


@ai_tools(category="common", capability_domain=_CAP)
async def evaluate_agent_mesh_capability(
    ctx: RunContext[ToolContext],
    user_goal: str,
) -> str:
    """调用内部 capability_evaluator，对复合多代理任务做"现有能力是否覆盖"评估。

    必须在 register_kanban_task 之前调用。返回结构化 JSON（已字符串化）：
    - covered: bool
    - missing_capabilities: list[str]
    - available_profiles: list[str]
    - suggested_subtasks: list[{description, required_capability, agent_profile,
      depends_on, params_hint}]
    - risk_notes: list[str]
    - summary: str

    主人格行为约束：
    - covered=false 时**禁止**调用 register_kanban_task；如实告诉主人"能力不足"。
    - covered=true 时，根据 suggested_subtasks 调用 register_kanban_task 创建任务树。
    - 严禁把评估结果写入长期人格记忆——这只是任务编排过程的中间态。
    """
    ev = ctx.deps.ev
    if ev is None:
        return json.dumps(
            {
                "covered": False,
                "missing_capabilities": ["session"],
                "available_profiles": [],
                "suggested_subtasks": [],
                "risk_notes": ["无法获取会话信息"],
                "summary": "评估失败",
            },
            ensure_ascii=False,
        )

    persona_name = ""
    try:
        from gsuid_core.ai_core.persona import persona_config_manager

        persona_name = persona_config_manager.get_persona_for_session(ev.session_id) or ""
    except ImportError:
        pass

    from gsuid_core.ai_core.capability_agents.evaluator import evaluate_capability

    result = await evaluate_capability(
        user_goal=user_goal,
        owner_user_id=str(ev.user_id),
        persona_name=persona_name,
    )
    return json.dumps(result.to_dict(), ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────
# 注册任务树
# ─────────────────────────────────────────────────────────────────────


@ai_tools(category="planning", capability_domain=_CAP)
async def register_kanban_task(
    ctx: RunContext[ToolContext],
    goal: str,
    subtasks: List[KanbanSubtaskSpec],
    broadcast_to_group: bool = False,
    recurring_trigger: Optional[str] = None,
    recurring_until: Optional[str] = None,
    confirm_one_shot: bool = False,
) -> str:
    """注册一棵 Kanban 任务树（主任务 + N 个子任务节点）。

    **⚠️ 周期任务请直接传 `recurring_trigger`（cron / interval 两种格式），
    不要枚举 add_once_task —— 后者一定撞 20 个待执行任务硬上限。**

    使用前必须先调用 evaluate_agent_mesh_capability，且最近一次评估 covered=true。
    每个子任务必须分配一个**已注册**的 agent_profile。

    **两种模式**：
    1. **一次性任务**（默认，`recurring_trigger=None`）：创建后立即触发一次
       ``kick_root``，把全部可跑子任务并发派出；事件驱动接力推进直至完结。
       单个子任务可带 ``not_before="2026-05-26T09:30:00"`` 把派出时间推迟到
       绝对时间点——适合"初始化完成后等到指定时刻才开始执行"这类一次性延后
       （如等业务时段开始、等用户下班、等次日清晨等）。
    2. **周期模板**（`recurring_trigger` 非空）：根任务存为**模板**，自身不
       执行；按 ``recurring_trigger`` 挂上 APScheduler，到点克隆出全新实例
       任务树并执行。模板可持续触发，直到 ``recurring_until`` 过期或主人手动
       disarm。**适用于"每个工作日定时多步执行""每周一出周报""每日早晚打卡"
       等周期性多步任务**——cron 表达式按用户描述的实际时段写。注意：周期模板
       克隆出实例时**不复制 not_before**——周期 cron 已决定开火时间，子任务级
       再叠 not_before 没意义。

    **周期触发示范**（最容易被错走 add_once_task 枚举的场景）：

        # 「持久化状态 + 周期更新 + 最终汇总」三棵树模板（与具体业务无关）：
        # ⓐ 一次性初始化主集合 → ⓑ 周期模板每个触发点执行一次更新 → ⓒ 截止日
        # 一次性汇总。cron 表达式按用户描述的时段写，下例只是示意结构。

        register_kanban_task(  # ⓐ 一次性：建好任务要维护的 record 主集合
            goal="<任务简称> 状态初始化",
            subtasks=[
                {"description": "用 record_put 建本任务要维护的所有集合 "
                                "（账户 / 打卡日历 / 进度表 / 客户名单等）",
                 "agent_profile": "code_agent"},
            ],
        )
        register_kanban_task(  # ⓑ 周期模板：每个触发点克隆一棵实例执行一次
            goal="<任务简称> 周期执行",
            subtasks=[
                {"description": "查当前状态 → 决策或采集 → record_append 写流水 "
                                "→ 必要时 record_update 更新主表 → 汇报本次",
                 "agent_profile": "<evaluate 返回的业务画像 id>"},
            ],
            recurring_trigger="cron:<根据用户时段反推>",  # 按 §周期触发提示
            recurring_until="<截止日 ISO>",
        )
        register_kanban_task(  # ⓒ 一次性：截止日后用 not_before 自动派出
            goal="<任务简称> 最终汇总",
            subtasks=[
                {"description": "record_list 拉流水 + record_summary 算关键指标 "
                                "→ render_markdown_to_image 出报告",
                 "agent_profile": "internal_reporter",
                 "not_before": "<结算时刻 ISO>"},
            ],
        )
        # ⚠ 禁忌：① 把"初始化"塞进周期模板——每次开火都会清空主集合；
        #         ② 用 20 个 add_once_task 枚举每个触发点——撞 20 个任务硬上限；
        #         ③ 在主人格侧自己写决策循环——必须用 Kanban 周期模板由框架克隆。

    Args:
        goal: 任务树总目标。
        subtasks: 子任务描述列表（KanbanSubtaskSpec 结构）。
        broadcast_to_group: 是否允许把进展播报到当前群。
        recurring_trigger: 周期触发规则，留空表示一次性任务。**只要用户描述含
            "每 / 每天 / 每隔 / 每开盘日 / 持续 N 天"等周期意图，必须传本字段**——
            不要外挂 add_once_task / add_interval_task。格式：
            - ``"interval:<seconds>"``（最小 60 秒，防过密）
            - ``"cron:<minute> <hour> <day> <month> <day_of_week>"``（标准 5 段 cron）
        recurring_until: 周期模式下的失效时间（ISO 字符串，如 "2026-06-21T15:00:00"）；
            留空表示不过期，需主人手动 disarm。
        confirm_one_shot: **跳过周期意图强校验的逃生口**。当 goal 含 "每天 / 每开盘 /
            每隔" 等周期关键词、但你确认就是要"立刻一次性"执行（如"现在演示一次每日
            体检流程"）、且你不需要周期托管时，传 True 跳过强校验。**绝大多数情况
            下不要传 True**——周期任务一律应当走 `recurring_trigger`；只是想延迟
            一次执行的应当用子任务的 `not_before` 字段。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息，Kanban 任务树创建失败。"
    if not subtasks:
        return "⚠️ 子任务列表为空，无法创建任务树。"
    if len(subtasks) > kanban.DEFAULT_MAX_SUBTASKS:
        return f"⚠️ 子任务数超过上限 {kanban.DEFAULT_MAX_SUBTASKS}。"

    # 0) 循环防护：60 秒内同 owner 调用本工具 ≥ N 次直接拒绝（默认 5）。
    # 用户案例（2026-05-24 session 7a29c54d）显示主人格因模型 schema 误解反复
    # register_kanban_task(recurring_trigger=None) → fail_task_tree → 再 register，
    # 框架必须把闸刀拉死，否则一直产生孤儿子任务 + 大量 relay 会话日志。
    # 但"同一次 evaluation 触发的多次合法 register"（如串行建几棵相关任务树）
    # 不应计入窗口——见下方 same-eval 豁免逻辑。
    owner_id = str(ev.user_id)
    now = time.time()
    history = [t for t in _REGISTER_KANBAN_RECENT.get(owner_id, []) if now - t <= _REGISTER_KANBAN_WINDOW_SEC]

    # 1) 必须先评估——缓存按 owner 分组、命中策略先精确后模糊（token 重叠率 ≥ _FUZZY_MIN_OVERLAP）
    # 见 capability_agents/evaluator.py:get_recent_evaluation 注释。
    from gsuid_core.ai_core.capability_agents.evaluator import (
        _tokenize_for_overlap,  # 复用同一套分词避免行为分裂
        get_recent_evaluation,
        list_recent_evaluations_for_owner,
    )

    # 先尝试模糊匹配命中的 eval（用于判断本次 register 是否豁免限流）
    matched_eval_for_rate_check = get_recent_evaluation(owner_id, goal)
    same_eval = _REGISTER_KANBAN_SAME_EVAL_EXEMPT and matched_eval_for_rate_check is not None
    # 修复 B2：same-eval 改为「放宽上限」而非「完全豁免」，且无论是否 same-eval 都照常记入
    # 窗口——否则失败环借"每次 evaluate 刷新一条模糊匹配"永久旁路计数。
    effective_limit = _REGISTER_KANBAN_SAME_EVAL_LIMIT if same_eval else _REGISTER_KANBAN_LIMIT_IN_WINDOW

    if len(history) >= effective_limit:
        _REGISTER_KANBAN_RECENT[owner_id] = history  # 清理过期
        diag = _diagnose_register_loop(owner_id)
        return (
            f"⚠️ 你已在 {int(_REGISTER_KANBAN_WINDOW_SEC)} 秒内调用 "
            f"register_kanban_task {len(history)} 次，超过 "
            f"{effective_limit} 次上限——这通常是因为参数有问题"
            "导致 register-fail 循环。\n\n"
            f"{diag}\n\n"
            "通用提示：(1) 如果只想修已存在子任务参数请改用 `respawn_subtask` 或 "
            "webconsole，不要 fail+recreate；(2) 如果反复同主题失败请如实告诉主人"
            "你做不到，让主人决定下一步，不要继续重试。\n"
            "(3) 想在一棵树里同时表达「init + 周期更新 + 最终汇总」请用**子任务级**"
            "`recurring_trigger`（KanbanSubtaskSpec.recurring_trigger 字段）——一棵树"
            "包完整生命周期，不需要拆多棵树。"
        )
    # 关键修复：无论 same-eval 与否都记入窗口（豁免不再跳过计数）
    history.append(now)
    _REGISTER_KANBAN_RECENT[owner_id] = history

    eval_result = matched_eval_for_rate_check or get_recent_evaluation(owner_id, goal)
    if eval_result is None:
        recents = list_recent_evaluations_for_owner(owner_id)
        if recents:
            hints = "; ".join(f"「{r.user_goal[:30]}」" for r in recents[:3])
            _record_register_reject(owner_id, "eval_miss")
            return (
                f"⚠️ 当前 goal 与最近评估不匹配（模糊重叠率 < {_FUZZY_MIN_OVERLAP}），"
                "请重新调用 evaluate_agent_mesh_capability 评估这个具体目标。\n"
                "**提示**：evaluate 的 user_goal 用一句**覆盖整棵树意图**的话即可，"
                "不需要为每个子任务单独评估——同一棵任务树的 register goal 与 "
                "evaluate user_goal 通常表述非常接近。\n"
                f"近期评估过的任务：{hints}"
            )
        _record_register_reject(owner_id, "eval_miss")
        return "⚠️ 请先调用 evaluate_agent_mesh_capability 评估能力覆盖，否则不允许创建 Kanban 任务树。"
    if not eval_result.covered:
        _record_register_reject(owner_id, "eval_failed")
        return (
            "⚠️ 最近一次能力评估 covered=false（"
            f"缺失能力：{eval_result.missing_capabilities}）；"
            "禁止创建任务树。请如实告诉主人缺什么。"
        )

    # 1.5) 重复根任务防护：owner 名下若已存在"活跃且 goal 文本高重叠"的根任务，
    # 直接拒绝新建，引导主人格走 respawn / fail_task_tree。实测会话 b8cf57ca 一次
    # 对话里连开了任务#1（3 子任务）和任务#5（1 子任务）两棵同主题长期任务树，
    # 任务#5 是任务#1 的子集；主人看到看板时有两条根条目，状态错乱。
    own_toks = _tokenize_for_overlap(goal)
    if own_toks:
        active_roots = await AIAgentTask.list_for_owner(owner_id, only_active=True, root_only=True)
        for existing in active_roots:
            their = _tokenize_for_overlap(existing.goal or "")
            if not their:
                continue
            denom = min(len(own_toks), len(their))
            if denom == 0:
                continue
            overlap = len(own_toks & their) / denom
            # 阈值 0.6：比 evaluator 模糊匹配（0.45）严，避免误伤"扩展同主题但实际
            # 子任务结构差异大"的合法新建；同时足以拦住"同主题反复 register"循环
            if overlap >= 0.6:
                _record_register_reject(owner_id, "dup_active_root")
                return (
                    f"⚠️ 已存在同主题活跃根任务【任务#{existing.ordinal}｜"
                    f"{existing.display_name}】(goal 重叠率 {overlap:.0%})；"
                    "**禁止重复创建**——同一主题的长期任务在 owner 名下只应有一棵"
                    "根任务树。如果想改子任务结构请用 `respawn_subtask`；如果想换思路"
                    "请先 `fail_task_tree` 终结旧树再重建；如果只是想触发一次新执行"
                    "请等周期触发或在 webconsole 手动 kick。"
                )

    # 1.6) 周期意图强校验：goal 含周期关键词，但根级 recurring_trigger=None、
    # 所有子任务也都没带 `not_before`、且没有任何子任务带 `recurring_trigger` 时
    # ——直接拒绝。早先版本仅给软警告就允许创建，导致实测会话 17ed4f85 中
    # "虚拟盘每日看盘"在周日（非开盘日）被立刻派给 stock_agent 执行了一次错误
    # 交易决策。
    # 这里通用拦截"看起来要周期执行但忘了表达周期"的所有领域（股票 / 健康打卡 /
    # 学习计划 / 销售追踪等都受益）。**新版子任务级 `recurring_trigger`** 也算合法
    # 表达——只要任意子任务带 recurring_trigger 即视为已配置周期，不再拒绝。
    # 主人格如果确实想"立刻一次性"演示一次周期任务的单轮流程，应显式传
    # `confirm_one_shot=True` 跳过校验。
    _has_subtask_recurring = any((s.recurring_trigger or "").strip() for s in subtasks)
    if (
        not recurring_trigger
        and not _has_subtask_recurring
        and _RECURRING_HINTS_RE.search(goal)
        and not any(s.not_before for s in subtasks)
        and not confirm_one_shot
    ):
        _record_register_reject(owner_id, "recurring_miss")
        return (
            "⚠️ goal 含周期意图关键词（每天 / 每隔 / 每开盘 / 持续 N 天 / cron 等），"
            "但 `recurring_trigger=None`、也没有任何子任务带 `recurring_trigger`、"
            "且每个子任务都没设 `not_before`——这棵树会**立刻**派出所有子任务执行"
            "**一次**，与你描述的周期语义不符。\n\n"
            "请四选一：\n"
            "  (A) **一棵树包完整生命周期**（**新版推荐**）：在 `subtasks` 列表里给"
            "周期更新子任务加 `recurring_trigger` 字段（同根级格式）——这样一棵任务"
            "树既可以有一次性 init 子任务（depends_on=[]、立即跑）、周期触发子任务"
            "（depends_on=[init]、init 完成后 armed 等到点 fire）、又可以有一次性 final 子任务"
            "（加 `not_before` 到结算时刻派出）。**不要 depends_on 周期子任务**——"
            "周期子任务永远 armed 不 completed，下游死锁。\n"
            '  (B) **整棵树周期模板**（旧版兼容）：传根级 `recurring_trigger="cron:..."` '
            '让整棵树按 cron 克隆实例。适合"每次开火都跑同一套子任务"的纯周期场景；'
            "但状态/流水需要跨实例持久化时仍推荐用 (A) 把所有阶段写进一棵树。\n"
            '  (C) **绝对时间一次性延后**：给每个子任务加 `not_before="<ISO 时间>"`，'
            "框架挂 APScheduler 到点派出。适合「等到某个具体时刻再执行一次」。\n"
            "  (D) **现在就跑一次**（罕见）：显式传 `confirm_one_shot=True`，告诉框架"
            "你确认要立刻派出一次（如演示 / 单次手动触发）。但**不要**用 (D) 绕开"
            "周期任务——周期一律走 (A) 或 (B)，否则只跑一次后不会再触发。"
        )

    # 2) 校验每个子任务 agent_profile 都已注册
    from gsuid_core.ai_core.agent_node import get_node

    invalid: List[str] = []
    for i, s in enumerate(subtasks):
        if not s.agent_profile or get_node(s.agent_profile) is None:
            invalid.append(f"#{i}({s.agent_profile or '空'})")
    if invalid:
        _record_register_reject(owner_id, "bad_args")
        return f"⚠️ 以下子任务的 agent_profile 未注册：{invalid}"

    # 3) 整理 spec（含 subtask 级 recurring_trigger 字段）
    spec_dicts: List[Dict[str, Any]] = []
    not_before_errors: List[str] = []
    recurring_errors: List[str] = []
    # 用于"周期意图强校验"覆盖更广——既看根级 recurring_trigger，也看任意子任务级
    has_any_subtask_recurring = False
    for i, s in enumerate(subtasks):
        nb_iso: Optional[str] = None
        if s.not_before:
            from datetime import datetime as _dt

            try:
                _dt.fromisoformat(s.not_before)
                nb_iso = s.not_before
            except ValueError:
                not_before_errors.append(f"#{i + 1} not_before 不是合法 ISO 时间: {s.not_before!r}")

        sub_recurring = (s.recurring_trigger or "").strip() or None
        if sub_recurring:
            has_any_subtask_recurring = True
            from .recurring import parse_trigger_spec

            try:
                parse_trigger_spec(sub_recurring)
            except ValueError as e:
                recurring_errors.append(f"#{i + 1} recurring_trigger 非法: {e}")
        sub_recurring_until: Optional[str] = None
        if s.recurring_until:
            from datetime import datetime as _dt

            try:
                _dt.fromisoformat(s.recurring_until)
                sub_recurring_until = s.recurring_until
            except ValueError:
                recurring_errors.append(f"#{i + 1} recurring_until 必须是 ISO 时间字符串: {s.recurring_until!r}")

        spec_dicts.append(
            {
                "description": s.description,
                "agent_profile": s.agent_profile,
                "depends_on": s.depends_on,
                "params_hint": s.params_hint or {},
                "not_before": nb_iso,
                "recurring_trigger": sub_recurring,
                "recurring_until": sub_recurring_until,
            }
        )
    if not_before_errors:
        _record_register_reject(owner_id, "bad_args")
        return "⚠️ 子任务 not_before 参数错误：\n" + "\n".join(not_before_errors)
    if recurring_errors:
        _record_register_reject(owner_id, "bad_args")
        return "⚠️ 子任务 recurring 参数错误：\n" + "\n".join(recurring_errors)

    # 3.5) 编排校验：依赖周期子任务会死锁（周期模板永不 completed）
    recurring_indexes = {i for i, spec in enumerate(spec_dicts) if spec.get("recurring_trigger")}
    if recurring_indexes:
        bad_deps: List[str] = []
        for i, spec in enumerate(spec_dicts):
            for dep in spec.get("depends_on") or []:
                if isinstance(dep, int) and dep in recurring_indexes:
                    bad_deps.append(f"#{i + 1} 依赖周期子任务 #{dep + 1}")
        if bad_deps:
            _record_register_reject(owner_id, "bad_args")
            return (
                "⚠️ 子任务依赖周期子任务会死锁——周期子任务持续 armed 永不 completed，"
                "下游 depends_on 它就一直等不到。请用 `not_before` 给下游设定开始时间"
                "把周期与终结子任务在时间上错开：\n  " + "\n  ".join(bad_deps)
            )

    persona_name = None
    try:
        from gsuid_core.ai_core.persona import persona_config_manager

        persona_name = persona_config_manager.get_persona_for_session(ev.session_id)
    except ImportError:
        pass

    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

    scope_key = make_scope_key(
        ScopeType.GROUP if ev.group_id else ScopeType.USER_GLOBAL,
        str(ev.group_id or ev.user_id),
    )
    broadcast = [str(ev.group_id)] if (broadcast_to_group and ev.group_id) else []

    # 周期模式：先校验 trigger 格式合法，挂到 APScheduler 才接受创建
    parsed_until = None
    if recurring_trigger:
        from .recurring import parse_trigger_spec

        try:
            parse_trigger_spec(recurring_trigger)
        except ValueError as e:
            return f"⚠️ recurring_trigger 格式非法：{e}"
        if recurring_until:
            from datetime import datetime as _dt

            try:
                parsed_until = _dt.fromisoformat(recurring_until)
            except ValueError:
                return f"⚠️ recurring_until 必须是 ISO 时间字符串（如 2026-06-21T15:00:00），收到：{recurring_until!r}"

    root, children = await kanban.create_kanban_tree(
        goal=goal,
        owner_user_id=owner_id,
        scope_key=scope_key,
        bot_id=ev.bot_id,
        persona_name=persona_name,
        bot_self_id=ev.bot_self_id or "",
        group_id=ev.group_id,
        user_type=ev.user_type or "direct",
        WS_BOT_ID=ev.WS_BOT_ID,
        session_id=ev.session_id,
        user_pm=ev.user_pm,
        broadcast_targets=broadcast,
        display_name=goal[:64],
        subtasks=spec_dicts,
        recurring_trigger=recurring_trigger,
        recurring_until=parsed_until,
    )

    # 4) 周期模板 → 挂 APScheduler，不直接 kick；一次性任务 → 立刻 kick
    if recurring_trigger:
        from .recurring import schedule_template

        end_iso = parsed_until.isoformat() if parsed_until else None
        ok = schedule_template(root.id, recurring_trigger, end_date=end_iso)
        if not ok:
            return (
                f"⚠️ 周期模板已建（任务#{root.ordinal}），但 APScheduler 挂载失败，请改用 disarm/re-arm 重试或检查日志。"
            )
        return (
            f"✅ 已创建周期 Kanban 模板【任务#{root.ordinal}｜{root.display_name}】，"
            f"共 {len(children)} 个子任务，触发规则: {recurring_trigger}"
            + (f"（截止 {parsed_until.isoformat()}）" if parsed_until else "")
            + "。**注意**：模板本身不立即执行——按 cron / interval 到点才克隆实例。"
            "**你无需立刻 fail / respawn**，到点会自动开火；主人可在 webconsole "
            "看每次开火的实例树。如果想立刻验证一次，可在 webconsole 触发"
            " disarm + 单次手动 kick 或调 webconsole API。"
        )

    import asyncio

    from .recurring import schedule_not_before_wakeup
    from .kanban_executor import kick_root

    asyncio.create_task(kick_root(root.id))

    # 4.5) 子任务级 not_before 唤醒：把"未到点"的子任务挂上 APScheduler 单次
    # 定时器，到点 kick_root 一次。本根任务下多个子任务有 not_before 时每个都挂
    # 一个独立 job，按各自时间点轮流唤醒，互不影响。
    for child in children:
        if child.not_before is not None:
            schedule_not_before_wakeup(child.id, root.id, child.not_before)

    # 一次性任务：在返回里点明"接下来会发生什么"，避免主人格不知道任务在跑 → 立刻 fail 重建
    # （session 7a29c54d 的循环根因之一）。再附"是否漏传 recurring_trigger"的软警告。
    subtask_lines: List[str] = []
    for i, (s, child) in enumerate(zip(subtasks, children), start=1):
        dep_part = f"，依赖 {','.join(f'#{j + 1}' for j in s.depends_on)}" if s.depends_on else ""
        sub_recurring = (s.recurring_trigger or "").strip()
        if sub_recurring:
            # 周期子任务：到点 fire
            ready = f"周期模板（{sub_recurring}），依赖满足后 armed 等到点 fire"
        elif child.not_before is not None:
            ready = f"等待到 {child.not_before.isoformat(timespec='minutes')} 自动派出"
        elif s.depends_on:
            ready = "pending 等依赖完成"
        else:
            ready = "ready 立即派出"
        subtask_lines.append(f"  {i}) [{s.agent_profile}] {s.description[:40]}{dep_part}（{ready}）")
    text = (
        f"✅ 已创建 Kanban 任务树【任务#{root.ordinal}｜{root.display_name}】，"
        f"共 {len(children)} 个子任务：\n"
        + "\n".join(subtask_lines)
        + "\n所有无依赖一次性子任务已立刻派出运行，预计 5-60 秒内有进展。**不要立刻 fail "
        "重建**：要等结果就用 webconsole 看进度或主动让主人追问；要改 args 用 "
        "respawn_subtask；要彻底放弃才用 fail_task_tree。"
    )
    if has_any_subtask_recurring:
        text += (
            "\n\n📅 本树含**子任务级周期模板**——init 完成后自动 arm 挂 APScheduler，"
            "到点 fire 时框架自动克隆一个执行实例子任务跑一遍（结果写 record_*/artifact 持久化）。"
            "整棵树会保持 running 状态直到所有 armed 子任务过期 / 被 disarm。"
        )
    # 周期意图但 confirm_one_shot=True 显式跳过校验——成功后给个轻提示，避免
    # 主人格忘了这棵树不会自动重跑。
    if _RECURRING_HINTS_RE.search(goal) and confirm_one_shot:
        text = (
            "ℹ️ 你显式传了 `confirm_one_shot=True`，已按「立刻一次性」创建——这棵树"
            "只会跑一次，之后不会自动重启。如果实际想要周期托管，请在跑完后用 "
            "`register_kanban_task(recurring_trigger=...)` 另起一棵周期模板。\n\n" + text
        )
    return text


# ─────────────────────────────────────────────────────────────────────
# 重派 / 整树失败 / 审批
# ─────────────────────────────────────────────────────────────────────


async def _resolve_subtask(ev, subtask_ref: str) -> Optional[AIAgentTask]:
    """按自然语言句柄解析单个子任务节点（基于其根任务的引用 + 序号）。

    格式约定：`subtask_ref` 形如 `"<root_ref>#sub<N>"`（N 为子任务在树中的 1-based
    序号）；或直接 `"#sub<N>"` 时取主人格当前正在用的最近一棵任务树。

    **叶子根支持**：当目标任务树是叶子根（``create_subagent(agent_profile=...)``
    自动创建的"单步自执行根任务"，没有子任务）时，``#sub<N>`` 后缀会被忽略，
    直接返回根任务本身——主人格可以用 ``"<root_ref>"`` 或 ``"<root_ref>#sub1"``
    都能命中。
    """
    if not subtask_ref:
        return None
    ref = subtask_ref.strip()
    if "#sub" in ref:
        root_ref, sub_part = ref.split("#sub", 1)
    else:
        root_ref, sub_part = ref, ""
    candidates = await resolve_task_ref(root_ref, str(ev.user_id)) if root_ref else []
    if not candidates:
        roots = await AIAgentTask.list_for_owner(str(ev.user_id), only_active=True, root_only=True)
        candidates = roots[:1]
    if not candidates:
        return None
    root = candidates[0]
    _, children = await kanban.get_task_tree(root.id)
    # 叶子根：根任务自身就是执行节点，没有子任务时直接返回根任务
    if not children:
        if kanban.is_leaf_root(root, 0):
            return root
        return None
    try:
        idx = int(sub_part.strip()) if sub_part.strip() else 1
    except ValueError:
        idx = 1
    if 1 <= idx <= len(children):
        return children[idx - 1]
    return children[0]


@ai_tools(category="planning", capability_domain=_CAP)
async def respawn_subtask(
    ctx: RunContext[ToolContext],
    subtask_ref: str,
    new_description: Optional[str] = None,
    new_params: Optional[Dict[str, Any]] = None,
    new_agent_profile: Optional[str] = None,
) -> str:
    """复活某个 failed 子任务并重派执行。

    Args:
        subtask_ref: 子任务引用句柄；形如 "炒股周报#sub2" 或 "#sub2"（默认取最近根任务）。
        new_description: 修正后的任务描述（覆盖原 goal）。
        new_params: 修正后的参数（覆盖 params_override）。
        new_agent_profile: 改派给其它 profile（必须已注册）。

    超过 3 次重派会强制转 waiting_approval，请改用 webconsole 走主人审批，
    或让主人在对话中明示同意 / 拒绝后调用 respond_approval。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息。"
    task = await _resolve_subtask(ev, subtask_ref)
    if task is None:
        return f"⚠️ 找不到子任务: {subtask_ref}"

    if new_agent_profile:
        from gsuid_core.ai_core.agent_node import get_node

        if get_node(new_agent_profile) is None:
            return f"⚠️ 改派的 agent_profile 未注册: {new_agent_profile}"

    ok, msg = await kanban.respawn_child_task(
        task,
        new_description=new_description,
        new_params=new_params,
        new_agent_profile=new_agent_profile,
    )
    if ok and task.root_task_id:
        import asyncio

        from .kanban_executor import kick_root

        asyncio.create_task(kick_root(task.root_task_id))
    return ("✅ " if ok else "ℹ️ ") + msg


@ai_tools(category="planning", capability_domain=_CAP)
async def fail_task_tree(ctx: RunContext[ToolContext], task_ref_text: str, reason: str) -> str:
    """主人格明确判断整棵任务树不应继续时调用：根任务 failed + 级联 failed 未完成子任务。

    Args:
        task_ref_text: 任务自然语言引用（如 "炒股周报""第3个"）。
        reason: 终结原因，会写入 failure_reason 与日志。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息。"
    candidates = await resolve_task_ref(task_ref_text, str(ev.user_id))
    # 只匹配根任务，且**过滤掉已经终结**的——主人格如果反复 fail 同一棵已 failed 树
    # 一定是搞混了，此时让候选列表为空并提示"已经 failed / cancelled，不要重复 fail"
    _ACTIVE_STATUSES = ("pending", "running", "paused", "waiting_approval")
    candidates = [t for t in candidates if t.node_kind == "root" and t.status in _ACTIVE_STATUSES]
    if not candidates:
        return (
            f"⚠️ 没找到匹配「{task_ref_text}」的**活跃**根任务（已 failed / cancelled / "
            "completed 的不算）。如果你刚 fail 过同名任务又想再 fail 一次——这是循环"
            "信号，停下来检查 args 或问主人确认。"
        )
    if len(candidates) > 1:
        listing = "；".join(f"任务#{t.ordinal}｜{t.display_name}" for t in candidates[:5])
        return f"❓ 匹配到多个根任务，请说明：{listing}"
    root = candidates[0]
    ok = await kanban.fail_task_tree(root.id, reason)
    if not ok:
        return f"⚠️ 任务树终结失败: {task_ref_text}"
    return f"✅ 已终结整树【任务#{root.ordinal}｜{root.display_name}】：{reason}"


# 子任务审批的转达已统一到 buildin_tools/approval_tools.py::respond_approval
# （统一审批中心 kanban_subtask 领域），本模块不再注册专用转达工具。


# ─────────────────────────────────────────────────────────────────────
# Artifact Hub 工具
# ─────────────────────────────────────────────────────────────────────


@ai_tools(category="planning", capability_domain="产物")
async def artifact_put(
    ctx: RunContext[ToolContext],
    payload: str = "",
    summary: str = "",
    mime: str = "",
    artifact_kind: str = "output",
    file_path: str = "",
) -> str:
    """登记一个产出 artifact（仅在 Kanban / ad-hoc 任务执行上下文中有效）。

    自动绑定当前 root_task_id / task_id；返回 res 句柄供下游引用。

    **三种登记模式（必须三选一，不要混用）**：

    1. **登记真实文件**（PNG / PDF / CSV / 二进制等"代码实际跑出来的产物"）——
       传 ``file_path``（相对路径默认相对 workspace，也可以传 workspace 内绝对
       路径）。框架**直接登记 workspace 内的原文件路径**（不再复制副本，所有
       中间代码 + 最终产物都在同一个 workspace 文件夹里方便用户查看），并按后缀
       自动推断 mime，主人格之后可以直接 `send_message_by_ai(image_id="res_xxx")`
       把这张图 / 这份文件原样发给主人。文件必须在 workspace 内，越界路径会被拒绝。
       ✅ 正确：`artifact_put(file_path="love_heart.png", summary="渐变彩色爱心")`
       ❌ 错误：`artifact_put(payload='{"file": "x.png", "size": 17842}')` ← 这只是
          一段 JSON 元数据，**不是图片**；主人格 `send_message_by_ai` 时拿到的不是
          图片字节，会被识别成 inline 文本 artifact 而拒绝发送。
    2. **登记 inline 文本**（结论 / 报告正文，≤ 4KB）—— 传 ``payload``；默认 mime
       `text/plain`。主人格 `artifact_get(res_id)` 直读。
    3. **登记落盘大文本**（> 4KB 的 JSON / HTML / Markdown 报告）—— 传 ``payload``
       + 合适的 ``mime``（`application/json` / `text/html` / `text/markdown` 等），
       框架自动落盘到 workspace 内 `_artifact_<id>.<ext>` 文件。

    Args:
        payload: 文本内容（模式 2 / 3 使用）。不与 file_path 同时传。
        summary: 一句话摘要，回写到 artifact.summary 字段；文件模式留空时默认为
            ``"file: <basename>"``。
        mime: 内容类型；文件模式留空时按后缀自动推断（``.png`` → `image/png` 等）；
            文本模式留空时默认 `text/plain`。
        artifact_kind: ``output`` / ``log`` / ``report`` / ``patch`` 之一，默认 ``output``。
        file_path: 模式 1 使用——workspace 内的相对路径或绝对路径，指向实际落盘的
            产物文件。文件必须存在，否则返回失败。
    """
    plan_ctx = get_plan_context()
    if plan_ctx is None or not plan_ctx.root_task_id:
        return "ℹ️ 当前不在 Kanban 任务上下文中，artifact_put 不可用。"

    file_path_obj = None
    if file_path:
        from pathlib import Path

        file_path_obj = Path(file_path)
        # 错误用法预警：同时传 payload + file_path 时只走 file_path 分支
        if payload:
            logger.warning(
                "📋 [Kanban] artifact_put 同时收到 payload 和 file_path，"
                "按 file_path 模式登记真实文件，payload 会被丢弃。"
            )

    art = await put_artifact(
        payload=payload,
        summary=summary or (payload[:120] if payload else ""),
        mime=mime,
        artifact_kind=artifact_kind,
        plan_ctx=plan_ctx,
        file_path=file_path_obj,
    )
    if art is None:
        if file_path_obj is not None:
            return (
                f"⚠️ 登记 artifact 失败：找不到文件 `{file_path}`（相对路径默认相对"
                f" workspace）。请先确认 `list_directory` 能看到这个文件再登记。"
            )
        return "⚠️ 登记 artifact 失败（payload 与 file_path 均为空？）。"
    # 同步更新当前任务的 output_artifact_id（最近一次产出）
    await AIAgentTask.update_data_by_data(
        select_data={"id": plan_ctx.task_id},
        update_data={"output_artifact_id": art.id},
    )
    binary_hint = ""
    if art.payload_path and art.mime and art.mime.startswith("image/"):
        binary_hint = "（真实图片文件，主人格可 send_message_by_ai 直发）"
    elif art.payload_path:
        binary_hint = "（落盘文件）"
    return f"✅ 已登记 artifact: {art.id}（{art.size_bytes} bytes，mime={art.mime}）{binary_hint}"


@ai_tools(category="planning", capability_domain="产物")
async def artifact_get(
    ctx: RunContext[ToolContext],
    res_id: str,
) -> str:
    """按 res 句柄取回某 artifact 的内容（同 root_task_id 才允许跨任务读取）。"""
    plan_ctx = get_plan_context()
    art = await AIAgentArtifact.get_by_id(res_id)
    if art is None:
        return f"⚠️ artifact 不存在: {res_id}"
    if plan_ctx is not None and plan_ctx.root_task_id and art.root_task_id != plan_ctx.root_task_id:
        logger.warning(
            f"📋 [Kanban] 拒绝跨树读取 artifact: req_root={plan_ctx.root_task_id} art_root={art.root_task_id}"
        )
        return "⚠️ 该 artifact 属于其它任务树，跨树读取被拒绝。"
    return _format_artifact(art)


@ai_tools(category="planning", capability_domain="产物")
async def artifact_list(
    ctx: RunContext[ToolContext],
    task_ref_text: str = "",
) -> str:
    """列出某任务树下的全部 artifact（默认当前任务树或最近活跃任务树）。"""
    root_task_id = await _resolve_root_task_id(ctx, task_ref_text)
    if not root_task_id:
        return "ℹ️ 未定位到任务树。"
    arts = await AIAgentArtifact.list_for_root(root_task_id)
    if not arts:
        return f"ℹ️ 任务树 {root_task_id} 暂无 artifact。"
    lines = [f"📦 任务树 {root_task_id} 的 artifact ({len(arts)})："]
    for a in arts[:30]:
        lines.append(f"- {a.id} | kind={a.artifact_kind} | from={a.from_profile or '-'} | {a.summary[:80]}")
    if len(arts) > 30:
        lines.append(f"…还有 {len(arts) - 30} 条未列出。")
    return "\n".join(lines)


@ai_tools(category="planning", capability_domain="产物")
async def artifact_get_recent(
    ctx: RunContext[ToolContext],
    task_ref_text: str = "",
) -> str:
    """取某根任务**最近一份** artifact 的完整原文——追问溯源专用入口。

    主人追问"为什么这样选 / 基于什么数据决定"时调用本工具，把专职代理留下的
    完整原文拿回来再用角色口吻转告主人；**严禁**自己重新 web_search /
    search_knowledge 拼凑一个新理由——那不是当时做决定的依据，会与原文矛盾。

    `task_ref_text` 留空时取主人最近活跃的根任务；传自然语言引用时按引用解析。

    Args:
        task_ref_text: 用于定位根任务的自然语言引用（可空）。

    Returns:
        最近一份 artifact 的完整原文；任务无 artifact 时返回友好提示。
    """
    root_task_id = await _resolve_root_task_id(ctx, task_ref_text)
    if not root_task_id:
        return "ℹ️ 未定位到任务树。"
    recent = await AIAgentArtifact.list_recent_for_root(root_task_id, limit=1)
    if not recent:
        return (
            f"ℹ️ 任务树 {root_task_id} 暂无 artifact——"
            "可能任务还没跑完第一步，或代理没显式 artifact_put。"
            "请如实告诉主人当时没留下完整原文，不要替它编造理由。"
        )
    art = recent[0]
    return (
        "📄 最近一份 artifact（请直接基于此原文用你自己的角色口吻转告主人，"
        "不要二次分析 / 编造）：\n" + _format_artifact(art)
    )


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────


async def _resolve_root_task_id(ctx: RunContext[ToolContext], task_ref_text: str) -> Optional[str]:
    """按上下文 / 自然语言引用 / 最近活跃根任务，依次解析 root_task_id。"""
    ev = ctx.deps.ev
    plan_ctx = get_plan_context()
    if plan_ctx and plan_ctx.root_task_id:
        return plan_ctx.root_task_id
    if ev is None:
        return None
    if task_ref_text:
        candidates = await resolve_task_ref(task_ref_text, str(ev.user_id))
        candidates = [t for t in candidates if t.node_kind == "root"]
        if candidates:
            return candidates[0].id
    actives = await AIAgentTask.list_for_owner(str(ev.user_id), only_active=True, root_only=True)
    return actives[0].id if actives else None


def _format_artifact(art: AIAgentArtifact) -> str:
    head = f"artifact {art.id} | kind={art.artifact_kind} | mime={art.mime}\nsummary: {art.summary}\n"
    if art.payload_inline:
        return head + f"payload:\n{art.payload_inline}"
    if art.payload_path:
        try:
            from pathlib import Path

            text = Path(art.payload_path).read_text(encoding="utf-8", errors="replace")
            return head + f"payload:\n{text[:12000]}"
        except OSError as e:
            return head + f"⚠️ 读取 artifact 落盘失败: {e}"
    return head + "（无 inline / 落盘内容）"


__all__ = [
    "KanbanSubtaskSpec",
    "evaluate_agent_mesh_capability",
    "register_kanban_task",
    "respawn_subtask",
    "fail_task_tree",
    "artifact_put",
    "artifact_get",
    "artifact_list",
    "artifact_get_recent",
    "list_my_kanban_tasks",
    "pause_my_kanban_tree",
    "resume_my_kanban_tree",
]


# ─────────────────────────────────────────────────────────────────────
# Owner 视角的 Kanban Introspect（list / pause / resume）
# ─────────────────────────────────────────────────────────────────────


@ai_tools(category="common", capability_domain="长期任务编排")
async def list_my_kanban_tasks(
    ctx: RunContext[ToolContext],
    goal_filter: str = "",
    status: str = "active",
) -> str:
    """列出当前 owner 名下的 Kanban 任务树（root 节点）。

    用于：
    - 主人格 / 能力代理 introspect 自己的长期任务；
    - 命令"我有哪些任务在跑""暂停/恢复 AI 模拟盘"前的查表。

    Args:
        goal_filter: 按 goal 模糊过滤（子串匹配，留空返回全部）
        status: "active"（默认；含 pending/running/paused/waiting_approval）
                / "all"（含已结束）/ "failed" / "completed"

    Returns:
        Markdown 表格，含 ordinal / goal / status / 周期 / 进度
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息。"
    owner = str(ev.user_id)
    import re as _re

    from .models import AIAgentTask

    only_active = status == "active"
    roots = await AIAgentTask.list_for_owner(owner, only_active=only_active, root_only=True)
    if not roots:
        return "ℹ️ 当前 owner 名下没有 Kanban 任务树。"

    if goal_filter:
        pat = _re.compile(_re.escape(goal_filter), _re.IGNORECASE)
        roots = [r for r in roots if pat.search(r.goal or "")]

    if status in ("failed", "completed"):
        roots = [r for r in roots if r.status == status]

    if not roots:
        return f"ℹ️ 过滤后无任务（goal_filter={goal_filter!r}, status={status}）。"

    lines = [f"📋 Kanban 任务树（owner={owner}，{len(roots)} 棵）：", ""]
    lines.append("| # | goal | 状态 | 周期 | 错误 |")
    lines.append("|---|------|------|------|------|")
    for r in roots[:30]:
        trig = (r.recurring_trigger or "-")[:24]
        err = (r.failure_reason or "")[:40]
        lines.append(f"| #{r.ordinal} | {(r.goal or '')[:50]} | {r.status} | {trig} | {err} |")
    if len(roots) > 30:
        lines.append(f"…还有 {len(roots) - 30} 棵未列出。")
    return "\n".join(lines)


@ai_tools(category="common", capability_domain="长期任务编排")
async def pause_my_kanban_tree(
    ctx: RunContext[ToolContext],
    task_ref: str,
    reason: str = "",
) -> str:
    """暂停当前 owner 的某棵 Kanban 周期树（disarm，不删除树）。

    对一次性任务等价于 fail_task_tree；对周期模板会 disarm_template 让其
    不再 fire，但任务树本身保留。

    Args:
        task_ref: 任务引用（同 respawn_subtask 的 subtask_ref；留空取最近一棵）
        reason: 暂停原因
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息。"
    target = await _resolve_subtask(ev, task_ref)
    if target is None:
        return f"⚠️ 找不到任务: {task_ref!r}"
    root = await _resolve_root_from_subtask(target, ev)
    if root is None:
        return f"⚠️ 无法解析根任务: {task_ref!r}"

    from . import kanban

    # 周期模板 → disarm；一次性 → mark paused
    if root.recurring_trigger:
        ok = await kanban.disarm_template(root.id)
        if ok:
            return f"⏸️ 已暂停周期 Kanban 树【任务#{root.ordinal}｜{root.display_name}】：{reason or '无原因'}"
        return f"⚠️ disarm 失败: 任务#{root.ordinal}"

    # 一次性任务 → 改状态
    from .models import AIAgentTask

    await AIAgentTask.update_data_by_data(
        select_data={"id": root.id},
        update_data={"status": "paused", "failure_reason": reason or "paused by owner"},
    )
    return f"⏸️ 已暂停一次性任务【任务#{root.ordinal}｜{root.display_name}】"


@ai_tools(category="common", capability_domain="长期任务编排")
async def resume_my_kanban_tree(
    ctx: RunContext[ToolContext],
    task_ref: str,
) -> str:
    """恢复被暂停的 Kanban 任务树。

    周期模板：arm_recurring_subtask 重挂 APScheduler
    一次性任务：状态置回 pending（需要 respawn_subtask 重新派发）
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息。"
    target = await _resolve_subtask(ev, task_ref)
    if target is None:
        return f"⚠️ 找不到任务: {task_ref!r}"
    root = await _resolve_root_from_subtask(target, ev)
    if root is None:
        return f"⚠️ 无法解析根任务: {task_ref!r}"

    from . import kanban

    if root.recurring_trigger:
        ok, msg = await kanban.arm_recurring_subtask(root, root.recurring_trigger)
        if ok:
            return f"▶️ 已恢复周期 Kanban 树【任务#{root.ordinal}｜{root.display_name}】"
        return f"⚠️ arm 失败: {msg}"

    from .models import AIAgentTask
    from .kanban_executor import kick_root

    await AIAgentTask.update_data_by_data(
        select_data={"id": root.id},
        update_data={"status": "pending", "failure_reason": ""},
    )
    import asyncio

    asyncio.create_task(kick_root(root.id))
    return f"▶️ 已重新派发一次性任务【任务#{root.ordinal}｜{root.display_name}】"


async def _resolve_root_from_subtask(task: AIAgentTask, ev) -> Optional[AIAgentTask]:
    """从子任务回溯根任务。"""
    if task.node_kind == "root":
        return task
    root_id = task.root_task_id
    if not root_id:
        # fallback：取 owner 最近一棵 root
        roots = await AIAgentTask.list_for_owner(str(ev.user_id), root_only=True)
        return roots[0] if roots else None
    return await AIAgentTask.get_by_id(root_id)
