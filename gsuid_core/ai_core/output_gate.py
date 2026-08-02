"""发送前统一输出闸门（pre_send_gate）。

把「能不能给用户看 / 要不要打回模型」集中在一处编排；各策略仍独立可测。

策略顺序（短路）：
1. ``angle_bracket`` — 非法 ``<>`` 标签（含 ``<br>`` / ``<bubble/>``）
2. ``ooc`` — 出戏防火墙（模型名 / AI 自指 / 系统术语 / 资金 / 机器腔）

``send_chat_result`` 仍负责呈现层（拆条 / meme / 长文出图 / sanitize 兜底），
不在此重复做通道变换。

状态：仅 ``ToolContext.extra["output_gate"]`` → ``GateBag``（会话重启即丢，无旧键）。

用法::

    from gsuid_core.ai_core.output_gate import pre_send_gate, GateDecision

    r = pre_send_gate(text, extra, user_text=..., channel="main")
    if r.decision is GateDecision.FUSE:
        ...
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Sequence
from dataclasses import field, dataclass

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger

# ── 结果类型 ────────────────────────────────────────────────────────


class GateDecision(str, Enum):
    ALLOW = "allow"
    """放行原文。"""
    REWRITE = "rewrite"
    """不发送原文；``feedback`` 打回模型。"""
    FALLBACK = "fallback"
    """立即发送 ``send_text``（安全兜底句）。"""
    FUSE = "fuse"
    """本轮熔断静默；``feedback`` 可提示模型只输出 SILENCE。"""


@dataclass
class GateResult:
    decision: GateDecision
    policy: str = ""
    feedback: str = ""
    send_text: str = ""
    # OOC 主路径：收集后在 run 末尾轻量重说
    defer_ooc: bool = False
    ooc_hit: Any = None
    fused: bool = False
    detail: str = ""


# ── 类型化状态（挂在 extra["output_gate"]） ─────────────────────────


@dataclass
class PolicyState:
    attempts: int = 0
    blocked: list[str] = field(default_factory=list)
    abort: bool = False
    fuse_injected: bool = False
    # 同一 ModelResponse 内多 TextPart 只计 1 次 attempt
    batch_counted: bool = False


@dataclass
class GateBag:
    angle_bracket: PolicyState = field(default_factory=PolicyState)
    ooc: PolicyState = field(default_factory=PolicyState)
    ooc_warned_turn_ids: set[str] = field(default_factory=set)


_STATE_KEY = "output_gate"


def ensure_gate_bag(extra: Dict[str, Any]) -> GateBag:
    """取得或创建 ``GateBag``；非 bag 实例时覆盖写入。"""
    raw = extra[_STATE_KEY] if _STATE_KEY in extra else None
    if isinstance(raw, GateBag):
        return raw
    bag = GateBag()
    extra[_STATE_KEY] = bag
    return bag


def begin_response_batch(extra: Dict[str, Any]) -> None:
    """每个 ModelResponse 处理 TextPart 前调用：重置「本批已计数」。"""
    bag = ensure_gate_bag(extra)
    bag.angle_bracket.batch_counted = False
    bag.ooc.batch_counted = False


def _policy(bag: GateBag, name: str) -> PolicyState:
    if name == "angle_bracket":
        return bag.angle_bracket
    if name == "ooc":
        return bag.ooc
    # 未知名勿返回 throwaway：否则 attempts/blocked 静默丢状态
    raise ValueError(f"unknown output_gate policy: {name!r}")


def is_fused(extra: Dict[str, Any]) -> bool:
    """任一策略熔断。"""
    if _STATE_KEY not in extra:
        return False
    raw = extra[_STATE_KEY]
    if not isinstance(raw, GateBag):
        return False
    return raw.angle_bracket.abort or raw.ooc.abort


def mark_fuse_injected(extra: Dict[str, Any]) -> None:
    ensure_gate_bag(extra).angle_bracket.fuse_injected = True


def fuse_already_injected(extra: Dict[str, Any]) -> bool:
    if _STATE_KEY not in extra:
        return False
    raw = extra[_STATE_KEY]
    if not isinstance(raw, GateBag):
        return False
    return raw.angle_bracket.fuse_injected


def blocked_texts(extra: Dict[str, Any], policy: str = "angle_bracket") -> List[str]:
    """被某策略拦下的原文列表（历史裁剪用）。"""
    if _STATE_KEY not in extra:
        return []
    raw = extra[_STATE_KEY]
    if not isinstance(raw, GateBag):
        return []
    ps = _policy(raw, policy)
    return [x.strip() for x in ps.blocked if x.strip()]


def attempt_count(extra: Dict[str, Any], policy: str = "angle_bracket") -> int:
    if _STATE_KEY not in extra:
        return 0
    raw = extra[_STATE_KEY]
    if not isinstance(raw, GateBag):
        return 0
    return _policy(raw, policy).attempts


def _record_block(
    extra: Dict[str, Any],
    policy: str,
    text: str,
    *,
    count_attempt: bool,
) -> int:
    """累计 blocked；可选 +1 attempts。返回当前 attempts。"""
    bag = ensure_gate_bag(extra)
    ps = _policy(bag, policy)
    stripped = text.strip()
    if stripped:
        ps.blocked.append(stripped)
    if count_attempt and not ps.batch_counted:
        ps.attempts += 1
        ps.batch_counted = True
    return ps.attempts


def _set_abort(extra: Dict[str, Any], policy: str) -> None:
    bag = ensure_gate_bag(extra)
    _policy(bag, policy).abort = True


def set_fused(extra: Dict[str, Any], policy: str = "angle_bracket") -> None:
    """收尾路径显式熔断（与环内 FUSE 对齐）。"""
    _set_abort(extra, policy)


# ── 策略：angle_bracket ─────────────────────────────────────────────


def _eval_angle_bracket(
    text: str,
    extra: Dict[str, Any],
    *,
    channel: str,
    count_attempt: bool,
) -> Optional[GateResult]:
    from gsuid_core.ai_core import angle_bracket_guard as ab

    if is_fused(extra):
        return GateResult(
            decision=GateDecision.FUSE,
            policy="angle_bracket",
            feedback=ab.build_fuse_warning(),
            fused=True,
            detail="already_fused",
        )

    tags = ab.find_illegal_angle_tags(text)
    if not tags:
        return None

    attempts = _record_block(extra, "angle_bracket", text, count_attempt=count_attempt)
    if attempts >= ab.MAX_RETRIES:
        _set_abort(extra, "angle_bracket")
        logger.warning(
            i18n_t(
                "log.ai.output_gate_angle_bracket_fuse",
                attempts=attempts,
                channel=channel,
                tags=repr(tags[:4]),
                preview=repr(text[:80]),
            )
        )
        return GateResult(
            decision=GateDecision.FUSE,
            policy="angle_bracket",
            feedback=ab.build_fuse_warning(),
            fused=True,
            detail=",".join(tags[:6]),
        )

    logger.warning(
        i18n_t(
            "log.ai.output_gate_angle_bracket_rewrite",
            attempts=attempts,
            max_retries=ab.MAX_RETRIES,
            channel=channel,
            tags=repr(tags[:4]),
            preview=repr(text[:80]),
        )
    )
    return GateResult(
        decision=GateDecision.REWRITE,
        policy="angle_bracket",
        feedback=ab.build_rewrite_warning(tags, text),
        detail=",".join(tags[:6]),
    )


# ── 策略：ooc ───────────────────────────────────────────────────────


def _eval_ooc(
    text: str,
    extra: Dict[str, Any],
    *,
    user_text: str,
    channel: str,
) -> Optional[GateResult]:
    from gsuid_core.ai_core import output_firewall as of

    if not of.is_enabled():
        return None

    hit = of.check_ooc(text, user_text=user_text)
    if hit is None:
        return None

    if hit.category == "machine_dump":
        if channel == "main":
            logger.warning(
                i18n_t(
                    "log.ai.output_gate_ooc_fallback_machine_dump",
                    preview=repr(text[:80]),
                )
            )
            return GateResult(
                decision=GateDecision.FALLBACK,
                policy="ooc",
                send_text=of.MACHINE_FALLBACK_TEXT,
                ooc_hit=hit,
                detail=hit.category,
            )
        logger.warning(i18n_t("log.ai.output_gate_ooc_rewrite_machine_dump_tool"))
        return GateResult(
            decision=GateDecision.REWRITE,
            policy="ooc",
            feedback=of.build_rewrite_warning(hit),
            ooc_hit=hit,
            detail=hit.category,
        )

    if channel == "main":
        logger.warning(
            i18n_t(
                "log.ai.output_gate_ooc_defer",
                category=hit.category,
                matched=repr(hit.matched[:4]),
            )
        )
        return GateResult(
            decision=GateDecision.REWRITE,
            policy="ooc",
            feedback=of.build_rewrite_warning(hit),
            defer_ooc=True,
            ooc_hit=hit,
            detail=hit.category,
        )

    # tool：提醒一次 → 再命中非 never-release 放行
    bag = ensure_gate_bag(extra)
    turn_id = ""
    if "turn_id" in extra and extra["turn_id"] is not None:
        turn_id = str(extra["turn_id"])
    if turn_id and turn_id in bag.ooc_warned_turn_ids:
        if hit.category in of.NEVER_RELEASE_CATEGORIES:
            logger.warning(
                i18n_t(
                    "log.ai.output_gate_ooc_rewrite_never_release",
                    category=hit.category,
                )
            )
            return GateResult(
                decision=GateDecision.REWRITE,
                policy="ooc",
                feedback=of.build_rewrite_warning(hit),
                ooc_hit=hit,
                detail=hit.category,
            )
        logger.warning(
            i18n_t(
                "log.ai.output_gate_ooc_allow_after_warn",
                category=hit.category,
            )
        )
        return GateResult(decision=GateDecision.ALLOW, policy="ooc", detail="second_pass")

    if turn_id:
        bag.ooc_warned_turn_ids.add(turn_id)
    logger.warning(
        i18n_t(
            "log.ai.output_gate_ooc_rewrite_first_warn",
            category=hit.category,
            matched=repr(hit.matched[:4]),
        )
    )
    return GateResult(
        decision=GateDecision.REWRITE,
        policy="ooc",
        feedback=of.build_rewrite_warning(hit),
        ooc_hit=hit,
        detail=hit.category,
    )


# ── 收尾规划 ─────────────────────────────────────────────────────────


@dataclass
class AfterRunAnglePlan:
    """run 结束后尖括号策略怎么处理（由 gs_agent 执行发送/改 history）。"""

    fused: bool
    blocked: list[str]
    attempts: int
    # 脏 TextPart → 干净文案的 1:1/安全映射（禁止多脏→同一条干净）
    replace_map: dict[str, str] = field(default_factory=dict)
    # 否则需要补轻量重写（仅最后一条 blocked）
    rewrite_original: str = ""
    # 需要 scrub nudge；若 fused 或 rewrite 失败则 drop blocked
    scrub_nudges: bool = True
    drop_blocked: bool = False
    # 尖括号熔断仍恢复独立 OOC 段（默认 False=不跳过）
    skip_ooc_rewrite: bool = False


def _build_angle_replace_map(blocked: Sequence[str], cleaned: Sequence[str]) -> dict[str, str]:
    """把 blocked 脏文映射到干净文案，避免「多脏 → 最后一条干净」污染 history。

    - 单脏：用最后一条干净文替换
    - 等长：按顺序 zip
    - 否则：只替换最后一条脏文，其余留给 scrub/保留
    """
    if not blocked or not cleaned:
        return {}
    clean_last = cleaned[-1]
    if len(blocked) == 1:
        return {blocked[0]: clean_last}
    if len(blocked) == len(cleaned):
        return {b: c for b, c in zip(blocked, cleaned) if b and c}
    return {blocked[-1]: clean_last}


def plan_angle_after_run(
    extra: Dict[str, Any],
    *,
    clean_sent: Sequence[str],
) -> AfterRunAnglePlan:
    """根据 GateBag 与本轮已发干净文本，规划尖括号收尾。

    不变量：尖括号熔断只 scrub 脏尖括号文，**不**取消本轮已 defer 的 OOC 重说。
    """
    from gsuid_core.ai_core import angle_bracket_guard as ab

    fused = is_fused(extra)
    blocked = blocked_texts(extra, "angle_bracket")
    attempts = attempt_count(extra, "angle_bracket")
    if fused:
        return AfterRunAnglePlan(
            fused=True,
            blocked=blocked,
            attempts=attempts,
            scrub_nudges=True,
            drop_blocked=True,
            skip_ooc_rewrite=False,
        )
    if not blocked:
        return AfterRunAnglePlan(
            fused=False,
            blocked=[],
            attempts=attempts,
            scrub_nudges=bool(attempts),
            drop_blocked=False,
            skip_ooc_rewrite=False,
        )
    cleaned = [t for t in clean_sent if t and not ab.has_illegal_angle_tags(t)]
    if cleaned:
        return AfterRunAnglePlan(
            fused=False,
            blocked=blocked,
            attempts=attempts,
            replace_map=_build_angle_replace_map(blocked, cleaned),
            scrub_nudges=True,
            drop_blocked=False,
            skip_ooc_rewrite=False,
        )
    return AfterRunAnglePlan(
        fused=False,
        blocked=blocked,
        attempts=attempts,
        rewrite_original=blocked[-1],
        scrub_nudges=True,
        drop_blocked=False,
        skip_ooc_rewrite=False,
    )


def merge_rewrite_feedbacks(feedbacks: Sequence[str]) -> str:
    """同一 ModelResponse 多段 REWRITE 合成一条注入文案。"""
    parts = [f.strip() for f in feedbacks if f and f.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "\n\n---\n\n".join(parts)


# ── 公开入口 ────────────────────────────────────────────────────────


def pre_send_gate(
    text: str,
    extra: Optional[Dict[str, Any]] = None,
    *,
    user_text: str = "",
    channel: str = "main",
    count_attempt: bool = True,
) -> GateResult:
    """对即将发出的 agent 台词做发送前闸门。

    Args:
        text: 模型要对用户说的正文
        extra: ``ToolContext.extra``；None 时无状态
        user_text: 本轮用户原文（OOC 身份追问门）
        channel: ``"main"`` | ``"tool"``
        count_attempt: 尖括号策略是否计入 attempts（同 response 后续段传 False）
    """
    if not text or not str(text).strip():
        return GateResult(decision=GateDecision.ALLOW)

    if channel not in ("main", "tool"):
        logger.warning(i18n_t("log.ai.output_gate_unknown_channel", channel=repr(channel)))
        channel = "main"

    bag_extra: Dict[str, Any] = extra if extra is not None else {}

    r = _eval_angle_bracket(text, bag_extra, channel=channel, count_attempt=count_attempt)
    if r is not None and r.decision is not GateDecision.ALLOW:
        return r

    r = _eval_ooc(text, bag_extra, user_text=user_text, channel=channel)
    if r is not None and r.decision is not GateDecision.ALLOW:
        return r

    return GateResult(decision=GateDecision.ALLOW)


def tool_gate_feedback(
    text: str,
    extra: Dict[str, Any],
    *,
    user_text: str = "",
) -> Optional[str]:
    """工具路径：需打回时返回 feedback，放行返回 None。"""
    r = pre_send_gate(text, extra, user_text=user_text, channel="tool")
    if r.decision is GateDecision.ALLOW:
        return None
    if r.decision is GateDecision.FALLBACK:
        return r.feedback or r.send_text
    return r.feedback or None


# 历史裁剪 / _relean_user_turn 识别系统打回前缀
GATE_NUDGE_MARKERS: tuple[str, ...] = (
    "（系统校验：发送内容含非法尖括号标签",
    "⛔ 你要发送的内容命中",
)
