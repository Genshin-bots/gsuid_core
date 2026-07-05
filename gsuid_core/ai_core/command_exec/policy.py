"""闸门③：策略引擎（allow / deny / require_approval）。

吃 config + CommandPlan，决定放行 / 拒绝 / 转审批。元工具（解释器/包管理器/联网）
永远不走自动放行快速通道——见设计文档 §5.3。
"""

from enum import Enum

from gsuid_core.ai_core.command_exec.config import cfg_get
from gsuid_core.ai_core.command_exec.analyzer import CommandPlan


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL = "approval"


# 元工具名单（代码级硬编码,用户配置改不了）：具备二次解释 / 任意代码执行 / 外联能力,
# 一旦免审批放行 = 彻底绕过所有后续闸门。这些命令永远只走单次审批,拒绝「记住」。
NEVER_AUTO_ALLOW = {
    # 解释器 / eval —— 天生可执行任意代码
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
    # 参数即可 exec / 外联 / 写盘的「利器」
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
    policy = cfg_get("path_arg_policy")
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
    deny = {c.lower() for c in (cfg_get("deny_commands") or [])}
    allow = {c.lower() for c in (cfg_get("auto_allow_commands") or [])}

    if exe in deny:
        return Decision.DENY, f"'{exe}' 在永久黑名单中"

    if plan.touches_network and not cfg_get("allow_network"):
        return Decision.DENY, "该命令需要联网,但'允许联网命令'未开启"

    is_meta = exe in NEVER_AUTO_ALLOW
    net_needs_approval = plan.touches_network and cfg_get("require_approval_for_network")

    mode = cfg_get("approval_mode")
    if mode == "all":
        return Decision.APPROVAL, "当前为'全部审批'模式"
    # 元工具门 + 联网强制审批放在 allow 短路之前,堵死「元工具被 always-allow 后越权」。
    if is_meta:
        return Decision.APPROVAL, f"'{exe}' 是元工具(可二次执行/外联),强制单次审批"
    if net_needs_approval:
        return Decision.APPROVAL, "联网命令强制审批"
    if mode == "auto":
        return _guard_path(Decision.ALLOW, plan)
    # smart(部分审批)
    if exe in allow:
        return _guard_path(Decision.ALLOW, plan)
    return Decision.APPROVAL, f"'{exe}' 不在自动放行白名单,需主人审批"
