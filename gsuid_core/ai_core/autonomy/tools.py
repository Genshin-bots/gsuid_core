"""自主行动专用工具。不注册进全局工具表，只挂在这一轮的 Agent 上。"""

from __future__ import annotations

from pathlib import Path
from contextvars import Token, ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Sequence, Awaitable

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from gsuid_core.models import Event
from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.tool_risk import HIGH_RISK_TOOL_NAMES

USER_PM = 6
ARTIFACT_LIMIT_BYTES = 32 * 1024
AUTONOMY_TOOL_ALLOWLIST = frozenset(
    {
        "find_tools",
        "web_search_tool",
        "web_fetch_tool",
        "read_image",
        "search_cognition",
        "list_plugin_commands",
        "run_bot_command",
        "artifact_put",
    }
)
_EXECUTABLE = frozenset({"command", "prefix", "fullmatch", "keyword", "suffix"})
_RANK = {"command": 5, "prefix": 4, "fullmatch": 3, "suffix": 2, "keyword": 1}


@dataclass(slots=True)
class CommandHit:
    """一条触发器。``func`` 只在真正执行时使用。"""

    plugin_name: str
    sv_name: str
    trigger_type: str
    keyword: str
    pm: int
    plugin_pm: int
    func: object | None = None


@dataclass(frozen=True, slots=True)
class CommandChoice:
    hit: CommandHit | None
    denied: bool
    reason: str


@dataclass(slots=True)
class GuardState:
    """本轮观察名单。工具从这里读，不从模型自由填的群号读。"""

    persona_name: str
    whitelist: frozenset[str]
    sessions: dict[str, tuple[str | None, str, str, str]]
    bot_id: str
    bot_self_id: str
    ws_bot_id: str


CommandExecutor = Callable[[CommandHit, Event], Awaitable[str]]

_GUARD: ContextVar[GuardState | None] = ContextVar("gs_autonomy_guard", default=None)


def autonomy_tool_allowed(name: str) -> bool:
    """钩子补进来的工具也要过这张表。发送和高危执行不在表里。"""
    if name in HIGH_RISK_TOOL_NAMES:
        return False
    return name in AUTONOMY_TOOL_ALLOWLIST


def bind_guard(state: GuardState) -> Token[GuardState | None]:
    return _GUARD.set(state)


def reset_guard(token: Token[GuardState | None]) -> None:
    _GUARD.reset(token)


def select_command(text: str, hits: Sequence[CommandHit]) -> CommandChoice:
    """最长的命中优先。它如果是管理命令，就不改去执行更短的普通命令。"""
    body = text.strip()
    if not body:
        return CommandChoice(None, False, "空命令")
    matched = [hit for hit in hits if _matches(body, hit)]
    if not matched:
        return CommandChoice(None, False, "没有匹配的命令")
    best = max(matched, key=lambda hit: (len(hit.keyword), _RANK[hit.trigger_type] if hit.trigger_type in _RANK else 0))
    if best.pm < USER_PM or best.plugin_pm < USER_PM:
        return CommandChoice(best, True, "权限不足：这条命令需要更高权限")
    if best.trigger_type not in _EXECUTABLE:
        return CommandChoice(best, True, "不代发这类触发器")
    return CommandChoice(best, False, "")


def format_commands(hits: Sequence[CommandHit], keyword: str = "") -> str:
    needle = keyword.strip()
    lines: list[str] = []
    for hit in hits:
        blob = f"{hit.plugin_name} {hit.sv_name} {hit.keyword}"
        if needle and needle not in blob:
            continue
        lines.append(f"{hit.plugin_name} / {hit.sv_name} / {hit.trigger_type} / {hit.keyword}")
        if len(lines) >= 40:
            break
    if not lines:
        return "没有可列出的命令"
    return "\n".join(lines)


