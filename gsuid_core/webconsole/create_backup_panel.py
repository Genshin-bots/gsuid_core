from __future__ import annotations

from typing import Dict, List
from pathlib import Path

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import backup_config

from .models import Option
from .create_base_panel import (
    get_api,
    get_tab,
    get_form,
    get_page,
    get_tabs,
    get_alert,
    get_button,
    get_checkboxes,
    get_input_tree,
    get_time_select,
    get_list_download,
)


def path_to_option(path_obj: Path) -> Option:
    """
    递归地将一个 pathlib.Path 对象转换为 Option 字典，
    并确保文件夹始终置顶。
    """
    option: Option = {
        "label": path_obj.name,
        "value": str(path_obj.absolute()),  # 使用 .absolute() 确保路径完整
    }

    if path_obj.is_dir():
        children_options: List[Option] = []
        children_paths: List[Path] = []  # 临时存储所有子路径

        try:
            for child_path in path_obj.iterdir():
                if child_path.name.startswith("."):
                    continue

                children_paths.append(child_path)

            children_paths.sort(key=lambda p: (-p.is_dir(), p.name.lower()))

            for child_path in children_paths:
                child_option = path_to_option(child_path)
                children_options.append(child_option)

            if children_options:
                option["children"] = children_options

        except PermissionError:
            logger.warning(f"[Tree] Permission denied for: {path_obj}")

    return option


def generate_file_tree_options(root_path_obj: Path) -> List[Option]:
    """
    生成文件树的 Option 列表。
    它负责顶层的收集、排序（文件夹置顶），然后调用 path_to_option 进行递归转换。
    """
    if not root_path_obj.is_dir():
        return []

    # 1. 收集顶层所有非隐藏的 Path 对象
    top_level_paths: List[Path] = []
    try:
        for top_level_path in root_path_obj.iterdir():
            if top_level_path.name.startswith("."):
                continue

            top_level_paths.append(top_level_path)
    except PermissionError:
        logger.warning(f"[Tree] Permission denied for root path: {root_path_obj}")
        return []
    top_level_paths.sort(key=lambda p: (-p.is_dir(), p.name.lower()))

    options: List[Option] = []
    for top_level_path in top_level_paths:
        options.append(path_to_option(top_level_path))

    return options


def get_input_tree_from_pathlib(name: str, label: str, root_dir: Path) -> Dict:
    root_path_obj = Path(root_dir)

    value: List[str] = backup_config.get_config("backup_dir").data
    backup_time: str = backup_config.get_config("backup_time").data
    backup_method: List[str] = backup_config.get_config("backup_method").data

    if not root_path_obj.exists() or not root_path_obj.is_dir():
        logger.warning(f"[Tree] 该目录并不存在或不为目录: {root_dir}")
        # 返回一个空的 tree data
        return get_page("数据备份", [get_input_tree(name, label, value, options=[])])

    options: List[Option] = generate_file_tree_options(root_path_obj)

    tabs = get_tabs(
        [
            get_tab(
                "数据备份配置",
                [
                    get_form(
                        "数据备份",
                        "/genshinuid/setBackUp",
                        [
                            get_alert(
                                "修改配置之后需要立即重启早柚核心才能生效！",
                                "danger",
                            ),
                            get_checkboxes(
                                "backup_method",
                                "备份方式",
                                backup_method,
                                [
                                    {
                                        "label": "备份到文件",
                                        "value": "file",
                                    },
                                    {
                                        "label": "备份到WebDav（暂不可用）",
                                        "value": "web_dav",
                                    },
                                ],
                            ),
                            get_time_select("备份时间", "backup_time", backup_time),
                            get_input_tree(name, label, value, options),
                        ],
                    ),
                ],
            ),
            get_tab(
                "备份下载",
                [
                    get_button(
                        "立即进行一次备份",
                        get_api(
                            "/genshinuid/backUpNow",
                            "get",
                            [],
                        ),
                        "backup_list",
                    ),
                    get_list_download(),
                ],
            ),
        ]
    )

    return get_page("数据备份", [tabs])
