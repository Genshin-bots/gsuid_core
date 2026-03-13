import shutil
from typing import List, Optional
from pathlib import Path
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.data_store import backup_path, gs_data_path
from gsuid_core.utils.plugins_config.gs_config import backup_config


def copy_and_rebase_paths(_paths_to_copy: Optional[List[Path]] = None, file_id: Optional[str] = None) -> int:
    """
    将路径列表中的文件/文件夹复制到备份目录，并移除指定的路径前缀。

    :param paths_to_copy: 待复制的 Path 对象列表 (List[Path])。
    """
    if _paths_to_copy is None:
        # 获取配置中的路径，并确保它们是相对于gs_data_path的完整路径
        config_paths = backup_config.get_config("backup_dir").data
        paths_to_copy: List[Path] = []
        for p in config_paths:
            path = Path(p)
            # 如果路径不是绝对路径，或者不是以gs_data_path开头的，就拼接上gs_data_path
            if not path.is_absolute() or not path.is_relative_to(gs_data_path):
                path = gs_data_path / path
            paths_to_copy.append(path)
    else:
        paths_to_copy = _paths_to_copy

    prefix_to_remove = gs_data_path

    date_str = datetime.now().strftime("%Y-%m-%d")
    if file_id is None:
        file_id = date_str
    else:
        file_id = file_id.strip()

    final_backup_dir = backup_path / f"{file_id}-{date_str}"

    if final_backup_dir.exists():
        logger.warning(f"备份目录已存在: {final_backup_dir}")
        # 确认一下这个目录是否是backup_path开头的
        if not final_backup_dir.is_relative_to(backup_path):
            logger.warning(f"目录 {final_backup_dir} 不是 {backup_path} 的子目录，跳过删除。")
            return -1

        # 递归删除该目录下的所有文件和子目录
        shutil.rmtree(final_backup_dir)

    try:
        final_backup_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"已确保备份目录存在: {final_backup_dir}")

    except Exception as e:
        logger.info(f"创建备份目录失败: {e}")
        return -5

    # 4. 遍历并复制路径
    for src_path in paths_to_copy:
        try:
            relative_path = src_path.relative_to(prefix_to_remove)

            dest_path = final_backup_dir / relative_path

            if src_path.is_file():
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
                logger.success(f"♻️ [早柚核心] 已复制文件: {src_path} -> {dest_path}")

            elif src_path.is_dir():
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
                logger.success(f"♻️ [早柚核心] 已复制目录: {src_path} -> {dest_path}")

            else:
                logger.success(f"♻️ [早柚核心] 跳过非文件/非目录路径: {src_path}")

        except ValueError:
            logger.warning(f"♻️ [早柚核心] 路径 '{src_path}' 不包含前缀 '{prefix_to_remove}'，跳过。")
        except Exception as e:
            logger.warning(f"♻️ [早柚核心] 复制 '{src_path}' 时发生错误: {e}")

    # 最后, 打zip压缩包
    try:
        shutil.make_archive(str(final_backup_dir), "zip", final_backup_dir)
        logger.success(f"已压缩备份目录: {final_backup_dir}.zip")
    except Exception as e:
        logger.warning(f"压缩备份目录失败: {e}")
        return -10

    return 0


def remove_old_backups(days: int = 30) -> int:
    """
    删除超过指定天数的备份文件或目录。

    :param days: 保留的天数，默认为30天。
    :return: 被删除的文件/目录数量。
    """
    if not backup_path.exists():
        logger.warning(f"备份目录不存在: {backup_path}，跳过清理。")
        return 0

    current_time = datetime.now()
    deleted_count = 0

    logger.info(f"开始清理超过 {days} 天的备份文件...")

    # 遍历备份目录下的所有项目
    for item in backup_path.iterdir():
        # 获取文件名（不含扩展名），例如 'mydata-2023-11-19'
        # item.stem 会自动去掉 .zip 后缀
        name_stem = item.stem

        # 尝试提取日期部分
        # 你的命名格式是: f'{file_id}-{date_str}'，date_str 是 "YYYY-MM-DD" (10个字符)
        # 所以我们取 stem 的最后 10 位
        if len(name_stem) < 10:
            continue

        date_str_part = name_stem[-10:]

        try:
            # 尝试将后缀解析为日期
            backup_date = datetime.strptime(date_str_part, "%Y-%m-%d")
        except ValueError:
            # 如果解析失败（说明不是符合该日期格式的文件），则跳过
            continue

        # 计算时间差
        time_delta = current_time - backup_date

        if time_delta.days > days:
            try:
                if item.is_file():
                    item.unlink()  # 删除文件 (通常是 .zip)
                    logger.info(f"🗑️ [早柚核心] 已删除过期备份文件: {item.name} ({time_delta.days}天前)")
                elif item.is_dir():
                    shutil.rmtree(item)  # 删除目录 (如果存在未压缩的残留目录)
                    logger.info(f"🗑️ [早柚核心] 已删除过期备份目录: {item.name} ({time_delta.days}天前)")

                deleted_count += 1
            except Exception as e:
                logger.warning(f"❌ 删除 {item.name} 失败: {e}")

    if deleted_count > 0:
        logger.success(f"✅ 清理完成，共删除 {deleted_count} 个过期备份。")
    else:
        logger.info("✨ 没有发现需要删除的过期备份。")

    return deleted_count
