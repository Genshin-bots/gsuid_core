"""条件隐藏谓词（Phase 3 ``visible_when``）的共享判定。

保底池里 ``read_image`` / ``read_video`` / ``web_fetch_tool`` 属"窄场景常驻"——绝大多数轮
用不到，却每轮都向模型下发 schema。这里用**廉价的内存扫描**（``ev`` 文本 + 本轮 run 已
发生的消息）判断"上下文里有没有图片 / 视频 / URL"，无关时对模型隐藏。
``read_video`` 还要求本轮 ``model_support`` 声明了 video，否则不下发。

判定一律**偏可见**（拿不准就显示）：``visible_when`` 误隐藏会让模型够不到真正需要的工具，
代价远大于多显示一个，故只在"确实没有任何线索"时才隐藏。

另含 ``visible_to_admin``：管理员专属工具（execute_shell_command / install_skill 等）
共用的"仅主人可见"谓词，与各自的 ``check_func=check_pm`` 执行期拦截互补。
"""

from __future__ import annotations

import re
from typing import Iterator, Protocol, Sequence

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext


class VisibilityScanCtx(Protocol):
    """visible_when 扫描面：只要 deps 与本轮 messages。"""

    deps: ToolContext | None

    @property
    def messages(self) -> Sequence[object]: ...


_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _iter_context_texts(ctx: VisibilityScanCtx) -> Iterator[str]:
    """产出本轮可供扫描的文本：用户当前文本 + run 内已发生的消息（含工具结果）。

    web_fetch 的 URL 多来自 web_search 的**工具结果**（在 messages 里、不在 ev 文本），
    故必须连消息一起扫；全程内存、无 IO，满足 visible_when 每步廉价求值的约束。
    """
    ev = ctx.deps.ev if ctx.deps is not None else None
    if ev is not None:
        yield ev.text
        yield ev.raw_text
    for msg in ctx.messages:
        yield str(msg)


def context_has_url(ctx: VisibilityScanCtx) -> bool:
    """``web_fetch_tool`` 的 visible_when：上下文里出现可抓取 URL 时才暴露。"""
    ev = ctx.deps.ev if ctx.deps is not None else None
    if ev is None:
        return True  # 后台 / 能力代理无 ev：不隐藏，交调用方与执行期兜底
    if ev.file_type == "url" and ev.file:
        return True
    for text in _iter_context_texts(ctx):
        if _URL_RE.search(text):
            return True
    return False


def visible_to_admin(ctx: RunContext[ToolContext]) -> bool:
    """管理员专属工具的 visible_when：对普通用户隐藏 schema，减少高危工具噪声。

    无 ev（后台 / 能力代理）时不隐藏，交 check_func 执行期拦截，避免误伤显式装配方。
    仅判 ``user_pm == 0``；需叠加开关 / 白名单等额外条件的（如 command_exec）自行实现。
    """
    ev = ctx.deps.ev if ctx.deps is not None else None
    if ev is None:
        return True
    return ev.user_pm == 0


def capability_only_from_deps(deps: ToolContext | None) -> bool:
    """主人格隐藏；无 deps / 能力代理保持可见。"""
    if deps is None:
        return True
    return not deps.allow_user_outbound


def visible_to_capability_only(ctx: RunContext[ToolContext]) -> bool:
    """主人格隐藏；能力代理 / 无 deps 保持可见。深读走 search_cognition + read_handle。"""
    return capability_only_from_deps(ctx.deps)


GROUP_RECALL_OK_KEY = "group_recall_ok"
SCHED_CREATE_OK_KEY = "sched_create_ok"
SCHED_MUTATE_OK_KEY = "sched_mutate_ok"
MODEL_DECLARES_VIDEO_KEY = "model_declares_video"


def group_recall_allowed(*, is_group: bool, call_to_self: bool, followup_detected: bool) -> bool:
    """群聊回想/发现工具是否该露。点名或任务跟进才开；不含 soft_continue。"""
    if not is_group:
        return True
    return call_to_self or followup_detected


def visible_when_group_recall(ctx: RunContext[ToolContext]) -> bool:
    """PIN 恒可见。未点名由 check_func 拒执行，不拆 schema。"""
    _ = ctx
    return True


