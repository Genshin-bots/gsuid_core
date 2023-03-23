from typing import Dict

from gsuid_core.plugins.GenshinUID.GenshinUID.genshinuid_config import (
    gs_config,
)
from gsuid_core.plugins.GenshinUID.GenshinUID.genshinuid_config.models import (
    GsStrConfig,
)

gsconfig = gs_config.gsconfig


def get_str_panel(name: str, value: str) -> Dict:
    return {}


def get_all_config():
    page = {
        'type': 'page',
        'title': '配置管理',
        'body': [],
        'id': 'u:a9be7e0dc626',
    }
    body = []
    for config in gsconfig:
        gsc = gsconfig[config]
        if isinstance(gsc, GsStrConfig):
            body.append(get_str_panel(config, gsc.data))

    page['body'] = body
    return page
