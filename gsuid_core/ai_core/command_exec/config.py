"""命令执行器独立配置（StringConfig · 热重载）。

单 JSON 文件、类型化条目、写入即落盘、WebConsole 自动渲染、下次消息处理即生效。
主人在控制台改档位 / 白名单，无需重启（对齐 SKILL §03）。
"""

from typing import Any

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsDictConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

COMMAND_EXEC_CONFIG: dict[str, GSC] = {
    "enable": GsBoolConfig(
        "启用命令执行器",
        "总开关；关闭后 run_command 直接拒绝",
        False,
    ),
    "_AuthDivider": GsDivider(
        "身份与授权",
        "谁能触发",
        "身份与授权",
    ),
    "operator_user_ids": GsListStrConfig(
        "额外授权用户ID",
        "主人(masters)之外、额外允许触发命令执行器的 user_id 列表；留空=仅主人",
        [],
    ),
    "_ApprovalDivider": GsDivider(
        "审批策略",
        "命令放行档位",
        "审批策略",
    ),
    "approval_mode": GsStrConfig(
        "审批模式",
        "all=全部命令都需主人审批; smart=白名单自动/黑名单拒绝/其余审批; "
        "auto=仅黑名单拒绝其余自动(高风险,仅信任的单人服务器)",
        "smart",
        options=["all", "smart", "auto"],
    ),
    "auto_allow_commands": GsListStrConfig(
        "自动放行命令白名单",
        "smart/auto 模式下免审批直接执行的可执行文件——只应放只读/信息类命令。"
        "解释器/包管理器/联网工具(python/node/npm/git/curl...)即便写进这里也不会走快速通道:"
        "它们在 NEVER_AUTO_ALLOW 元工具名单里(代码级),会被强制转单次审批,防越权后门。",
        [
            "ls",
            "cat",
            "echo",
            "pwd",
            "whoami",
            "which",
            "where",
            "head",
            "tail",
            "wc",
            "date",
            "uname",
            "hostname",
            "df",
            "free",
        ],
    ),
    "deny_commands": GsListStrConfig(
        "永久拒绝命令黑名单",
        "这些可执行文件在任何模式下都拒绝(即便主人审批也不放行)",
        [
            "rm",
            "rmdir",
            "del",
            "mkfs",
            "dd",
            "fdisk",
            "parted",
            "format",
            "shutdown",
            "reboot",
            "poweroff",
            "sudo",
            "su",
            "chmod",
            "chown",
            "kill",
            "killall",
            "taskkill",
            "nc",
            "ncat",
            "netcat",
            "iptables",
            "reg",
            "diskpart",
        ],
    ),
    "path_arg_policy": GsStrConfig(
        "参数路径越界策略",
        "自动放行的命令若参数含逃出沙盒的路径(绝对路径/../): approval=转单次审批(默认); "
        "deny=直接拒; off=不检查。仅在'本要免审批'时触发,不影响审批/黑名单档。",
        "approval",
        options=["approval", "deny", "off"],
    ),
    "_NetworkDivider": GsDivider(
        "网络与下载",
        "外联控制",
        "网络与下载",
    ),
    "allow_network": GsBoolConfig(
        "允许联网命令",
        "curl/wget/git clone/npm install/pip install 等外联操作",
        True,
    ),
    "require_approval_for_network": GsBoolConfig(
        "联网命令强制审批",
        "即便 allow_network 开启,联网命令仍逐次审批",
        True,
    ),
    "allow_auto_provision": GsBoolConfig(
        "允许自动安装缺失工具",
        "缺 npm/node 等时尝试装到受管工具链目录(高风险,默认关)",
        False,
    ),
    "provision_mirror_urls": GsDictConfig(
        "自动安装镜像源",
        "工具→[下载基址] 映射(值为单元素列表,受 GsDictConfig 类型约束);把配方里的官方源换成"
        "国内镜像(如 node→['https://npmmirror.com/mirrors/node'])。sha256 仍取自官方配方,"
        "对得上就安全。留空=用配方内置官方源。只填已知镜像白名单基址,不接受任意 URL。",
        {},
    ),
    "_LimitDivider": GsDivider(
        "执行限制",
        "资源与沙盒",
        "执行限制",
    ),
    "default_timeout": GsIntConfig(
        "默认超时(秒)",
        "单条命令默认超时",
        60,
    ),
    "max_timeout": GsIntConfig(
        "最大超时(秒)",
        "允许的最大超时上限",
        600,
    ),
    "max_output_bytes": GsIntConfig(
        "最大输出字节",
        "stdout+stderr 合并输出上限",
        1024 * 1024,
    ),
    "sandbox_dir": GsStrConfig(
        "沙盒工作目录",
        "命令默认 cwd;留空=data/ai_core/file",
        "",
    ),
    "approval_ttl_seconds": GsIntConfig(
        "审批有效期(秒)",
        "pending 请求超时自动作废",
        1800,
    ),
    "_AuditDivider": GsDivider(
        "审计",
        "留痕",
        "审计",
    ),
    "audit_enabled": GsBoolConfig(
        "启用审计",
        "每次决策+执行落库",
        True,
    ),
    "audit_ttl_days": GsIntConfig(
        "审计保留天数",
        "每日清理早于该天数的低风险审计;0=永不清理;高危/补全永久留存",
        30,
    ),
    "notify_master_on_exec": GsBoolConfig(
        "执行后通知主人",
        "自动放行的命令执行后也私信主人一条摘要",
        False,
    ),
}

command_exec_config = StringConfig(
    "GsCore AI 命令执行器配置",
    get_res_path("ai_core") / "command_exec_config.json",
    COMMAND_EXEC_CONFIG,
)


def cfg_get(key: str) -> Any:
    """读某配置项的 data；避免各处硬编码 key、每次现读以支持热重载。"""
    return command_exec_config.get_config(key).data
