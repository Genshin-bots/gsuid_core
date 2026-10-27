"""自主行动专用工具。不注册进全局工具表，只挂在这一轮的 Agent 上。"""

from __future__ import annotations

from pathlib import Path
from contextvars import Token, ContextVar
from dataclasses import field, dataclass
from collections.abc import Callable, Sequence, Awaitable

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from gsuid_core.models import Event
from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.tool_risk import HIGH_RISK_TOOL_NAMES
from gsuid_core.ai_core.autonomy.models import GroupObservation, CareTargetSnapshot
from gsuid_core.ai_core.heartbeat.dispatcher import get_dispatcher, make_target_key

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
        "send_message_by_ai",
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
    allowed_groups: dict[str, GroupObservation] = field(default_factory=dict)
    allowed_users: dict[str, CareTargetSnapshot] = field(default_factory=dict)
    sent_count: int = 0
    sent_targets: list[str] = field(default_factory=list)
    now: float = 0.0
    hour_cap: int = 2
    day_cap: int = 8
    last_sent_user_id: str = ""
    last_sent_group_id: str = ""


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


async def index_note_node(
    persona_name: str,
    title: str,
    body: str,
    handle: str,
    *,
    bot_self_id: str = "",
) -> None:
    """把自主行动产物登记到认知节点表，供 search_cognition 和 read_handle 回想。"""
    from datetime import datetime

    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember

    ref = f"autonomy_note:{persona_name}:{title}"
    summary = body.replace("\n", " ").strip()[:300]
    await remember(
        MemoryWrite(
            kind=CogKind.SELF_NOTE,
            ref=ref,
            scope_key=make_scope_key(ScopeType.SELF, bot_self_id) if bot_self_id else "",
            title=title,
            summary=summary,
            as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
            source="autonomy",
            handle=handle,
        )
    )


async def sync_persona_artifacts(persona_name: str, bot_self_id: str = "") -> int:
    """把已有磁盘笔记同步进认知节点表，用于重启时补全索引。"""
    folder = artifact_dir(persona_name)
    if not folder.exists():
        return 0
    count = 0
    for path_item in folder.glob("*.txt"):
        stem = path_item.stem
        if not stem:
            continue
        try:
            content = path_item.read_text(encoding="utf-8")
        except OSError:
            continue
        handle = f"note:{persona_name}:{stem}"
        await index_note_node(persona_name, stem, content, handle, bot_self_id=bot_self_id)
        count += 1
    return count


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
    res = write_artifact(title, body, persona_name=persona_name)
    if res.startswith("已写入"):
        stem = safe_filename(title).removesuffix(".txt")
        handle = f"note:{persona_name}:{stem}"
        bot_self_id = state.bot_self_id if state is not None else ""
        await index_note_node(persona_name, stem, body, handle, bot_self_id=bot_self_id)
        return f"已写入笔记《{stem}》（句柄: {handle}）"
    return res


def _resolve_send_target(
    state: GuardState,
    *,
    target_id: str,
    target_type: str,
    group_id: str,
    user_id: str,
) -> tuple[str | None, str]:
    """解析调用方写出的目的地。用户目标不会改写成快照里的群。"""
    named_group = group_id.strip()
    named_user = user_id.strip()
    tid = target_id.strip()
    ttype = target_type.strip().lower()
    dest_group_id: str | None = named_group or None
    dest_user_id = named_user
    if tid:
        if ttype == "user":
            if not dest_user_id:
                dest_user_id = tid
        elif ttype == "group":
            if dest_group_id is None:
                dest_group_id = tid
        elif tid in state.allowed_groups and dest_group_id is None:
            dest_group_id = tid
        elif tid in state.allowed_users and not dest_user_id:
            dest_user_id = tid
        elif dest_group_id is None:
            dest_group_id = tid
    if dest_group_id is None and not dest_user_id:
        if len(state.allowed_groups) == 1 and not state.allowed_users:
            dest_group_id = next(iter(state.allowed_groups.keys()))
        elif len(state.allowed_users) == 1 and not state.allowed_groups:
            dest_user_id = next(iter(state.allowed_users.keys()))
    return dest_group_id, dest_user_id


