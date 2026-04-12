"""
命令执行工具模块

提供在服务器上执行系统命令的能力。
"""

import re
import shlex
import asyncio
import platform
from typing import Set, Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.check_func import check_pm

# 允许执行的安全命令白名单（基础命令）
ALLOWED_COMMANDS: Set[str] = {
    # 文件和目录操作
    "ls",
    "dir",
    "pwd",
    "cd",
    "cat",
    "type",
    "head",
    "tail",
    "less",
    "more",
    "find",
    "grep",
    "wc",
    "sort",
    "uniq",
    "diff",
    "file",
    # 系统信息
    "ps",
    "top",
    "htop",
    "df",
    "du",
    "free",
    "uptime",
    "whoami",
    "id",
    "uname",
    "hostname",
    "date",
    "cal",
    "env",
    "printenv",
    # 网络
    "ping",
    "curl",
    "wget",
    "netstat",
    "ss",
    "ifconfig",
    "ip",
    "nslookup",
    "dig",
    "host",
    "traceroute",
    "tracepath",
    # Python 相关
    "python",
    "python3",
    "pip",
    "pip3",
    "pdm",
    "poetry",
    "pytest",
    # 版本控制
    "git",
    "gitk",
    # 压缩
    "tar",
    "zip",
    "unzip",
    "gzip",
    "gunzip",
    # 其他常用工具
    "echo",
    "printf",
    "which",
    "whereis",
    "man",
    "help",
    "clear",
    "reset",
    "history",
    "alias",
    "export",
    # Windows 特定
    "chdir",
    "dir",
    "ver",
    "systeminfo",
    "tasklist",
    "taskkill",
    "ipconfig",
    "tracert",
    "nslookup",
    "net",
    "sc",
}

# 危险命令和模式黑名单
DANGEROUS_PATTERNS = [
    # 文件删除和格式化
    r"rm\s+-[rf]*[rf]",  # rm -rf, rm -r -f 等变体
    r"mkfs\.?\w*",  # mkfs, mkfs.ext4 等
    r"dd\s+if=",
    r"fdisk",
    r"parted",
    r"format\s+",
    # 系统破坏
    r":\s*\(\s*\)\s*\{\s*:\s*\|:\s*&\s*\};\s*:",  # fork bomb
    r">\s*/dev/[sh]da",
    r">\s+/dev/null",
    r"\$\(\s*:\s*\)\s*\{\s*:\s*\|:\s*&\s*\}",  # 另一种 fork bomb
    # 权限提升和敏感操作
    r"sudo\s+",
    r"su\s+-",
    r"chmod\s+.*777",
    r"chmod\s+.*755\s+/",
    r"chown\s+-R\s+root",
    # 代码执行和注入
    r"\$\(.*\)",  # 命令替换 $()
    r"`.*`",  # 反引号命令替换
    r"\|\s*bash",
    r"\|\s*sh\s",
    r"\|\s*python",
    r"eval\s*\$",
    r"exec\s*\$",
    # 重定向和管道风险
    r"\|\s*rm",
    r">\s*~/.\w+",  # 写入 home 目录配置文件
    r">\s*/etc/",
    r">\s*/var/",
    r">\s*/usr/",
    r">\s*/bin/",
    r">\s*/sbin/",
    r">\s*/lib",
    r"&\s*>",  # 后台重定向
    # 网络风险
    r"nc\s+-[l]*[lp]",  # netcat 监听模式
    r"ncat\s+-[l]*[lp]",
    r"nmap\s+-.*-s[SPV]",
    # 信息泄露
    r"cat\s+/etc/(passwd|shadow|ssh)",
    r"cat\s+.*\.env",
    r"cat\s+.*config\.py",
    r"cat\s+.*secret",
    r"cat\s+.*key",
    # 特殊字符注入
    r"[;&|]\s*rm",
    r"[;&|]\s*mkfs",
    r"[;&|]\s*dd",
    r"[;&|]\s*format",
]

# 编译后的正则表达式以提高性能
_DANGEROUS_REGEX = [re.compile(pattern, re.IGNORECASE) for pattern in DANGEROUS_PATTERNS]

# 最大输出限制 (1MB)
MAX_OUTPUT_SIZE = 1024 * 1024

# 默认工作目录（限制命令执行范围）
DEFAULT_WORK_DIR: Optional[Path] = None


