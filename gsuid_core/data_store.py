from pathlib import Path
from typing import Optional

gs_data_path = Path(__file__).parents[1] / 'data'


def get_res_path(_path: Optional[str] = None) -> Path:
    if _path:
        path = gs_data_path / _path
    else:
        path = gs_data_path

    if not path.exists():
        path.mkdir()

    return path
