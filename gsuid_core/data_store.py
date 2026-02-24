from typing import List, Union, Optional
from pathlib import Path

core_path = Path(__file__).parent
gs_data_path = Path(__file__).parents[1] / "data"


def get_res_path(_path: Optional[Union[str, List]] = None) -> Path:
    if _path:
        if isinstance(_path, str):
            path = gs_data_path / _path
        else:
            path = gs_data_path.joinpath(*_path)
    else:
        path = gs_data_path

    if not path.exists():
        path.mkdir(parents=True)

    return path


image_res = get_res_path("IMAGE_TEMP")
data_cache_path = get_res_path("DATA_CACHE_PATH")
backup_path = get_res_path("GsCore_BACKUP_PATH")
gscore_data_path = get_res_path("GsCore")
error_mark_path = get_res_path(["logs", "error_reports"])
AI_CORE_PATH = get_res_path("ai_core")
