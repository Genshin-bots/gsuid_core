"""高危执行与仅主人可触发的能力节点。

两层都认 ``core_config["masters"]``（``_is_master_user``），不认任务行上的
``user_pm``——后者能被派活记录写成 0，非主人照样跑过 ``check_pm``。

1. 节点门：``master_only`` 或白名单含高危工具的节点，非主人不能 ``create_subagent`` /
   ``register_kanban_task`` / 重派；执行器在跑起来之前再拦一次。
2. 工具门：``execute_file`` / ``execute_shell_command`` / ``run_command`` /
   ``run_skill_script`` 无论主人格还是子代理，非主人或没有 Event 都拒绝。
   子代理装配时会把这些名字从工具列表剥掉；Agent 的 ``wrap_tool_execute``
   在函数体之前再拦一次，技能脚本也走这道门。
"""

from typing import TypeVar, Optional, Protocol

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import _is_master_user
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.agent_node.models import AgentNode

# 会在宿主上跑代码或 shell 的工具。写文件不算：没执行入口就落在沙盒里。
HIGH_RISK_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "execute_file",
        "execute_shell_command",
        "run_command",
        "run_skill_script",
    }
)

MASTER_ONLY_NODE_DENY = "🚫 该能力代理仅主人可触发。"
HIGH_RISK_TOOL_DENY = "🚫 高危工具仅主人可执行，已拒绝。"


class _NamedTool(Protocol):
    name: str


_TNamed = TypeVar("_TNamed", bound=_NamedTool)


def operator_is_master(user_id: str) -> bool:
    """``user_id`` 是否在配置的主人名单里。空串不是主人。"""
    uid = str(user_id or "").strip()
    if not uid:
        return False
    return _is_master_user(uid)


def event_is_master(ev: Optional[Event]) -> bool:
    """没有 Event、或说话人/任务归属人不在主人名单 → 不是主人。"""
    if ev is None:
        return False
    return operator_is_master(str(ev.user_id))


def node_requires_master(node: AgentNode) -> bool:
    """显式 ``master_only``，或白名单里已经挂了高危执行工具。"""
    if node.master_only:
        return True
    return any(name in HIGH_RISK_TOOL_NAMES for name in node.tool_names)


def refuse_master_only_node(ev: Optional[Event], node: Optional[AgentNode]) -> Optional[str]:
    """允许则 None；非主人触发受限节点则返回给模型的拒绝文案。"""
    if node is None or not node_requires_master(node):
        return None
    if event_is_master(ev):
        return None
    uid = str(ev.user_id) if ev is not None else ""
    logger.warning(t("log.ai.tool_risk_refused_node", node_id=node.node_id, user_id=uid))
    return f"{MASTER_ONLY_NODE_DENY}（`{node.node_id}`）"


def _log_high_risk_refuse(ev: Optional[Event]) -> str:
    uid = str(ev.user_id) if ev is not None else ""
    logger.warning(t("log.ai.tool_risk_refused_tool", user_id=uid))
    return HIGH_RISK_TOOL_DENY


def check_high_risk_operator(ev: Optional[Event]) -> tuple[bool, str]:
    """``@ai_tools(check_func=...)``：非主人或无 Event 直接拒绝，不开审批票。"""
    if event_is_master(ev):
        return True, ""
    return False, _log_high_risk_refuse(ev)


def high_risk_block(ev: Optional[Event], tool_name: str) -> Optional[str]:
    """高危工具且调用人不是主人时返回拒绝文案，否则 None。"""
    if tool_name not in HIGH_RISK_TOOL_NAMES or event_is_master(ev):
        return None
    return _log_high_risk_refuse(ev)


def _event_from_deps(deps: object) -> Optional[Event]:
    if isinstance(deps, ToolContext):
        return deps.ev
    return None


def skill_tool_visible(ctx: RunContext[object], tool_def: ToolDefinition) -> bool:
    """技能工具集过滤器：非主人看不见 ``run_skill_script``，其余技能工具保留。"""
    if tool_def.name not in HIGH_RISK_TOOL_NAMES:
        return True
    return event_is_master(_event_from_deps(ctx.deps))


async def block_high_risk_execute(
    ctx: RunContext[object],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
    handler: WrapToolExecuteHandler,
) -> object:
    """``wrap_tool_execute``：非主人的高危调用不进入工具函数。"""
    name = (call.tool_name or tool_def.name or "").strip()
    denied = high_risk_block(_event_from_deps(ctx.deps), name)
    if denied is not None:
        return denied
    return await handler(args)


def visible_to_master_operator(ctx: RunContext[ToolContext]) -> bool:
    """高危工具的 visible_when。无 Event 也隐藏，避免能力代理在缺归属人时看见它们。"""
    ev = ctx.deps.ev if ctx.deps is not None else None
    return event_is_master(ev)


def strip_high_risk_tools(tools: list[_TNamed], ev: Optional[Event]) -> list[_TNamed]:
    """非主人的能力代理工具列表里拿掉高危执行工具。主人保留，交给 check_func。"""
    if event_is_master(ev):
        return tools
    kept: list[_TNamed] = []
    removed: list[str] = []
    for tool in tools:
        if tool.name in HIGH_RISK_TOOL_NAMES:
            removed.append(tool.name)
            continue
        kept.append(tool)
    if removed:
        uid = str(ev.user_id) if ev is not None else ""
        logger.info(t("log.ai.tool_risk_stripped", user_id=uid, names=",".join(removed)))
    return kept
