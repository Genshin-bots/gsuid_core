"""命令执行器：结构安全分析（闸门②）、策略引擎（闸门③）与统一审批领域适配。"""

from __future__ import annotations

import re
import json
import shlex
import platform
from enum import Enum
from typing import Set, List, Optional
from pathlib import Path
from dataclasses import field, dataclass

from gsuid_core.models import Event
from gsuid_core.ai_core import approval as approval_center
from gsuid_core.ai_core.approval import AIApprovalRequest
from gsuid_core.ai_core.command_exec.models import (
    cfg_int,
    cfg_str,
    cfg_bool,
    audit_log,
    cfg_str_list,
)
from gsuid_core.ai_core.command_exec.executor import ExecResult, run_resolved

# ── 闸门②：引号感知结构分析 ────────────────────────────────────

_IS_WINDOWS = platform.system() == "Windows"
_META_CHARS = set("|&;<>$`(){}\n")
_WIN_EXTRA_META = set("%^")
_DIRECTION_CONTROLS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"

NETWORK_TOOLS = {
    "curl",
    "wget",
    "git",
    "npm",
    "pnpm",
    "yarn",
    "npx",
    "pip",
    "pip3",
    "uv",
    "ping",
    "ssh",
    "scp",
    "rsync",
    "ftp",
    "telnet",
    "nc",
    "ncat",
}
_INSTALL_SUBCMDS = {
    ("npm", "install"),
    ("npm", "i"),
    ("pnpm", "add"),
    ("yarn", "add"),
    ("pip", "install"),
    ("pip3", "install"),
    ("uv", "pip"),
    ("git", "clone"),
}
_URL_RE = re.compile(r"\b(https?|ftp|git|ssh)://|\bwww\.", re.IGNORECASE)


@dataclass
class SimpleCommand:
    argv: List[str]
    executable: str


@dataclass
class CommandPlan:
    raw: str
    ok: bool
    reason: str = ""
    commands: List[SimpleCommand] = field(default_factory=list)
    touches_network: bool = False
    risk: str = "low"
    findings: List[str] = field(default_factory=list)
    path_escapes: List[str] = field(default_factory=list)


def _sanitize(raw: str) -> str:
    """去控制字符（保留 \\t\\n）+ 去 Unicode 方向欺骗字符。"""
    cleaned = "".join(c for c in raw if c.isprintable() or c in "\t\n")
    for c in _DIRECTION_CONTROLS:
        cleaned = cleaned.replace(c, "")
    return cleaned.strip()


def _build_simple(words: List[str]) -> SimpleCommand:
    if not words:
        return SimpleCommand(argv=[], executable="")
    return SimpleCommand(argv=words, executable=Path(words[0]).name.lower())


def _win_argv(raw: str) -> List[str]:
    """Windows 感知分词：保留反斜杠路径、剥掉两端引号。"""
    lex = shlex.shlex(raw, posix=False)
    lex.whitespace_split = True
    out: List[str] = []
    for tok in lex:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        out.append(tok)
    return out


def _tokenize(raw: str) -> List[str]:
    return _win_argv(raw) if _IS_WINDOWS else shlex.split(raw, posix=True)


def _find_unquoted_meta(text: str, meta: Set[str]) -> List[str]:
    """扫描引号外的元字符。POSIX 上 '\\' 转义下一字符；Windows 上不转义。"""
    hits: List[str] = []
    quote: Optional[str] = None
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if (not _IS_WINDOWS) and ch == "\\":
            escaped = True
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch in meta and ch not in hits:
            hits.append(ch)
    return hits


def _is_install(sc: SimpleCommand) -> bool:
    return len(sc.argv) >= 2 and (sc.executable, sc.argv[1].lower()) in _INSTALL_SUBCMDS


def _classify(sc: SimpleCommand) -> tuple[bool, str]:
    exe = sc.executable
    touches = exe in NETWORK_TOOLS or any(_URL_RE.search(a) for a in sc.argv[1:])
    if _is_install(sc):
        touches = True
    risk = "high" if _is_install(sc) else ("medium" if touches else "low")
    return touches, risk


def _looks_like_path(arg: str) -> bool:
    if _URL_RE.search(arg):
        return False
    return "/" in arg or "\\" in arg or ".." in arg or arg.startswith("~") or (len(arg) >= 2 and arg[1] == ":")


