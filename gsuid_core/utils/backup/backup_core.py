import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Optional

from gsuid_core.logger import logger
from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.utils.plugins_config.gs_config import backup_config


def copy_and_rebase_paths(_paths_to_copy: Optional[List[Path]] = None) -> None:
    """
    将路径列表中的文件/文件夹复制到备份目录，并移除指定的路径前缀。

    :param paths_to_copy: 待复制的 Path 对象列表 (List[Path])。
    """
    if _paths_to_copy is None:
        paths_to_copy: List[Path] = [
            Path(p) for p in backup_config.get_config('backup_dir').data
        ]
    else:
        paths_to_copy = _paths_to_copy

    prefix_to_remove = gs_data_path
    date_str = datetime.now().strftime("%Y-%m-%d")

    final_backup_dir = backup_path / date_str

    try:
        final_backup_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"已确保备份目录存在: {final_backup_dir}")

    except Exception as e:
        logger.info(f"创建备份目录失败: {e}")
        return

    # 4. 遍历并复制路径
    for src_path in paths_to_copy:
        try:
            relative_path = src_path.relative_to(prefix_to_remove)

            dest_path = final_backup_dir / relative_path

            if src_path.is_file():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
                logger.success(
                    f"♻️ [早柚核心] 已复制文件: {src_path} -> {dest_path}"
                )

            elif src_path.is_dir():
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
                logger.success(
                    f"♻️ [早柚核心] 已复制目录: {src_path} -> {dest_path}"
                )

            else:
                logger.success(
                    f"♻️ [早柚核心] 跳过非文件/非目录路径: {src_path}"
                )

        except ValueError:
            logger.warning(
                f"♻️ [早柚核心] 路径 '{src_path}' 不包含前缀 '{prefix_to_remove}'，跳过。"
            )
        except Exception as e:
            logger.warning(f"♻️ [早柚核心] 复制 '{src_path}' 时发生错误: {e}")

    # 最后, 打zip压缩包
    try:
        shutil.make_archive(str(final_backup_dir), 'zip', final_backup_dir)
        logger.success(f"已压缩备份目录: {final_backup_dir}.zip")
    except Exception as e:
        logger.warning(f"压缩备份目录失败: {e}")