def build_command_event(
    *,
    command_text: str,
    keyword: str,
    user_id: str,
    group_id: str | None,
    bot_id: str,
    bot_self_id: str,
    ws_bot_id: str,
) -> Event:
    """权限固定为普通成员。主人 ID 也不会把 user_pm 抬高。"""
    event = Event(
        bot_id=bot_id or "Bot",
        bot_self_id=bot_self_id,
        user_id=user_id,
        user_pm=USER_PM,
        user_type="group" if group_id else "direct",
        group_id=group_id,
        WS_BOT_ID=ws_bot_id or None,
        text=command_text,
        raw_text=command_text,
        command=keyword,
    )
    return event


async def run_guarded_command(
    command_text: str,
    *,
    target_user_id: str,
    whitelist: frozenset[str],
    persona_name: str,
    bot_id: str,
    bot_self_id: str,
    ws_bot_id: str,
    group_id: str | None,
    hits: Sequence[CommandHit] | None = None,
    executor: CommandExecutor | None = None,
) -> str:
    target = target_user_id.strip()
    if target and target not in whitelist:
        return "拒绝：这个用户不在本轮观察名单里"
    user_id = target if target else f"autonomy:{persona_name}"
    catalog = list(hits) if hits is not None else scan_plugin_commands()
    choice = select_command(command_text, catalog)
    if choice.denied:
        return choice.reason or "拒绝：不能执行"
    if choice.hit is None:
        return choice.reason or "没有匹配的命令"
    event = build_command_event(
        command_text=command_text.strip(),
        keyword=choice.hit.keyword,
        user_id=user_id,
        group_id=group_id,
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        ws_bot_id=ws_bot_id,
    )
    runner = executor if executor is not None else execute_command_hit
    return await runner(choice.hit, event)


def scan_plugin_commands() -> list[CommandHit]:
    from gsuid_core.sv import SL

    found: list[CommandHit] = []
    for sv in SL.lst.values():
        if not sv.is_initialized or not sv.enabled:
            continue
        if "plugins" not in sv.__dict__:
            continue
        plugin = sv.plugins
        if not plugin.enabled:
            continue
        plugin_name = plugin.name if isinstance(plugin.name, str) and plugin.name else sv.name
        sv_pm = sv.pm if isinstance(sv.pm, int) else 0
        plugin_pm = plugin.pm if isinstance(plugin.pm, int) else 0
        for _trigger_type, table in sv.TL.items():
            for key, trigger in table.items():
                keyword = (
                    trigger.keyword.strip() if isinstance(trigger.keyword, str) and trigger.keyword.strip() else key
                )
                found.append(
                    CommandHit(
                        plugin_name=plugin_name,
                        sv_name=sv.name,
                        trigger_type=trigger.type,
                        keyword=keyword,
                        pm=sv_pm,
                        plugin_pm=plugin_pm,
                        func=trigger.func,
                    )
                )
    return found


async def execute_command_hit(hit: CommandHit, event: Event) -> str:
    if hit.func is None or not callable(hit.func):
        return "失败：命令没有处理函数"
    from gsuid_core.bot import Bot
    from gsuid_core.gss import gss
    from gsuid_core.ai_core.trigger_bridge import run_trigger_via_mockbot

    raw = None
    if event.WS_BOT_ID and event.WS_BOT_ID in gss.active_bot:
        raw = gss.active_bot[event.WS_BOT_ID]
    elif gss.active_bot:
        raw = next(iter(gss.active_bot.values()))
    if raw is None:
        return "没有在线的适配器，这条命令没有执行"
    wrapped = hit.func.__dict__["__wrapped__"] if "__wrapped__" in hit.func.__dict__ else None
    target = wrapped if callable(wrapped) else hit.func
    bot = Bot(raw, event)
    # 插件处理函数自己抛错时，不能把这一轮自主行动打崩。
    try:
        return await run_trigger_via_mockbot(bot, event, target)
    except Exception as exc:
        return f"失败：{exc}"


def write_artifact(title: str, body: str, *, persona_name: str, root: Path | None = None) -> str:
    text = body.strip()
    if not text:
        return "拒绝：正文是空的"
    if len(text.encode("utf-8")) > ARTIFACT_LIMIT_BYTES:
        return "拒绝：笔记超过 32KB"
    folder = artifact_dir(persona_name, root)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_filename(title)
        path.write_text(text, encoding="utf-8")
    except OSError:
        return "失败：笔记没有写进去"
    return f"已写入 {path.name}"


