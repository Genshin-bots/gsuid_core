"""
文件管理工具模块

提供在 FILE_PATH 目录下读写执行文件的能力。
"""

import os
import asyncio
import platform
from typing import Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.resource import FILE_PATH


def _get_safe_path(base_path: Path, relative_path: str) -> Optional[Path]:
    """安全地获取路径，防止路径遍历攻击

    Args:
        base_path: 基础路径 (FILE_PATH)
        relative_path: 用户提供的相对路径

    Returns:
        安全解析后的路径，如果路径不合法则返回 None
    """
    try:
        # 清理路径，去除多余的斜杠和点
        clean_path = os.path.normpath(relative_path)
        # 构建完整路径
        full_path = (base_path / clean_path).resolve()
        # 确保路径在 base_path 下，防止路径遍历
        if not str(full_path).startswith(str(base_path.resolve())):
            return None
        return full_path
    except Exception:
        return None


@ai_tools()
async def read_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,
) -> str:
    """
    读取文件内容

    读取 FILE_PATH 目录下的指定文件内容。只能读取文件，不能读取目录。

    Args:
        ctx: 工具执行上下文
        file_path: 相对于 FILE_PATH 的文件路径，例如 "subfolder/file.txt"

    Returns:
        文件内容字符串，读取失败时返回错误信息

    Example:
        >>> content = await read_file_content(ctx, "data/config.json")
    """
    if not file_path:
        return "错误：文件路径不能为空"

    safe_path = _get_safe_path(FILE_PATH, file_path)
    if safe_path is None:
        return f"错误：非法路径访问拒绝: {file_path}"

    try:
        if not safe_path.exists():
            return f"错误：文件不存在: {file_path}"

        if not safe_path.is_file():
            return f"错误：路径不是文件: {file_path}"

        content = safe_path.read_text(encoding="utf-8")
        logger.info(f"🧠 [BuildinTools] 读取文件成功: {file_path}")
        return content

    except UnicodeDecodeError:
        # 尝试用其他编码读取
        try:
            content = safe_path.read_text(encoding="gbk")
            return content
        except Exception:
            return f"错误：文件编码不支持，请确保文件为文本格式: {file_path}"
    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 读取文件失败: {e}")
        return f"错误：读取文件失败: {str(e)}"


@ai_tools()
async def write_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,
    content: str,
    overwrite: bool = True,
) -> str:
    """
    写入文件内容

    向 FILE_PATH 目录下指定文件写入内容。如果文件不存在则创建，如果存在且 overwrite=False 则返回错误。

    Args:
        ctx: 工具执行上下文
        file_path: 相对于 FILE_PATH 的文件路径，例如 "output/result.txt"
        content: 要写入的内容
        overwrite: 如果文件已存在是否覆盖，默认 True

    Returns:
        操作结果信息

    Example:
        >>> result = await write_file_content(ctx, "output/result.txt", "Hello World")
    """
    if not file_path:
        return "错误：文件路径不能为空"

    safe_path = _get_safe_path(FILE_PATH, file_path)
    if safe_path is None:
        return f"错误：非法路径访问拒绝: {file_path}"

    try:
        # 检查文件是否存在
        if safe_path.exists() and not overwrite:
            return f"错误：文件已存在且 overwrite=False: {file_path}"

        # 确保父目录存在
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        safe_path.write_text(content, encoding="utf-8")
        logger.info(f"🧠 [BuildinTools] 写入文件成功: {file_path}")
        return f"成功写入文件: {file_path}"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 写入文件失败: {e}")
        return f"错误：写入文件失败: {str(e)}"


@ai_tools()
async def execute_file(
    ctx: RunContext[ToolContext],
    file_path: str,
    args: Optional[str] = None,
) -> str:
    """
    执行文件

    执行 FILE_PATH 目录下的脚本文件（如 .py, .bat, .sh 等）。

    Args:
        ctx: 工具执行上下文
        file_path: 相对于 FILE_PATH 的文件路径，例如 "scripts/test.py"
        args: 可选的命令行参数，默认为空

    Returns:
        命令执行的标准输出和标准错误

    Example:
        >>> result = await execute_file(ctx, "scripts/test.py")
        >>> result = await execute_file(ctx, "scripts/test.py", "--help")
    """
    if not file_path:
        return "错误：文件路径不能为空"

    safe_path = _get_safe_path(FILE_PATH, file_path)
    if safe_path is None:
        return f"错误：非法路径访问拒绝: {file_path}"

    if not safe_path.exists():
        return f"错误：文件不存在: {file_path}"

    if not safe_path.is_file():
        return f"错误：路径不是文件: {file_path}"

    # 根据文件扩展名确定执行命令
    suffix = safe_path.suffix.lower()

    # 检测系统
    is_windows = platform.system() == "Windows"

    try:
        if suffix == ".py":
            cmd = ["python", str(safe_path)]
        elif suffix == ".pyw":
            cmd = ["pythonw", str(safe_path)]
        elif suffix == ".bat" or suffix == ".cmd":
            if is_windows:
                cmd = ["cmd", "/c", str(safe_path)]
            else:
                return "错误：.bat 文件只能在 Windows 系统上执行"
        elif suffix == ".sh":
            if is_windows:
                return "错误：.sh 文件需要在 Linux/Mac 系统上执行"
            else:
                cmd = ["bash", str(safe_path)]
        elif suffix == ".ps1":
            if is_windows:
                cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(safe_path)]
            else:
                return "错误：.ps1 文件只能在 Windows 系统上执行"
        else:
            # 尝试直接执行
            cmd = [str(safe_path)]

        # 添加参数
        if args:
            cmd.extend(args.split())

        logger.info(f"🧠 [BuildinTools] 执行文件: {' '.join(cmd)}")

        # 使用 asyncio 执行子进程
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(FILE_PATH),
        )

        stdout, stderr = await process.communicate()

        result_parts = []
        if stdout:
            result_parts.append(f"标准输出:\n{stdout.decode('utf-8', errors='replace')}")
        if stderr:
            result_parts.append(f"标准错误:\n{stderr.decode('utf-8', errors='replace')}")
        if process.returncode != 0:
            result_parts.append(f"退出码: {process.returncode}")

        result = "\n".join(result_parts) if result_parts else "命令执行完成，无输出"

        logger.info(f"🧠 [BuildinTools] 文件执行完成，退出码: {process.returncode}")
        return result

    except FileNotFoundError as e:
        return f"错误：执行器未找到，请确保系统已安装 Python 或相关执行环境: {str(e)}"
    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 执行文件失败: {e}")
        return f"错误：执行文件失败: {str(e)}"