def _is_dangerous_command(command: str) -> tuple[bool, str]:
    """
    检查命令是否包含危险模式

    Returns:
        (是否危险, 原因)
    """
    # 检查危险正则模式
    for pattern in _DANGEROUS_REGEX:
        if pattern.search(command):
            return True, f"检测到危险模式: {pattern.pattern[:50]}..."

    # 检查命令是否在白名单中
    cmd_parts = shlex.split(command)
    if not cmd_parts:
        return True, "命令解析失败"

    base_cmd = cmd_parts[0]
    # 处理路径形式的命令，如 /usr/bin/ls 或 ./script.py
    base_cmd_name = Path(base_cmd).name.lower()

    # 检查是否使用绝对路径执行非白名单命令
    if base_cmd.startswith(("/", "./", "../", "~")):
        if base_cmd_name not in ALLOWED_COMMANDS:
            return True, f"命令 '{base_cmd_name}' 不在允许的白名单中"
    elif base_cmd.lower() not in ALLOWED_COMMANDS:
        return True, f"命令 '{base_cmd}' 不在允许的白名单中"

    return False, ""


def _sanitize_command(command: str) -> str:
    """
    清理和规范化命令字符串
    """
    # 移除空字符和控制字符（除了常见的换行、制表符）
    sanitized = "".join(char for char in command if char.isprintable() or char in "\t\n")

    # 移除 Unicode 方向控制字符（用于视觉欺骗攻击）
    # U+202A to U+202E: LRE, RLE, PDF, LRO, RLO
    # U+2066 to U+2069: LRI, RLI, FSI, PDI
    direction_controls = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    for char in direction_controls:
        sanitized = sanitized.replace(char, "")

    return sanitized.strip()


def _get_safe_environment() -> dict:
    """
    获取清理后的环境变量，移除敏感信息
    """
    import os

    # 允许的环境变量白名单
    allowed_env_prefixes = (
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_",
        "TERM",
        "SHELL",
        "PWD",
        "OLDPWD",
        "LOGNAME",
        "HOSTNAME",
        "XDG_",
        "PYTHON",
        "PYENV",
        "VIRTUAL_ENV",
        "POETRY",
        "PDM_",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "WINDIR",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    )

    # 敏感环境变量黑名单（不应传递给子进程）
    sensitive_vars = {
        "TOKEN",
        "API_KEY",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "PWD_AUTH",
        "PRIVATE_KEY",
        "SSH_KEY",
        "AWS_ACCESS",
        "AZURE",
        "GCP_",
        "DATABASE_URL",
        "DB_PASSWORD",
        "REDIS_URL",
        "MONGO_URL",
        "COOKIE",
        "SESSION",
        "AUTH",
        "CREDENTIAL",
        "CREDIT_CARD",
    }

    safe_env = {}
    for key, value in os.environ.items():
        # 检查是否在白名单前缀中
        if any(key.upper().startswith(prefix) for prefix in allowed_env_prefixes):
            # 额外检查是否包含敏感信息
            if not any(sensitive in key.upper() for sensitive in sensitive_vars):
                safe_env[key] = value

    # 确保基本环境变量存在
    if "PATH" not in safe_env:
        safe_env["PATH"] = (
            "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            if platform.system() != "Windows"
            else "C:\\Windows\\System32;C:\\Windows;C:\\Windows\\System32\\Wbem"
        )

    if "HOME" not in safe_env and hasattr(Path, "home"):
        try:
            safe_env["HOME"] = str(Path.home())
        except Exception:
            safe_env["HOME"] = "/tmp" if platform.system() != "Windows" else "C:\\Temp"

    if "LANG" not in safe_env:
        safe_env["LANG"] = "en_US.UTF-8"

    if "TERM" not in safe_env:
        safe_env["TERM"] = "xterm-256color"

    return safe_env


