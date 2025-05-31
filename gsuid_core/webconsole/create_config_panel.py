from gsuid_core.utils.plugins_config.gs_config import all_config_list
from gsuid_core.utils.plugins_config.models import (
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsImageConfig,
    GsListStrConfig,
)
from gsuid_core.webconsole.create_base_panel import (
    get_api,
    get_tab,
    get_tabs,
    get_button,
    get_divider,
    get_service,
    get_input_tag,
    get_grid_panel,
    get_text_panel,
    get_image_input,
    get_input_number,
    get_select_panel,
    get_switch_panel,
)


def get_card_page(card_name: str):
    data = get_service(
        [
            {
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
            }
        ]
    )
    return data


def get_config_page():
    page = {
        'type': 'page',
        'title': '配置管理',
        'body': [],
        'id': 'u:a9be7e0dc626',
    }
    body = []
    tabs = []
    solo_body = []
    for config_name in all_config_list:
        body.append(get_divider())
        _config = all_config_list[config_name]
        for config in _config:
            gsc = _config[config]
            if isinstance(gsc, GsStrConfig):
                if gsc.options:
                    _data = get_select_panel(
                        gsc.title,
                        config,
                        gsc.data,
                        gsc.options,
                        gsc.title,
                        gsc.desc,
                    )
                else:
                    _data = get_text_panel(
                        gsc.title,
                        config,
                        gsc.data,
                        gsc.title,
                        gsc.desc,
                    )
                solo_body.append(_data)
            elif isinstance(gsc, GsIntConfig):
                solo_body.append(
                    get_input_number(
                        gsc.title,
                        config,
                        gsc.data,
                        gsc.max_value,
                        gsc.title,
                        gsc.desc,
                    )
                )
            elif isinstance(gsc, GsBoolConfig):
                solo_body.append(
                    get_switch_panel(
                        gsc.title,
                        config,
                        gsc.data,
                        gsc.title,
                        gsc.desc,
                    )
                )
            elif isinstance(gsc, GsListStrConfig):
                if not gsc.options:
                    _data = get_text_panel(
                        gsc.title,
                        config,
                        ':'.join(gsc.data),
                        gsc.title,
                        gsc.desc,
                    )
                else:
                    _data = get_input_tag(
                        gsc.title,
                        config,
                        gsc.data,
                        gsc.options,
                        gsc.title,
                        gsc.desc,
                    )
                solo_body.append(_data)
            elif isinstance(gsc, GsImageConfig):
                solo_body.append(
                    get_image_input(
                        gsc.title,
                        config,
                        gsc.upload_to,
                        gsc.filename,
                        gsc.suffix,
                    )
                )
            if len(solo_body) == 3:
                body.append(get_grid_panel(solo_body))
                body.append(get_divider())
                solo_body = []

        if solo_body:
            while len(solo_body) < 3:
                solo_body.append(get_service([]))

        body.append(get_grid_panel(solo_body))
        body.append(get_divider())
        body.append(
            get_button(
                '✅确认修改',
                get_api(
                    f'/genshinuid/setGsConfig/{config_name}',
                    'post',
                    [config for config in _config],
                ),
            )
        )
        tabs.append(get_tab(config_name, [get_service(body)]))
        body = []
        solo_body = []

    page['body'].append(get_tabs(tabs))
    return page