async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    text: str,
    target_id: str = "",
    target_type: str = "group",
    group_id: str = "",
    user_id: str = "",
) -> str:
    """主动向观察底片中的群或关心对象发送一条消息。

    单次自主行动只允许成功发送 1 条消息。发送前框架会自动进行防打扰与频次风控。
    私聊保持私聊。回群只用调用里写出的群号，不用关心对象最近待过的另一个群。

    Args:
        ctx: 工具上下文。
        text: 要发送的消息内容（以你的人格口吻自然表达）。
        target_id: 目标 ID，可以是群号（如 "123456"）或用户 ID（如 "10001"），必须来自底片。
        target_type: 目标类型，"group"（群聊）或 "user"（私聊），默认为 "group"。
        group_id: 可选，群号。传入后发到这个群。
        user_id: 可选，用户 ID。只传用户 ID 时发私聊；和群号一起传时在该群 @ 此人。
    """
    del ctx
    state = _GUARD.get()
    if state is None:
        return "拒绝：当前不在自主行动里"
    if state.sent_count >= 1:
        return "⚠️ 本次自主行动已经发送过消息了，请勿单轮重复刷屏。"

    body = text.strip()
    if not body:
        return "拒绝发送：消息内容不能为空"

    dest_group_id, dest_user_id = _resolve_send_target(
        state,
        target_id=target_id,
        target_type=target_type,
        group_id=group_id,
        user_id=user_id,
    )
    if dest_group_id is None and not dest_user_id:
        avail_g = list(state.allowed_groups.keys())
        avail_u = list(state.allowed_users.keys())
        return f"拒绝发送：必须指定发送目标 target_id。底片可选群：{avail_g}，关心用户：{avail_u}"

    bot_id = state.bot_id
    bot_self_id = state.bot_self_id
    ws_bot_id = state.ws_bot_id
    if dest_user_id and dest_user_id not in state.allowed_users:
        avail_u = list(state.allowed_users.keys())
        return f"拒绝发送：用户 {dest_user_id} 不在本次关心名单中。只能发送给名单内的用户：{avail_u}"
    if dest_group_id:
        if dest_group_id not in state.allowed_groups:
            avail_g = list(state.allowed_groups.keys())
            return f"拒绝发送：群 {dest_group_id} 不在本次观察底片中。只能发送给底片内的群：{avail_g}"
        g_obs = state.allowed_groups[dest_group_id]
        bot_id = g_obs.bot_id or bot_id
        bot_self_id = g_obs.bot_self_id or bot_self_id
        ws_bot_id = g_obs.ws_bot_id or ws_bot_id
    elif dest_user_id:
        # 快照上的群是这个人最近待过的地方，不是这条私聊的目的地。
        u_snap = state.allowed_users[dest_user_id]
        bot_id = u_snap.bot_id or bot_id
        bot_self_id = u_snap.bot_self_id or bot_self_id
        ws_bot_id = u_snap.ws_bot_id or ws_bot_id

    target_key = make_target_key(dest_group_id, dest_user_id or None)
    dispatcher = get_dispatcher()
    clock = state.now
    if dispatcher.should_suppress_by_rate(target_key, max_per_hour=state.hour_cap, max_per_day=state.day_cap):
        return (
            "发送被拦截：目标近期主动发言配额已达上限（防打扰）。请勿继续发信，可将成果用 artifact_put 存为个人笔记。"
        )
    if dispatcher.should_suppress_heartbeat(target_key):
        return "发送被拦截：目标当前处于防打扰冷静期。请勿连续打扰，建议转为自我探索并记入笔记。"

    if clock > 0 and dest_group_id and dest_group_id in state.allowed_groups:
        g_obs = state.allowed_groups[dest_group_id]
        if g_obs.last_assistant_ts > 0 and (clock - g_obs.last_assistant_ts < 60):
            return "发送被拦截：AI 刚刚（60秒内）在群内已有发言。为了防刷屏，请勿连续发信。"

    from gsuid_core.models import Event
    from gsuid_core.ai_core.proactive.emitter import emit_proactive_message

    event = Event(
        bot_id=bot_id or "Bot",
        real_bot_id=bot_id or "Bot",
        bot_self_id=bot_self_id,
        user_id=dest_user_id or "",
        user_pm=6,
        user_type="group" if dest_group_id else "direct",
        group_id=dest_group_id,
        WS_BOT_ID=ws_bot_id or None,
    )
    sent = await emit_proactive_message(
        event,
        body[:400],
        source="autonomy",
        trigger_reason=f"{state.persona_name} 自主发信",
        suppress_when_heartbeat_recent=True,
        at_user_id=dest_user_id if (dest_group_id and dest_user_id) else None,
    )
    if sent:
        state.sent_count += 1
        state.last_sent_user_id = dest_user_id
        state.last_sent_group_id = dest_group_id or ""
        if dest_group_id and dest_user_id:
            target_desc = f"群 {dest_group_id}（已前置 @{dest_user_id}）"
        elif dest_group_id:
            target_desc = f"群 {dest_group_id}"
        else:
            target_desc = f"用户 {dest_user_id}"
        state.sent_targets.append(target_desc)
        return f"已成功向 {target_desc} 发送消息：{body[:100]}"
    return "发送失败：当前处于冷却、频次限额或网络离线，消息未发出。"


def build_autonomy_tools() -> list[Tool[ToolContext]]:
    """已注册的搜索 / 读图工具，加上本文件的四个命令。缺一个就跳过。"""
    from gsuid_core.ai_core.register import find_tool_base

    mounted: list[Tool[ToolContext]] = [
        Tool(list_plugin_commands),
        Tool(run_bot_command),
        Tool(artifact_put),
        Tool(send_message_by_ai),
    ]
    for name in (
        "find_tools",
        "web_search_tool",
        "web_fetch_tool",
        "read_image",
        "search_cognition",
        "read_chat_history",
    ):
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
