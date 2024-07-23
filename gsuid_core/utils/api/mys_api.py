from gsuid_core.utils.api.mys import MysApi
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

gsconfig = core_plugins_config


class _MysApi(MysApi):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


mys_api = _MysApi()
