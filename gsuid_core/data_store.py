from typing import List, Union, Optional
from pathlib import Path

core_path = Path(__file__).parent
gs_data_path = Path(__file__).parents[1] / "data"


def get_res_path(_path: Optional[Union[str, List[str], Path]] = None) -> Path:
    """
    获取 gsuid_core 数据目录下的资源路径

    如果路径不存在，会自动创建该目录。

    Args:
        _path: 路径参数，支持多种格式：
            - str: 单个路径字符串，如 "configs"、"images/avatars"
            - List[str]: 路径段列表，如 ["logs", "error_reports"] 表示 "logs/error_reports"
            - Path: 完整的绝对路径，传入后直接使用（不与 data_path 拼接）
            - None: 返回数据根目录本身

    Returns:
        解析后的绝对路径Path对象

    Note:
        当传入 Path 对象时，函数会将其视为完整路径直接使用，
        并在路径不存在时调用 mkdir 创建。

    Example:
        >>> get_res_path()  # 返回 data/ 目录
        >>> get_res_path("configs")  # 返回 data/configs/
        >>> get_res_path(["logs", "error_reports"])  # 返回 data/logs/error_reports/
        >>> get_res_path(Path("/tmp/gs_data"))  # 返回 /tmp/gs_data/
    """
    if _path is None:
        path = gs_data_path
    elif isinstance(_path, Path):
        path = _path
    elif isinstance(_path, str):
        path = gs_data_path / _path
    else:
        path = gs_data_path.joinpath(*_path)

    if not path.exists():
        path.mkdir(parents=True)

    return path


# 资源路径
RES = get_res_path()

image_res = get_res_path("IMAGE_TEMP")
data_cache_path = get_res_path("DATA_CACHE_PATH")
backup_path = get_res_path("GsCore_BACKUP_PATH")
gscore_data_path = get_res_path("GsCore")
error_mark_path = get_res_path(["logs", "error_reports"])
CONFIGS_PATH = get_res_path("configs")

# 主题配置路径 / Theme Config Path
THEME_CONFIG_PATH = gs_data_path / "theme_config.json"

# 网页控制台相关路径 / Web Console
WEBCONSOLE_PATH = Path(__file__).parent / "webconsole"
DIST_PATH = WEBCONSOLE_PATH / "dist"
DIST_EX_PATH = gs_data_path / "dist"

# AI / AI Core
AI_CORE_PATH = get_res_path("ai_core")