def artifact_dir(persona_name: str, root: Path | None = None) -> Path:
    base = root if root is not None else get_res_path() / "ai_core" / "autonomy" / "artifacts"
    return base / safe_filename(persona_name).removesuffix(".txt")


def safe_filename(title: str) -> str:
    kept: list[str] = []
    for char in title.strip()[:40]:
        if char.isascii() and (char.isalnum() or char in "-_"):
            kept.append(char)
        elif "\u4e00" <= char <= "\u9fff":
            kept.append(char)
    stem = "".join(kept) or "note"
    return f"{stem}.txt"


async def list_plugin_commands(ctx: RunContext[ToolContext], keyword: str = "") -> str:
    """按插件列出普通成员能看见的触发器。

    返回插件名、功能名、触发类型和触发词。regex / message 只供查看，不会被代为执行。

    Args:
        ctx: 工具上下文。自主行动不使用其中的用户权限。
        keyword: 可选过滤词。留空则列出前 40 条。
    """
    del ctx
    return format_commands(scan_plugin_commands(), keyword)


async def run_bot_command(
    ctx: RunContext[ToolContext],
    command_text: str,
    target_user_id: str = "",
) -> str:
    """以普通成员身份执行一条插件命令。

    target_user_id 必须是本轮观察名单里的用户。留空则用自主行动自己的身份，不能查别人的绑定。
    管理命令、正则触发和消息触发都不会执行。

    Args:
        ctx: 工具上下文。
        command_text: 用户平时会发送的整句命令。
        target_user_id: 观察名单中的用户 ID。
    """
    del ctx
    state = _GUARD.get()
    if state is None:
        return "拒绝：当前不在自主行动里"
    group_id, bot_id, bot_self_id, ws_bot_id = _session_of(state, target_user_id)
    return await run_guarded_command(
        command_text,
        target_user_id=target_user_id,
        whitelist=state.whitelist,
        persona_name=state.persona_name,
        bot_id=bot_id or state.bot_id,
        bot_self_id=bot_self_id or state.bot_self_id,
        ws_bot_id=ws_bot_id or state.ws_bot_id,
        group_id=group_id,
    )


async def artifact_put(ctx: RunContext[ToolContext], title: str, body: str) -> str:
    """把这次探索写成一份本地笔记，不发给任何人。

    Args:
        ctx: 工具上下文。
        title: 笔记标题，只保留字母、数字、下划线和汉字。
        body: 笔记正文，最长 32KB。
    """
    del ctx
    state = _GUARD.get()
    persona_name = state.persona_name if state is not None else "autonomy"
    return write_artifact(title, body, persona_name=persona_name)


def build_autonomy_tools() -> list[Tool[ToolContext]]:
    """已注册的搜索 / 读图工具，加上本文件的三个命令。缺一个就跳过，不改去装发送工具。"""
    from gsuid_core.ai_core.register import find_tool_base

    mounted: list[Tool[ToolContext]] = [
        Tool(list_plugin_commands),
        Tool(run_bot_command),
        Tool(artifact_put),
    ]
    for name in ("find_tools", "web_search_tool", "web_fetch_tool", "read_image", "search_cognition"):
        base = find_tool_base(name)
        if base is not None and autonomy_tool_allowed(base.name):
            mounted.append(base.tool)
    return [tool for tool in mounted if autonomy_tool_allowed(tool.name)]


def _session_of(state: GuardState, target_user_id: str) -> tuple[str | None, str, str, str]:
    uid = target_user_id.strip()
    if uid and uid in state.sessions:
        return state.sessions[uid]
    return None, state.bot_id, state.bot_self_id, state.ws_bot_id


def _matches(body: str, hit: CommandHit) -> bool:
    key = hit.keyword.strip()
    if not key:
        return False
    kind = hit.trigger_type
    if kind in {"command", "prefix"}:
        return body == key or body.startswith(f"{key} ")
    if kind == "fullmatch":
        return body == key
    if kind == "suffix":
        return len(key) >= 2 and body.endswith(key)
    if kind == "keyword":
        return len(key) >= 2 and key in body
    return False