def sched_tool_visibility(
    *,
    is_group: bool,
    address_gated: bool,
    call_to_self: bool,
    followup_detected: bool,
    has_active_schedules: bool,
    manage_form: bool = False,
) -> tuple[bool, bool]:
    """返回 (新建可见, 变更可见)。管理形藏新建，不要求 history 里有 ToolCall。"""
    addressed = (not is_group) or ((not address_gated) and (call_to_self or followup_detected))
    managing = followup_detected or manage_form
    create_ok = addressed and not managing
    mutate_ok = (not is_group) or (addressed and (has_active_schedules or managing))
    return create_ok, mutate_ok


def visible_when_sched_create(ctx: RunContext[ToolContext]) -> bool:
    """调度新建恒可见。管理形由 check_func 拒执行，不拆 schema。"""
    _ = ctx
    return True


def visible_when_sched_mutate(ctx: RunContext[ToolContext]) -> bool:
    """调度变更恒可见。未点名由 check_func 拒执行，不拆 schema。"""
    _ = ctx
    return True


_GATE_REJECT_MARKERS: tuple[str, ...] = (
    "本轮是管理已有条目",
    "本轮未点名：不要",
)


def tool_return_is_gate_reject(content: str) -> bool:
    """PIN check_func 拒绝回执。outcome 仍可能是 success，世界没改。"""
    return any(m in content for m in _GATE_REJECT_MARKERS)


def check_sched_create(deps: ToolContext) -> tuple[bool, str]:
    """create_ok=False 时拒 add_*，提示改用查询/修改/取消。"""
    extra = deps.extra
    if SCHED_CREATE_OK_KEY not in extra:
        return True, ""
    if bool(extra[SCHED_CREATE_OK_KEY]):
        return True, ""
    return False, "本轮是管理已有条目：请用查询/修改/取消，不要新建。"


def check_sched_mutate(deps: ToolContext) -> tuple[bool, str]:
    """mutate_ok=False 时拒 list/modify/cancel/pause/resume。"""
    extra = deps.extra
    if SCHED_MUTATE_OK_KEY not in extra:
        return True, ""
    if bool(extra[SCHED_MUTATE_OK_KEY]):
        return True, ""
    return False, "本轮未点名：不要查询/修改/取消定时任务。"


def check_group_recall(deps: ToolContext) -> tuple[bool, str]:
    """recall_ok=False 时拒发现/委派/回想。"""
    extra = deps.extra
    if GROUP_RECALL_OK_KEY not in extra:
        return True, ""
    if bool(extra[GROUP_RECALL_OK_KEY]):
        return True, ""
    return False, "本轮未点名：不要调用发现/委派/回想。"


def visibility_user_hint(
    *,
    is_group: bool,
    call_to_self: bool,
    followup_detected: bool,
    has_active_task: bool,
    create_ok: bool,
) -> str:
    """进当前 user 的结构 hint；用（系统：）包裹以便入史可识别。"""
    if is_group and not call_to_self and not followup_detected and not has_active_task:
        return "（系统：本轮未点名：不要调用发现/调度/回想；默认 <SILENCE>。）"
    if not create_ok:
        return "（系统：本轮是管理已有条目：不要新建，用查询/修改/取消。）"
    return ""


def context_has_image(ctx: VisibilityScanCtx) -> bool:
    """``read_image`` 的 visible_when：当前轮或上下文里有图片时才暴露。"""
    ev = ctx.deps.ev if ctx.deps is not None else None
    if ev is None:
        return True
    if ev.image_id or ev.image_id_list or ev.image or ev.image_list:
        return True
    # 懒加载历史里的图片以"图片ID: img_xxx"文本形式留存，扫到也放行
    for text in _iter_context_texts(ctx):
        if "img_" in text or "图片ID" in text:
            return True
    return False


def context_has_video(ctx: VisibilityScanCtx) -> bool:
    """``read_video``：有视频句柄且当前模型声明 video 才暴露。"""
    deps = ctx.deps
    if deps is None:
        return True
    extra = deps.extra
    if MODEL_DECLARES_VIDEO_KEY in extra and extra[MODEL_DECLARES_VIDEO_KEY] is False:
        return False
    ev = deps.ev
    if ev is None:
        return True
    if ev.video_id or ev.video_id_list:
        return True
    for text in _iter_context_texts(ctx):
        if "vid_" in text or "视频ID" in text:
            return True
    return False