def _find_escaping_paths(argv: List[str], sandbox: Path) -> List[str]:
    """扫 argv[1:]，返回解析后逃出 sandbox 的路径参数（跳过选项与 URL）。"""
    escapes: List[str] = []
    sandbox_root = sandbox.resolve()
    for arg in argv[1:]:
        if arg.startswith("-") or not _looks_like_path(arg):
            continue
        candidate = Path(arg)
        resolved = candidate.resolve() if candidate.is_absolute() else (sandbox_root / candidate).resolve()
        if not resolved.is_relative_to(sandbox_root):
            escapes.append(arg)
    return escapes


def _apply_path_guard(plan: CommandPlan) -> CommandPlan:
    if not plan.ok or not plan.commands:
        return plan
    from gsuid_core.ai_core.command_exec.executor import get_sandbox_dir

    escapes = _find_escaping_paths(plan.commands[0].argv, get_sandbox_dir())
    if escapes:
        plan.path_escapes = escapes
        plan.findings.append(f"参数路径越界 {escapes}")
    return plan


def analyze(raw: str) -> CommandPlan:
    raw = _sanitize(raw)
    if not raw:
        return CommandPlan(raw, ok=False, reason="命令为空")

    meta = _META_CHARS | (_WIN_EXTRA_META if _IS_WINDOWS else set())
    hits = _find_unquoted_meta(raw, meta)
    if hits:
        pretty = " ".join(repr(h) for h in hits)
        return CommandPlan(
            raw,
            ok=False,
            risk="high",
            reason=(
                f"含未加引号的 shell 元字符 {pretty}：仅支持单条简单命令"
                "(不支持管道 |、重定向 >、命令链 && ;、后台 &、命令替换 $())。如需分步请多次调用。"
            ),
            findings=[f"shell 元字符 {pretty}"],
        )

    try:
        words = _tokenize(raw)
    except ValueError as e:
        return CommandPlan(raw, ok=False, reason=f"命令解析失败(引号不匹配?): {e}", risk="high")
    if not words:
        return CommandPlan(raw, ok=False, reason="未解析出可执行命令")

    sc = _build_simple(words)
    plan = CommandPlan(raw, ok=True, commands=[sc])
    plan.touches_network, plan.risk = _classify(sc)
    return _apply_path_guard(plan)


# ── 闸门③：策略引擎（allow / deny / approval）────────────────────


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL = "approval"


# 元工具名单（代码级）：具备二次解释 / 任意代码执行 / 外联能力，永不走免审批快速通道。
NEVER_AUTO_ALLOW = {
    "python",
    "python3",
    "node",
    "nodejs",
    "deno",
    "bun",
    "bash",
    "sh",
    "zsh",
    "fish",
    "dash",
    "ruby",
    "perl",
    "php",
    "lua",
    "rscript",
    "osascript",
    "powershell",
    "pwsh",
    "cmd",
    "tclsh",
    "expect",
    "npm",
    "pnpm",
    "yarn",
    "npx",
    "pip",
    "pip3",
    "uv",
    "git",
    "make",
    "cmake",
    "go",
    "cargo",
    "java",
    "dotnet",
    "find",
    "xargs",
    "env",
    "nice",
    "timeout",
    "ssh",
    "scp",
    "rsync",
    "curl",
    "wget",
    "docker",
    "awk",
    "gawk",
    "sed",
    "vim",
    "nvim",
    "emacs",
    "gdb",
}


def _guard_path(decision: Decision, plan: CommandPlan) -> tuple[Decision, str]:
    """仅当本要 ALLOW 时,对逃出沙盒的路径参数按 path_arg_policy 降级。"""
    if decision is not Decision.ALLOW or not plan.path_escapes:
        return decision, ""
    policy = cfg_str("path_arg_policy")
    if policy == "off":
        return decision, ""
    detail = f"参数路径逃出沙盒: {plan.path_escapes}"
    if policy == "deny":
        return Decision.DENY, detail
    return Decision.APPROVAL, detail + "（转审批）"


