"""主人格出站话术策略：单表面 + 策略解析 + render 候选门。

纯无状态文本检测器已收敛至 ``control.outbound_detectors``，本模块保留对话状态相关
的 ``SpeechPolicy`` / ``SpeechContext``、候选判断与策略环转发。
"""

from __future__ import annotations

from typing import Literal, Sequence
from dataclasses import dataclass

from gsuid_core.ai_core.content_guard import is_observation_untrusted
from gsuid_core.ai_core.control.lines import RES_HANDLE_RE
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    is_timeless_aggregate as _is_timeless_aggregate,
    fact_pack_is_multi_point as _fact_lines_multi_point,
    tool_return_is_delegation_refusal as _tool_return_is_delegation_refusal,
)

# 整轮因此零输出时要回灌改口，不走出图。在途静默不在此列。
ZERO_OUTPUT_VOICE_REASONS = frozenset(
    {
        "premature_delivery",
        "process_meta",
        "orchestration_leak",
        "delivery_narration",
    }
)

SpeechPolicy = Literal[
    "free",
    "silence_only",
    "status_ok",
    "framework_nudge",
    "framework_deliver",
]

# 单轮主通道可见台词上限
MAIN_CHANNEL_VISIBLE_LIMIT = 2

_RENDER_DELEGATE_NUDGE = (
    "（系统校验·内部轮：不对用户闲聊）"
    "本轮已有检索/工具材料或长结构内容且未出图 → **必须** "
    'create_subagent(agent_profile="render_agent", task=把本轮要点做成信息图)；'
    "禁止再输出长正文/标题列表；禁止引导性追问。"
    "仅当本轮工具全部失败且无任何可展示要点 → 只输出 <SILENCE>。"
    "禁止向用户解释本校验、禁止抱怨、禁止念节点名。"
)

# 主人格把长结构念成台词时的纠正（比通用 render nudge 更硬）
_REPORT_SPEECH_NUDGE = (
    "（系统校验·内部轮）你刚才用多段标题/列表把长信息念成了台词，这不允许。"
    '立即 create_subagent(agent_profile="render_agent", '
    "task=将本轮已查到的要点做成一张信息图)；"
    "禁止再把这张表念成台词。出图之后怎么收尾由你定。"
)

_STATUS_ZERO_TOOL_NUDGE = (
    "（系统校验：用户在追问进行中事项的进度，但你本轮未调用任何查询工具就报了状态。"
    "立即 list_my_kanban_tasks 或 artifact_get_recent 核实；"
    "再以角色短句说明，禁止空口「快好了」。）"
)


def resolve_speech_policy(
    *,
    is_framework: bool,
    fake_done_retry: bool,
    is_status_inquiry: bool,
    has_active_task: bool,
    user_text: str,
) -> SpeechPolicy:
    """根据本轮入口解析话术策略。"""
    if is_framework:
        # 任务完成回灌 vs 纠正 nudge：完成回灌含「任务完成」/产物句柄卡
        low = (user_text or "").lstrip()
        if low.startswith("（系统校验"):
            return "framework_nudge"
        if "任务完成" in low or "子任务交付" in low or "产物句柄" in low:
            return "framework_deliver"
        if low.startswith("[框架·"):
            return "framework_deliver"
        return "framework_nudge"
    if fake_done_retry:
        return "framework_nudge"
    if is_status_inquiry and has_active_task:
        return "status_ok"
    return "free"


def non_master_title(user_id: str, persona_name: str | None) -> str:
    """接收人不是主人时返回该人格的主人称呼，否则空串。"""
    uid = (user_id or "").strip()
    if not uid:
        return ""
    from gsuid_core.ai_core.utils import _is_master_user

    if _is_master_user(uid):
        return ""
    from gsuid_core.ai_core.persona.settings import get_master_title

    return get_master_title(persona_name).strip()


