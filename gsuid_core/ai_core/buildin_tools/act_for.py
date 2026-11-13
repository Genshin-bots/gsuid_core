"""act_for：多目标收件箱批次里切换工具身份。

只在本批 pending 项跨多个用户时出现（``visible_when``）；调用后本轮工具执行
改用目标项的 ``Event``，发送目标仍由回复计划决定、不受换绑影响。
"""

from typing import Union

from pydantic_ai import RunContext, ToolReturn

from gsuid_core.bot import Bot
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.inbox.turn import current_turn
from gsuid_core.ai_core.control.lines import ACT_FOR_REQUIRED_DENY, act_for_confirm_line, act_for_unknown_ref_line

# ACT_FOR 豁免集：切换身份本身与纯只读/元工具在多目标下也放行。
ACT_FOR_EXEMPT_TOOLS = frozenset({"act_for", "find_tools", "read_handle", "capability_map"})


def _act_for_visible(ctx: RunContext[ToolContext]) -> bool:
    turn = current_turn()
    return turn is not None and turn.multi_target


def _parse_ref(ref: str) -> int | None:
    s = ref.strip().lstrip("#")
    if not s.isdigit():
        return None
    return int(s)


@ai_tools(category="buildin", visible_when=_act_for_visible, brief="多目标批次里声明当前替谁执行工具")
async def act_for(ctx: RunContext[ToolContext], ref: str) -> Union[str, ToolReturn]:
    """本批有多人找你时，声明接下来替谁执行工具（传入 <reply> 里的 #编号）。

    换人再调一次。返回确认行；编号不在待回应项里时返回结构化错误。
    """
    turn = current_turn()
    if turn is None or not turn.multi_target:
        return ACT_FOR_REQUIRED_DENY
    seq = _parse_ref(ref)
    if seq is None:
        return ToolReturn(return_value={"error": "invalid_ref", "ref": ref})
    entry = turn.entry_for(seq)
    if entry is None:
        return ToolReturn(return_value={"error": "unknown_ref", "ref": ref, "line": act_for_unknown_ref_line(ref)})
    turn.active_seq = seq
    ev = entry.event
    ctx.deps.ev = ev
    if isinstance(ctx.deps.bot, Bot):
        ctx.deps.bot = Bot(ctx.deps.bot.bot, ev)
    sender_nick = ev.sender["nickname"] if "nickname" in ev.sender else ""
    speaker = sender_nick if isinstance(sender_nick, str) and sender_nick else str(ev.user_id or "")
    return act_for_confirm_line(seq, speaker)