@ai_tools(check_func=check_pm)
async def execute_shell_command(
    ctx: RunContext[ToolContext],
    command: str,
    timeout: int = 30,
    use_shlex: bool = True,
    work_dir: Optional[str] = None,
    max_output: int = MAX_OUTPUT_SIZE,
) -> str:
    """
    执行系统命令（安全增强版）

    在服务器上执行指定的Shell命令，返回命令输出结果。
    注意：此工具权限较高，会验证使用者是否是管理员。
    实施了多层安全防护：命令白名单、危险模式检测、输出限制、环境隔离。

    Args:
        ctx: 工具执行上下文
        command: 要执行的命令，如"ls -la"或"python script.py"
        timeout: 命令执行超时时间（秒），默认30秒，最大300秒
        use_shlex: 是否使用shlex分割命令，默认True（强烈建议保持True）
        work_dir: 命令执行的工作目录，默认项目根目录
        max_output: 最大输出字节数，默认1MB

    Returns:
        命令执行结果字符串，包含 stdout 和 stderr 的输出

    Raises:
        TimeoutError: 命令执行超时
        RuntimeError: 命令执行失败

    Example:
        >>> result = await execute_shell_command(ctx, "ls -la /tmp")
        >>> print(result)
        >>> result = await execute_shell_command(ctx, "python bot.py --status", timeout=60)
    """
    # 参数验证
    if not command or not command.strip():
        return "执行失败：命令为空"

    # 限制超时时间
    timeout = max(1, min(timeout, 300))  # 限制在 1-300 秒之间

    # 限制输出大小
    max_output = max(1024, min(max_output, MAX_OUTPUT_SIZE))  # 限制在 1KB-1MB 之间

    # 清理命令（移除控制字符和视觉欺骗字符）
    command = _sanitize_command(command)

    if not command:
        return "执行失败：命令清理后为空"

    # 危险命令检测
    is_dangerous, reason = _is_dangerous_command(command)
    if is_dangerous:
        logger.warning(f"🧠 [BuildinTools] 拒绝执行危险命令: {command[:100]}... 原因: {reason}")
        return f"执行失败：{reason}"

    # 强制使用 shlex（移除 use_shlex=False 的选项以提高安全性）
    try:
        cmd_list = shlex.split(command)
    except ValueError as e:
        logger.warning(f"🧠 [BuildinTools] 命令解析失败: {e}")
        return f"执行失败：命令解析错误 - {str(e)}"

    if not cmd_list:
        return "执行失败：命令解析结果为空"

    # 确定工作目录
    if work_dir:
        try:
            work_path = Path(work_dir).resolve()
            # 确保工作目录存在且不是敏感目录
            if not work_path.exists():
                return f"执行失败：工作目录不存在: {work_dir}"
            # 禁止在系统关键目录执行
            sensitive_paths = [
                "/etc",
                "/bin",
                "/sbin",
                "/lib",
                "/usr/bin",
                "/usr/sbin",
                "/sys",
                "/proc",
                "C:\\Windows\\System32",
            ]
            for sensitive in sensitive_paths:
                if str(work_path).startswith(sensitive):
                    return f"执行失败：禁止在系统目录执行命令: {work_dir}"
        except Exception as e:
            return f"执行失败：工作目录解析错误 - {str(e)}"
    else:
        work_path = Path.cwd()

    logger.info(f"🧠 [BuildinTools] 执行命令: {command[:100]}... 工作目录: {work_path}")

    try:
        # 使用清理后的环境变量
        env = _get_safe_environment()

        process = await asyncio.create_subprocess_exec(
            *cmd_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(work_path),
            env=env,
        )

        try:
            # 使用 communicate 但限制输出大小
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)

            # 截断过大的输出
            if len(stdout) > max_output:
                stdout = stdout[:max_output]
                truncated_msg = f"\n\n[输出已截断，超过最大限制 {max_output} 字节]"
            else:
                truncated_msg = ""

            output = stdout.decode("utf-8", errors="replace").strip()

            # 添加截断提示
            if truncated_msg:
                output += truncated_msg

            if process.returncode == 0:
                logger.info(f"🧠 [BuildinTools] 命令执行成功 (返回码: 0, 输出长度: {len(stdout)})")
                return output if output else "命令执行成功，无输出"
            else:
                logger.warning(f"🧠 [BuildinTools] 命令执行完成 (返回码: {process.returncode})")
                return (
                    f"[返回码: {process.returncode}]\n{output}"
                    if output
                    else f"命令执行完成 (返回码: {process.returncode})"
                )

        except asyncio.TimeoutError:
            # 更彻底的清理
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                # 如果 kill 后 5 秒还没结束，强制 terminate
                try:
                    process.terminate()
                except Exception as e:
                    logger.error(f"🧠 [BuildinTools] 强制终止失败: {e}")
                    pass

            logger.warning(f"🧠 [BuildinTools] 命令执行超时: {timeout}秒")
            return f"执行失败：命令超时 (超过 {timeout} 秒)"

    except FileNotFoundError:
        logger.warning(f"🧠 [BuildinTools] 命令未找到: {cmd_list[0]}")
        return f"执行失败：命令未找到 '{cmd_list[0]}'"
    except PermissionError:
        logger.warning(f"🧠 [BuildinTools] 权限不足: {command[:100]}")
        return "执行失败：权限不足"
    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 命令执行异常: {e}")
        return f"执行失败：{type(e).__name__}: {str(e)}"