@dataclass(frozen=True, slots=True)
class SpeechContext:
    """话术策略判定上下文：供轮中门闩、工具发信与 settle 出口共用。"""

    policy: SpeechPolicy
    pending_async: bool
    image_sent: bool
    has_status_tool: bool
    tool_calls_so_far: Sequence[str]
    wait_comfort_sent: bool = False
    fact_pack_pending: bool = False
    has_active_task: bool = False
    render_inflight: bool = False
    speech_len_hard: int = 0
    user_asked_detail: bool = False
    forbid_title: str = ""
    entity_routed: bool = False
    user_text: str = ""
    allow_render: bool = True
    in_settle_exit: bool = False
    render_obligation: bool = False
    skip_report_exit: bool = False
    is_delivery_wake: bool = False

    @property
    def inflight(self) -> bool:
        return bool(self.pending_async or self.render_inflight)

    @property
    def renderable_pack(self) -> bool:
        return bool(self.fact_pack_pending and self.allow_render)


def normalize_speech_policy(policy: str) -> SpeechPolicy:
    if policy == "silence_only":
        return "silence_only"
    if policy == "status_ok":
        return "status_ok"
    if policy == "framework_nudge":
        return "framework_nudge"
    if policy == "framework_deliver":
        return "framework_deliver"
    return "free"


def evaluate_speech_policy(ctx: SpeechContext, text: str) -> tuple[bool, str]:
    """委托统一策略环判定本段可见文本是否拦截。返回 (block, reason)。"""
    from gsuid_core.ai_core.control.policy_ring import evaluate_speech_ring

    return evaluate_speech_ring(ctx, text)


def content_is_render_candidate(
    *,
    tool_name: str,
    content: str,
    fileos_folded: bool,
) -> bool:
    """是否可作为「待出图事实包」候选（体积折叠 ≠ 可出图）。"""
    body = (content or "").strip()
    if not body:
        return False
    if is_observation_untrusted(body):
        return False
    if body.startswith("⚠️") or body.startswith("❌"):
        return False
    if "没有找到与" in body[:80] and "工具" in body[:80]:
        return False
    if "抓取失败" in body[:40] or "执行失败" in body[:40]:
        return False
    # 异步 ack 不是终态材料
    if "后台执行" in body or "自动回灌" in body or "仍在执行" in body:
        return False

    tn = (tool_name or "").lower()
    if tn == "create_subagent":
        # 「委派原问」导语会挡住 ⚠️ 前缀：软拒绝不是事实包
        if _tool_return_is_delegation_refusal(body):
            return False
        if "事实包" in body or "artifact" in body.lower() or RES_HANDLE_RE.search(body):
            return len(body) >= 80
        return len(body) >= 200 and ("|" in body or body.count("\n") >= 6)

    if RES_HANDLE_RE.search(body) and len(body) >= 40:
        return True

    # 无时点聚合（气候/月均/历史均值）：不武装出图，防把常年均值渲成实况答案。
    if _is_timeless_aggregate(body):
        return False

    # 检索/抓取类：单点问答可短回，只有多点结构才值得出图
    _searchish = any(h in tn for h in ("search", "web_", "fetch", "knowledge"))
    if _searchish and not _fact_lines_multi_point(body):
        return False

    # 真表。长散文不得单独武装（观测说明文误报）。
    if "|" in body and body.count("\n") >= 3:
        return True

    # FileOS 折叠：用原文形态判定。多点事实行已在上面过门，折叠后仍要武装出图。
    if fileos_folded:
        if _fact_lines_multi_point(body) and len(body) >= 80:
            return True
        if "事实包" in body and len(body) >= 80:
            return True
        if "|" in body and body.count("\n") >= 3 and len(body) >= 200:
            return True
        if body.count("\n\n") >= 3 and len(body) >= 600:
            return True
        return False

    if _searchish:
        if "|" in body and body.count("\n") >= 4 and len(body) >= 600:
            return True
        if body.count("\n\n") >= 3 and len(body) >= 1200:
            return True
        # 多点逐行观测：形态达标即武装，不卡绝对长度
        if len(body) >= 80:
            return True
        return False
    return False


def is_status_tool_name(name: str) -> bool:
    n = (name or "").lower()
    return (
        n
        in {
            "list_my_kanban_tasks",
            "artifact_get_recent",
            "artifact_list",
            "artifact_get",
            "list_persisted_outputs",
            "grep_persisted_outputs",
            "search_cognition",
            "read_handle",
            "list_my_tasks",
        }
        or "kanban" in n
    )
