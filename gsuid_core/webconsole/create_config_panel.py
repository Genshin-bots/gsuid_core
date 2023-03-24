from gsuid_core.plugins.GenshinUID.GenshinUID.genshinuid_config import (
    gs_config,
)
from gsuid_core.webconsole.create_base_panel import (
    get_text_panel,
    get_switch_panel,
    get_container_panel,
)
from gsuid_core.plugins.GenshinUID.GenshinUID.genshinuid_config.models import (
    GsStrConfig,
    GsBoolConfig,
    GsListStrConfig,
)

gsconfig = gs_config.gsconfig


def get_config_page():
    page = {
        'type': 'page',
        'title': '配置管理',
        'body': [],
        'id': 'u:a9be7e0dc626',
    }
    card = {
        'type': 'card',
        'header': {'title': '', 'subTitle': ''},
        'body': [],
        'actions': [
            {
                'type': 'button',
                'label': '确认修改',
                'id': 'u:5784cfaa5c0a',
                'actionType': 'ajax',
                'api': '/genshinuid/setGsConfig',
                'onEvent': {
                    'click': {
                        'weight': 0,
                        'actions': [
                            {
                                'args': {
                                    'msgType': 'success',
                                    'position': 'top-center',
                                    'closeButton': True,
                                    'showIcon': True,
                                    'msg': '成功设置！',
                                    'timeout': 100,
                                },
                                'actionType': 'toast',
                            }
                        ],
                    }
                },
            }
        ],
        'id': 'u:69b06813bfbe',
    }
    body = []
    solo_body = []
    for config in gsconfig:
        gsc = gsconfig[config]
        if isinstance(gsc, GsStrConfig):
            solo_body.append(get_text_panel(gsc.title, config, gsc.data))
        elif isinstance(gsc, GsBoolConfig):
            solo_body.append(get_switch_panel(gsc.title, config, gsc.data))
        elif isinstance(gsc, GsListStrConfig):
            solo_body.append(
                get_text_panel(gsc.title, config, ':'.join(gsc.data))
            )
        if len(solo_body) == 3:
            body.append(get_container_panel(solo_body))
            solo_body = []
    body.append(get_container_panel(solo_body))
    card['body'] = body
    page['body'].append(card)
    return page
