"""
文件管理工具模块

提供在 FILE_PATH 目录下读写执行文件的能力。
"""

import os
import asyncio
import platform
import subprocess
from typing import Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.resource import FILE_PATH

# Windows + SelectorEventLoop（见 core.py）不支持 asyncio 子进程；
# 与 command_executor.py 同源——见同名常量的注释。
_IS_WINDOWS = platform.system() == "Windows"


def _get_safe_path(base_path: Path, relative_path: str) -> Optional[Path]:
    """安全地获取路径，防止路径遍历攻击。

    v2 · Kanban：如果当前在任务执行上下文中（绑定了 artifact_workspace），路径会
    被强制解析到 Artifact Workspace 内，base_path 退化为兜底；否则保持原沙盒行为。
    """
    try:
        from gsuid_core.ai_core.planning.workspace import resolve_safe_path

        full_path, err = resolve_safe_path(relative_path, base_path)
        if err:
            return None
        return full_path
    except ImportError:
        # planning 模块尚未就绪（极早期启动），退回纯沙盒解析
        try:
            clean_path = os.path.normpath(relative_path)
            full_path = (base_path / clean_path).resolve()
            if not str(full_path).startswith(str(base_path.resolve())):
                return None
            return full_path
        except Exception:
            return None


@ai_tools(capability_domain="文件")
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
        logger.info(t("🧠 [BuildinTools] 读取文件成功: {file_path}", file_path=file_path))
        return content

    except UnicodeDecodeError:
        # 尝试用其他编码读取
        try:
            content = safe_path.read_text(encoding="gbk")
            return content
        except Exception:
            return f"错误：文件编码不支持，请确保文件为文本格式: {file_path}"
    except Exception as e:
        logger.exception(t("🧠 [BuildinTools] 读取文件失败: {e}", e=e))
        return f"错误：读取文件失败: {str(e)}"


@ai_tools(capability_domain="文件")
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
        # v2 · Kanban：越界写入直接登记 workspace_violation 事件供调度器统计
        await _record_workspace_violation(file_path, "write_file_content 越界拒绝")
        return f"错误：非法路径访问拒绝: {file_path}"

    try:
        # 检查文件是否存在
        if safe_path.exists() and not overwrite:
            return f"错误：文件已存在且 overwrite=False: {file_path}"

        # 确保父目录存在
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        safe_path.write_text(content, encoding="utf-8")
        logger.info(t("🧠 [BuildinTools] 写入文件成功: {file_path}", file_path=file_path))
        # v2 · Kanban：写入完成后立刻把新文件登记为 workspace_file artifact，
        # 让主人格 artifact_list / 看板工作区视图能立即看到中间代码。否则会回到
        # 实测会话 a5696b00 的状态：code_agent 写了 .py 文件但主人格只看到 .png，
        # 以为代理"没生成代码"。详见 §workspace 自动登记完整性章节。
        await _register_single_workspace_file(safe_path)
        return f"成功写入文件: {file_path}"

    except Exception as e:
        logger.exception(t("🧠 [BuildinTools] 写入文件失败: {e}", e=e))
        return f"错误：写入文件失败: {str(e)}"


async def _register_single_workspace_file(path: Path) -> None:
    """把单个 workspace 内文件登记为 workspace_file artifact（如未登记）。

    用于 ``write_file_content`` 写入完成后、``execute_file`` 执行前后扫描新增/修改文件。
    无任务上下文 / planning 未就绪时静默 no-op。同一份路径已被登记过时框架按
    payload_path 去重——多 artifact 共享一个 path 时 TTL 清理也按 path 去重。
    """
    try:
        from gsuid_core.ai_core.planning.runtime import get_plan_context
        from gsuid_core.ai_core.planning.workspace import register_workspace_artifacts

        plan_ctx = get_plan_context()
        if plan_ctx is None or plan_ctx.artifact_workspace is None or not plan_ctx.task_id:
            return
        if not path.exists() or not path.is_file():
            return
        # 确保路径在 workspace 内
        try:
            path.resolve().relative_to(plan_ctx.artifact_workspace.resolve())
        except ValueError:
            return
        size = path.stat().st_size
        await register_workspace_artifacts(
            root_task_id=plan_ctx.root_task_id,
            task_id=plan_ctx.task_id,
            workspace=plan_ctx.artifact_workspace,
            changes=[(path, size)],
            agent_profile=plan_ctx.agent_profile or "",
            parent_task_id=None,
        )
    except ImportError:
        return
    except Exception as e:
        logger.debug(t("🧠 [BuildinTools] workspace_file artifact 自动登记失败: {e}", e=e))


