"""闸门②：结构安全分析（纯 stdlib · 零依赖 · 完整类型）。

只放行「单条简单命令」。在 shell=False 下管道 / 替换 / 重定向等本就是字面 token（不会被
执行），但它们意味着用户期待我们不提供的 shell 语义——故一律拒绝（纵深防御 + 明确边界）。
用**引号感知**扫描器检测未加引号的 shell 元字符：既拦住 `a | b` / `$()` / `;` / 后台 `&`，
又不误伤 `python -c "print(1)"` 这类引号内的字面括号。argv 用 shlex 提取（Windows 感知）。

历史：初版依赖 bashlex（GNU bash 语法的 Python 移植），但其两年未更新且无类型标注（basedpyright
无法解析、AST 节点全是 object）。对「只放行单命令」的安全闸门而言，引号感知元字符扫描器更轻、
更安全（拒绝优先）、且完全可被静态类型检查。若将来要安全支持管道/重定向,再引入 tree-sitter-bash
之类的现代 AST 解析器。见设计文档 §5.2。
"""

import re
import shlex
import platform
from typing import Set, List, Optional
from pathlib import Path
from dataclasses import field, dataclass

_IS_WINDOWS = platform.system() == "Windows"

# 元字符：出现在引号外即拒绝（多命令 / 替换 / 重定向 / 管道 / 后台 / 分组 / 换行）。
_META_CHARS = set("|&;<>$`(){}\n")
# Windows 额外：% 变量展开、^ cmd 转义（.cmd/.bat 会二次经 cmd.exe,见 §5.4）。
_WIN_EXTRA_META = set("%^")

# Unicode 方向欺骗字符（视觉看着是 A、实际是 B）。
_DIRECTION_CONTROLS = "‪‫‬‭‮⁦⁧⁨⁩"

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
    executable = Path(words[0]).name.lower()
    return SimpleCommand(argv=words, executable=executable)


def _win_argv(raw: str) -> List[str]:
    """Windows 感知分词：保留反斜杠路径、剥掉两端引号（Windows 上 '\\' 是路径分隔符,非转义）。"""
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
    """扫描引号外的元字符。POSIX 上 '\\' 转义下一字符;Windows 上 '\\' 是路径分隔符不转义。"""
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
    if len(sc.argv) < 2:
        return False
    return (sc.executable, sc.argv[1].lower()) in _INSTALL_SUBCMDS


def _classify(sc: SimpleCommand) -> tuple[bool, str]:
    exe = sc.executable
    touches = exe in NETWORK_TOOLS or any(_URL_RE.search(a) for a in sc.argv[1:])
    if _is_install(sc):
        touches = True
    risk = "high" if _is_install(sc) else ("medium" if touches else "low")
    return touches, risk


def _looks_like_path(arg: str) -> bool:
    if _URL_RE.search(arg):  # URL 不是本地路径
        return False
    return (
        "/" in arg
        or "\\" in arg
        or ".." in arg
        or arg.startswith("~")
        or (len(arg) >= 2 and arg[1] == ":")  # Windows 盘符 C:
    )


def _find_escaping_paths(argv: List[str], sandbox: Path) -> List[str]:
    """扫 argv[1:]，返回解析后逃出 sandbox 的路径参数（跳过选项与 URL）。"""
    escapes: List[str] = []
    sandbox_root = sandbox.resolve()
    for arg in argv[1:]:
        if arg.startswith("-") or not _looks_like_path(arg):
            continue
        candidate = Path(arg)
        resolved = candidate.resolve() if candidate.is_absolute() else (sandbox_root / candidate).resolve()
        try:
            resolved.relative_to(sandbox_root)
        except ValueError:
            escapes.append(arg)
    return escapes


def _apply_path_guard(plan: CommandPlan) -> CommandPlan:
    """给已判 ok 的单命令补 path_escapes（闸门在 policy 层生效）。"""
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
