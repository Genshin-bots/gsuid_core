import importlib

from gsuid_core.sv import SL
from gsuid_core.gss import gss
from gsuid_core.logger import logger


def reload_plugin(plugin_name: str):
    logger.info(f'🔔 正在重载插件{plugin_name}...')

    del_k = []
    del_v = []
    for sv_name in SL.lst:
        sv = SL.lst[sv_name]
        if sv.plugins.name == plugin_name:
            del_k.append(sv_name)
            if sv.plugins not in del_v:
                del_v.append(sv.plugins)

    for k in del_k:
        del SL.lst[k]
    for v in del_v:
        del SL.detail_lst[v]
    del SL.plugins[plugin_name]

    retcode = gss.load_plugin(plugin_name)
    if isinstance(retcode, str):
        logger.info(f'❌ 重载插件{plugin_name}失败...')
        return retcode
    else:
        for module in retcode:
            importlib.reload(module)
        logger.info(f'✨ 已重载插件{plugin_name}')
        return f'✨ 已重载插件{plugin_name}!'
