"""
文件操作工具模块

提供在 ``data/ai_core/artifacts`` 目录内的文件移动、复制和打包成 zip 的能力。
所有操作严格限制在 artifacts 路径内，不允许路径遍历。

注意：本模块不提供删除功能；移动操作不允许覆盖已有文件。
"""

import os
import shutil
import zipfile
from typing import List, Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.planning.workspace import ARTIFACT_ROOT, _is_inside

# ---------------------------------------------------------------------------
# 内部安全校验
# ---------------------------------------------------------------------------


def _safe_resolve(base: Path, relative: str) -> Optional[Path]:
    """将 *relative* 解析为 *base* 下的绝对路径，防止路径遍历。

    返回 ``None`` 表示拒绝（越界 / 非法路径）。
    """
    try:
        cleaned = os.path.normpath(relative)
        full = (base / cleaned).resolve()
        if _is_inside(full, base):
            return full
        return None
    except Exception:
        return None


def _format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的字符串。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


@ai_tools(category="default")
async def move_file(
    ctx: RunContext[ToolContext],
    source_path: str,
    destination_path: str,
) -> str:
    """
    移动文件

    在 data/ai_core/artifacts 目录内将文件从一个位置移动到另一个位置。
    如果目标路径已存在文件或目录，操作将被拒绝（不允许覆盖）。

    Args:
        ctx: 工具执行上下文
        source_path: 源文件的相对路径（相对于 artifacts 根目录）
        destination_path: 目标位置的相对路径（相对于 artifacts 根目录）

    Returns:
        操作结果信息

    Example:
        >>> result = await move_file(ctx, "task1/workspace/report.txt", "task2/workspace/report.txt")
    """
    if not source_path or not destination_path:
        return "错误：源路径和目标路径都不能为空"

    safe_src = _safe_resolve(ARTIFACT_ROOT, source_path)
    safe_dst = _safe_resolve(ARTIFACT_ROOT, destination_path)

    if safe_src is None:
        return f"错误：非法源路径访问拒绝: {source_path}"
    if safe_dst is None:
        return f"错误：非法目标路径访问拒绝: {destination_path}"

    try:
        if not safe_src.exists():
            return f"错误：源文件不存在: {source_path}"

        if not safe_src.is_file():
            return f"错误：源路径不是文件（不支持移动目录）: {source_path}"

        if safe_dst.exists():
            return f"错误：目标路径已存在，不允许覆盖: {destination_path}"

        # 确保目标父目录存在
        safe_dst.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(str(safe_src), str(safe_dst))
        size = safe_dst.stat().st_size
        logger.info(f"🧠 [BuildinTools] 文件移动成功: {source_path} → {destination_path} ({_format_size(size)})")
        return f"文件移动成功: {source_path} → {destination_path} ({_format_size(size)})"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 文件移动失败: {e}")
        return f"错误：文件移动失败: {str(e)}"


@ai_tools(category="default")
async def copy_file(
    ctx: RunContext[ToolContext],
    source_path: str,
    destination_path: str,
    overwrite: bool = False,
) -> str:
    """
    复制文件

    在 data/ai_core/artifacts 目录内将文件从一个位置复制到另一个位置。

    Args:
        ctx: 工具执行上下文
        source_path: 源文件的相对路径（相对于 artifacts 根目录）
        destination_path: 目标位置的相对路径（相对于 artifacts 根目录）
        overwrite: 如果目标已存在是否覆盖，默认 False

    Returns:
        操作结果信息

    Example:
        >>> result = await copy_file(ctx, "task1/workspace/data.csv", "task1/workspace/data_backup.csv")
    """
    if not source_path or not destination_path:
        return "错误：源路径和目标路径都不能为空"

    safe_src = _safe_resolve(ARTIFACT_ROOT, source_path)
    safe_dst = _safe_resolve(ARTIFACT_ROOT, destination_path)

    if safe_src is None:
        return f"错误：非法源路径访问拒绝: {source_path}"
    if safe_dst is None:
        return f"错误：非法目标路径访问拒绝: {destination_path}"

    try:
        if not safe_src.exists():
            return f"错误：源文件不存在: {source_path}"

        if not safe_src.is_file():
            return f"错误：源路径不是文件: {source_path}"

        if safe_dst.exists() and not overwrite:
            return f"错误：目标路径已存在且 overwrite=False: {destination_path}"

        # 确保目标父目录存在
        safe_dst.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(str(safe_src), str(safe_dst))
        size = safe_dst.stat().st_size
        logger.info(f"🧠 [BuildinTools] 文件复制成功: {source_path} → {destination_path} ({_format_size(size)})")
        return f"文件复制成功: {source_path} → {destination_path} ({_format_size(size)})"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] 文件复制失败: {e}")
        return f"错误：文件复制失败: {str(e)}"