def _resolve_exec_cwd(fallback: Path) -> Path:
    """v2 · Kanban：处于任务执行上下文时，把 cwd 强制为 Artifact Workspace。

    2026-05-23 加固注释：``fallback`` 永远是 ``FILE_PATH``（``execute_file`` 入参），
    **绝不允许**被改成 ``Path.cwd()`` 或项目根——后者会让 code_agent 的脚本在主
    仓库根目录跑，污染框架自身。``capability_agents/runner._ensure_adhoc_workspace``
    保证 ``create_subagent`` 路径下也会绑定 ad-hoc workspace；这里的 fallback 只
    在"planning 模块未就绪 / fallback 路径仍能正常工作"的极早期场景生效。
    """
    try:
        from gsuid_core.ai_core.planning.runtime import get_plan_context

        plan_ctx = get_plan_context()
        if plan_ctx is not None and plan_ctx.artifact_workspace is not None:
            ws = plan_ctx.artifact_workspace
            ws.mkdir(parents=True, exist_ok=True)
            return ws
    except ImportError:
        pass
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


async def _record_workspace_violation(req_path: str, detail: str) -> None:
    """v2 · Kanban：若处于任务上下文，把越界拒绝写入任务日志（不在则静默）。"""
    try:
        from gsuid_core.ai_core.planning.runtime import get_plan_context
        from gsuid_core.ai_core.planning.workspace import record_violation

        plan_ctx = get_plan_context()
        if plan_ctx is None or not plan_ctx.task_id:
            return
        await record_violation(
            plan_ctx.task_id,
            f"{detail}: {req_path}",
            root_task_id=plan_ctx.root_task_id,
        )
    except ImportError:
        return


@ai_tools(capability_domain="文件")
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

        # v2 · Kanban：在任务执行上下文里把 cwd 强制为 Artifact Workspace，
        # 命令产出的所有文件都自动落在任务节点的工作区下；非任务上下文沿用 FILE_PATH。
        exec_cwd_path = _resolve_exec_cwd(FILE_PATH)
        exec_cwd = str(exec_cwd_path)

        logger.info(t("🧠 [BuildinTools] 执行文件: {p0} cwd={exec_cwd}", p0=" ".join(cmd), exec_cwd=exec_cwd))

        # 执行前快照 workspace（仅当 cwd 是任务的 workspace 时——非任务上下文跑
        # FILE_PATH 沙盒不登记 artifact）
        before_snapshot = None
        try:
            from gsuid_core.ai_core.planning.runtime import get_plan_context
            from gsuid_core.ai_core.planning.workspace import snapshot_workspace

            plan_ctx = get_plan_context()
            if plan_ctx is not None and plan_ctx.artifact_workspace is not None:
                ws = plan_ctx.artifact_workspace
                # 仅当 exec_cwd 就是当前任务的 workspace 时才扫描——避免把 FILE_PATH
                # 沙盒的产物错登记到任务 workspace（虽然两者通常一致）
                if str(exec_cwd_path.resolve()) == str(ws.resolve()):
                    before_snapshot = snapshot_workspace(ws)
        except ImportError:
            before_snapshot = None

        if _IS_WINDOWS:
            stdout, stderr, returncode = await _exec_file_in_thread(cmd, exec_cwd)
        else:
            stdout, stderr, returncode = await _exec_file_async(cmd, exec_cwd)

        # 执行后扫描 workspace 变更，登记新增 / 修改文件为 workspace_file artifact——
        # 让代理跑完脚本生成的所有产物自动出现在 artifact_list / 看板工作区视图。
        if before_snapshot is not None:
            try:
                from gsuid_core.ai_core.planning.runtime import get_plan_context
                from gsuid_core.ai_core.planning.workspace import (
                    scan_workspace_changes,
                    register_workspace_artifacts,
                )

                plan_ctx = get_plan_context()
                if plan_ctx is not None and plan_ctx.artifact_workspace is not None and plan_ctx.task_id:
                    changes = scan_workspace_changes(plan_ctx.artifact_workspace, before_snapshot)
                    if changes:
                        await register_workspace_artifacts(
                            root_task_id=plan_ctx.root_task_id,
                            task_id=plan_ctx.task_id,
                            workspace=plan_ctx.artifact_workspace,
                            changes=changes,
                            agent_profile=plan_ctx.agent_profile or "",
                            parent_task_id=None,
                        )
            except Exception as e:
                logger.debug(t("🧠 [BuildinTools] execute_file workspace 扫描失败: {e}", e=e))

        result_parts = []
        if stdout:
            result_parts.append(f"标准输出:\n{stdout.decode('utf-8', errors='replace')}")
        if stderr:
            result_parts.append(f"标准错误:\n{stderr.decode('utf-8', errors='replace')}")
        if returncode != 0:
            result_parts.append(f"退出码: {returncode}")

        result = "\n".join(result_parts) if result_parts else "命令执行完成，无输出"

        logger.info(t("🧠 [BuildinTools] 文件执行完成，退出码: {returncode}", returncode=returncode))
        return result

    except FileNotFoundError as e:
        return f"错误：执行器未找到，请确保系统已安装 Python 或相关执行环境: {str(e)}"
    except Exception as e:
        logger.exception(t("🧠 [BuildinTools] 执行文件失败: {e}", e=e))
        return f"错误：执行文件失败: {str(e)}"