@ai_tools()
async def diff_file_content(
    ctx: RunContext[ToolContext],
    file_path_1: str,
    file_path_2: str,
) -> str:
    """
    对比两个文件的差异

    比较 FILE_PATH 目录下两个文件的差异，返回差异详情。

    Args:
        ctx: 工具执行上下文
        file_path_1: 第一个文件的相对路径
        file_path_2: 第二个文件的相对路径

    Returns:
        两个文件的差异内容

    Example:
        >>> result = await diff_file_content(ctx, "old version.py", "new_version.py")
    """
    if not file_path_1 or not file_path_2:
        return "错误：两个文件路径都不能为空"

    safe_path_1 = _get_safe_path(FILE_PATH, file_path_1)
    safe_path_2 = _get_safe_path(FILE_PATH, file_path_2)

    if safe_path_1 is None:
        return f"错误：非法路径访问拒绝: {file_path_1}"
    if safe_path_2 is None:
        return f"错误：非法路径访问拒绝: {file_path_2}"

    try:
        if not safe_path_1.exists():
            return f"错误：第一个文件不存在: {file_path_1}"
        if not safe_path_2.exists():
            return f"错误：第二个文件不存在: {file_path_2}"

        content1 = safe_path_1.read_text(encoding="utf-8")
        content2 = safe_path_2.read_text(encoding="utf-8")

        if content1 == content2:
            return f"文件 {file_path_1} 和 {file_path_2} 内容相同，无差异"

        # 使用 difflib 生成差异
        import difflib

        diff = difflib.unified_diff(
            content1.splitlines(keepends=True),
            content2.splitlines(keepends=True),
            fromfile=f"a/{file_path_1}",
            tofile=f"b/{file_path_2}",
            lineterm="",
        )

        diff_content = "".join(diff)

        if diff_content:
            logger.info(f"🧠 [BuildinTools] 文件差异对比完成: {file_path_1} vs {file_path_2}")
            return f"文件差异 ({file_path_1} → {file_path_2}):\n\n{diff_content}"
        else:
            return f"文件 {file_path_1} 和 {file_path_2} 内容相同"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 文件对比失败: {e}")
        return f"错误：文件对比失败: {str(e)}"


@ai_tools()
async def list_directory(
    ctx: RunContext[ToolContext],
    dir_path: str = "",
) -> str:
    """
    列出目录内容

    列出 FILE_PATH 目录下指定文件夹的内容。

    Args:
        ctx: 工具执行上下文
        dir_path: 相对于 FILE_PATH 的目录路径，默认为空（列出根目录）

    Returns:
        目录内容列表

    Example:
        >>> result = await list_directory(ctx, "subfolder")
        >>> result = await list_directory(ctx)  # 列出根目录
    """
    safe_path = _get_safe_path(FILE_PATH, dir_path)
    if safe_path is None:
        return f"错误：非法路径访问拒绝: {dir_path}"

    try:
        if not safe_path.exists():
            return f"错误：目录不存在: {dir_path}"

        if not safe_path.is_dir():
            return f"错误：路径不是目录: {dir_path}"

        entries = []
        for entry in safe_path.iterdir():
            entry_type = "📁 目录" if entry.is_dir() else "📄 文件"
            entries.append(f"{entry_type}: {entry.name}")

        if not entries:
            return f"目录 {dir_path} 为空"

        logger.info(f"🧠 [BuildinTools] 列出目录: {dir_path}")
        return f"目录 {dir_path or '/'} 内容:\n\n" + "\n".join(entries)

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 列出目录失败: {e}")
        return f"错误：列出目录失败: {str(e)}"