@ai_tools(category="default")
async def pack_to_zip(
    ctx: RunContext[ToolContext],
    source_paths: str,
    zip_path: str,
) -> str:
    """
    打包文件为 zip

    将 data/ai_core/artifacts 目录内的一个或多个文件/目录打包成 zip 压缩文件。
    多个源路径用英文逗号分隔。

    Args:
        ctx: 工具执行上下文
        source_paths: 要打包的源文件/目录的相对路径，多个用逗号分隔。
                      例如 "task1/workspace/file1.txt,task1/workspace/file2.txt"
        zip_path: 输出 zip 文件的相对路径（相对于 artifacts 根目录）
                  例如 "task1/workspace/output.zip"

    Returns:
        操作结果信息，包含打包的文件数和压缩包大小

    Example:
        >>> result = await pack_to_zip(
        ...     ctx, "task1/workspace/report.txt,task1/workspace/data.csv", "task1/workspace/archive.zip"
        ... )
        >>> result = await pack_to_zip(ctx, "task1/workspace/", "task1/workspace/project.zip")
    """
    if not source_paths or not zip_path:
        return "错误：源路径和 zip 输出路径都不能为空"

    safe_zip = _safe_resolve(ARTIFACT_ROOT, zip_path)
    if safe_zip is None:
        return f"错误：非法 zip 输出路径访问拒绝: {zip_path}"

    if safe_zip.exists():
        return f"错误：zip 文件已存在，请使用不同的路径: {zip_path}"

    # 解析多个源路径
    raw_paths = [p.strip() for p in source_paths.split(",") if p.strip()]
    if not raw_paths:
        return "错误：未提供有效的源路径"

    resolved_sources: List[Path] = []
    for rp in raw_paths:
        sp = _safe_resolve(ARTIFACT_ROOT, rp)
        if sp is None:
            return f"错误：非法源路径访问拒绝: {rp}"
        if not sp.exists():
            return f"错误：源路径不存在: {rp}"
        resolved_sources.append(sp)

    try:
        # 确保目标父目录存在
        safe_zip.parent.mkdir(parents=True, exist_ok=True)

        file_count = 0
        with zipfile.ZipFile(str(safe_zip), "w", zipfile.ZIP_DEFLATED) as zf:
            for src in resolved_sources:
                if src.is_file():
                    # 单个文件：以文件名作为 zip 内路径
                    zf.write(str(src), src.name)
                    file_count += 1
                elif src.is_dir():
                    # 目录：递归添加，保持相对结构
                    for root, dirs, files in os.walk(str(src)):
                        for fname in files:
                            file_path = Path(root) / fname
                            arcname = file_path.relative_to(src.parent)
                            zf.write(str(file_path), str(arcname))
                            file_count += 1

        zip_size = safe_zip.stat().st_size
        logger.info(f"🧠 [BuildinTools] zip 打包成功: {zip_path} (共 {file_count} 个文件, {_format_size(zip_size)})")
        return f"zip 打包成功: {zip_path}\n包含文件数: {file_count}\n压缩包大小: {_format_size(zip_size)}"

    except Exception as e:
        logger.exception(f"🧠 [BuildinTools] zip 打包失败: {e}")
        # 清理可能不完整的 zip 文件
        if safe_zip.exists():
            try:
                safe_zip.unlink()
            except Exception:
                pass
        return f"错误：zip 打包失败: {str(e)}"