async def _exec_file_async(cmd: list, cwd: str) -> tuple[bytes, bytes, int]:
    """POSIX 路径：原生 asyncio 子进程跑文件。"""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await process.communicate()
    return stdout, stderr, process.returncode or 0


async def _exec_file_in_thread(cmd: list, cwd: str) -> tuple[bytes, bytes, int]:
    """Windows 路径：见 command_executor._run_subprocess_in_thread 的成因说明。"""

    def _runner() -> tuple[bytes, bytes, int]:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            check=False,
            creationflags=creationflags,
        )
        return completed.stdout or b"", completed.stderr or b"", completed.returncode

    return await asyncio.to_thread(_runner)


@ai_tools(capability_domain="文件")
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
            logger.info(
                t(
                    "🧠 [BuildinTools] 文件差异对比完成: {file_path_1} vs {file_path_2}",
                    file_path_1=file_path_1,
                    file_path_2=file_path_2,
                )
            )
            return f"文件差异 ({file_path_1} → {file_path_2}):\n\n{diff_content}"
        else:
            return f"文件 {file_path_1} 和 {file_path_2} 内容相同"

    except Exception as e:
        logger.exception(t("🧠 [BuildinTools] 文件对比失败: {e}", e=e))
        return f"错误：文件对比失败: {str(e)}"


@ai_tools(capability_domain="文件")
async def list_directory(
    ctx: RunContext[ToolContext],
    dir_path: str = "",
) -> str:
    """
    列出目录内容

    列出当前可写沙盒下指定文件夹的内容。在 Kanban / ad-hoc 任务上下文里，
    根目录是该任务的 Artifact Workspace；其它情况下回退到 FILE_PATH 沙盒。

    Args:
        ctx: 工具执行上下文
        dir_path: 相对于沙盒根的目录路径，默认为空（列出根目录本身）

    Returns:
        目录内容列表

    Example:
        >>> result = await list_directory(ctx, "subfolder")
        >>> result = await list_directory(ctx)  # 列出沙盒根
    """
    # 空字符串 = "当前沙盒根"。resolve_safe_path 把空串当成非法请求会拒绝，
    # 这里替成 "." 让它解析到 workspace / FILE_PATH 本身。实测会话里 code_agent
    # 多次 list_directory() 都被拒，只能改用 execute_shell_command 绕一圈。
    safe_path = _get_safe_path(FILE_PATH, dir_path or ".")
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

        logger.info(t("🧠 [BuildinTools] 列出目录: {dir_path}", dir_path=dir_path))
        return f"目录 {dir_path or '/'} 内容:\n\n" + "\n".join(entries)

    except Exception as e:
        logger.exception(t("🧠 [BuildinTools] 列出目录失败: {e}", e=e))
        return f"错误：列出目录失败: {str(e)}"
