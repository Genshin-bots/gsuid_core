from gsuid_core.utils.plugins_config.gs_config import all_config_list
from gsuid_core.utils.plugins_config.models import (
    GsStrConfig,
    GsBoolConfig,
    GsListStrConfig,
)
from gsuid_core.webconsole.create_base_panel import (
    get_text_panel,
    get_switch_panel,
    get_container_panel,
)


def get_card_page(card_name: str):
    return {
        'type': 'service',
        'body': {
            'type': 'card',
            'header': {'title': card_name, 'subTitle': ''},
            'body': [],
            'actions': [
                {
                    'type': 'button',
                    'label': '确认修改',
                    'id': 'u:5784cfaa5c0a',
                    'actionType': 'ajax',
                    'api': f'/genshinuid/setGsConfig/{card_name}',
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
        },
        'id': 'u:4c2981f6a055',
    }


def get_config_page():
    page = {
        'type': 'page',
        'title': '配置管理',
        'body': [],
        'id': 'u:a9be7e0dc626',
    }
    body = []
    solo_body = []
    for config_name in all_config_list:
        card = get_card_page(config_name)
        _config = all_config_list[config_name]
        for config in _config:
            gsc = _config[config]
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
        card['body']['body'] = body
        page['body'].append(card)
        body = []
        solo_body = []
    return page