def decide(plan: CommandPlan) -> tuple[Decision, str]:
    if not plan.ok:
        return Decision.DENY, plan.reason

    exe = plan.commands[0].executable
    deny = {c.lower() for c in cfg_str_list("deny_commands")}
    allow = {c.lower() for c in cfg_str_list("auto_allow_commands")}

    if exe in deny:
        return Decision.DENY, f"'{exe}' 在永久黑名单中"

    if plan.touches_network and not cfg_bool("allow_network"):
        return Decision.DENY, "该命令需要联网,但'允许联网命令'未开启"

    is_meta = exe in NEVER_AUTO_ALLOW
    net_needs_approval = plan.touches_network and cfg_bool("require_approval_for_network")

    mode = cfg_str("approval_mode")
    if mode == "all":
        return Decision.APPROVAL, "当前为'全部审批'模式"
    if is_meta:
        return Decision.APPROVAL, f"'{exe}' 是元工具(可二次执行/外联),强制单次审批"
    if net_needs_approval:
        return Decision.APPROVAL, "联网命令强制审批"
    if mode == "auto":
        return _guard_path(Decision.ALLOW, plan)
    if exe in allow:
        return _guard_path(Decision.ALLOW, plan)
    return Decision.APPROVAL, f"'{exe}' 不在自动放行白名单,需主人审批"


# ── 统一审批中心领域适配 ─────────────────────────────────────────

COMMAND_APPROVAL_CATEGORY = "command_exec"


def format_command_result(command: str, result: ExecResult) -> str:
    body = result.stdout or "（无输出）"
    if result.truncated:
        body += "\n[输出已截断]"
    head = f"✅ 已执行 `{command}`" if result.returncode == 0 else f"⚠️ `{command}` 返回码 {result.returncode}"
    return f"{head}\n```\n{body}\n```"


def _approval_ttl_seconds() -> int:
    raw = cfg_int("approval_ttl_seconds", 600)
    return raw if raw > 0 else 600


async def submit_approval(ev: Optional[Event], plan: CommandPlan, reason: str, request_id: str) -> AIApprovalRequest:
    """把 analyze 后的 argv 快照提交进审批中心（pending）。执行的永远是这份快照。"""
    argv = plan.commands[0].argv if plan.commands else []
    frozen = json.dumps(argv, ensure_ascii=False)
    return await approval_center.submit(
        category=COMMAND_APPROVAL_CATEGORY,
        title=f"`{plan.raw}`（{reason}）",
        ev=ev,
        audience="master",
        ref_key=request_id,
        payload={"raw": plan.raw, "argv": argv, "risk": plan.risk, "reason": reason},
        dedupe_key=f"command:{frozen}",
        effect=f"执行命令：{plan.raw}",
        ttl_seconds=_approval_ttl_seconds(),
    )


def _rebuild_plan(raw: str, argv: List[str]) -> CommandPlan:
    executable = argv[0] if argv else ""
    sc = SimpleCommand(argv=argv, executable=Path(executable).name.lower() if executable else "")
    plan = CommandPlan(raw=raw, ok=bool(argv), commands=[sc] if argv else [])
    if argv:
        plan.touches_network, plan.risk = _classify(sc)
    return plan


def _rebuild_event(req: AIApprovalRequest) -> Event:
    return Event(
        bot_id=req.bot_id,
        user_id=req.operator_user_id,
        bot_self_id=req.bot_self_id,
        user_type=req.user_type if req.user_type in ("group", "direct", "channel", "sub_channel") else "direct",
        group_id=req.group_id,
        real_bot_id=req.bot_id,
        msg_id="",
        user_pm=0,
    )


async def _on_resolve(req: AIApprovalRequest, approved: bool, note: str) -> str:
    """审批中心裁决回调：批准 → 复核 policy → 执行 argv 快照 → 审计。"""
    payload_raw: object = json.loads(req.payload_json or "{}")
    payload = payload_raw if isinstance(payload_raw, dict) else {}
    raw = str(payload["raw"]) if "raw" in payload else req.title
    argv_raw = payload["argv"] if "argv" in payload else []
    argv = [str(x) for x in argv_raw] if isinstance(argv_raw, list) else []

    if not approved:
        return f"🚫 已拒绝审批 #{req.short_id}：`{raw}`。"

    plan = _rebuild_plan(raw, argv)
    decision, why = decide(plan)
    if decision is Decision.DENY:
        return f"🚫 复核未通过，取消执行 #{req.short_id}：{why}"

    ev = _rebuild_event(req)
    result, exec_err = await run_resolved(ev, argv, cfg_int("default_timeout", 60))
    if result is None:
        return f"❌ 执行失败：{exec_err}"
    await audit_log(ev, plan, Decision.APPROVAL, "主人审批通过并执行", request_id=req.ref_key, result=result)
    return format_command_result(raw, result)


def register_command_approval_category() -> None:
    """注册 command_exec。新票和旧票都用命令配置里的审批有效期。"""
    approval_center.register_approval_category(
        COMMAND_APPROVAL_CATEGORY,
        _on_resolve,
        ttl_seconds=_approval_ttl_seconds(),
    )
