from pathlib import Path

gs_data_path = Path(__file__).parents[1] / 'data'


def get_res_path() -> Path:
    return gs_data_path
