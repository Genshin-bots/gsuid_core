from pathlib import Path

gs_data_path = Path(__file__).parents[1] / 'data'


def get_res_path() -> Path:
    if not gs_data_path.exists():
        gs_data_path.mkdir()
    return gs_data_path
