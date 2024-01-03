from typing import Dict, List

from gsuid_core.config import CONFIG_DEFAULT, core_config
from gsuid_core.webconsole.create_base_panel import (
    get_api,
    get_alert,
    get_button,
    get_divider,
    get_service,
    get_input_tag,
    get_grid_panel,
    get_text_panel,
    get_input_number,
    get_select_panel,
)


def get_core_config_page():
    page = {
        'type': 'page',
        'title': 'Core配置',
        'body': [],
        'id': 'u:a9be7e0dc626',
    }
    body = [get_alert('如无法确定选项原意，切勿随意修改，修改需重启GsCore生效', 'warning')]
    solo_body = []

    api_input = []
    for c in core_config.config:
        if c in ['sv', 'plugins']:
            continue
        data = core_config.config[c]

        if isinstance(data, List):
            api_input.append(c)
            _data = get_input_tag(c, c, data, CONFIG_DEFAULT[c])
            solo_body.append(_data)
        elif isinstance(data, int):
            api_input.append(c)
            _data = get_input_number(c, c, data)
            solo_body.append(_data)
        elif isinstance(data, str):
            api_input.append(c)
            _data = get_text_panel(c, c, data)
            solo_body.append(_data)
        elif isinstance(data, Dict):
            for d in data:
                tag = f'{c}_{d}'
                api_input.append(tag)
                if d == 'level':
                    _data = get_select_panel(
                        tag,
                        tag,
                        data[d],
                        ['INFO', 'DEBUG', 'WARNING', 'ERROR', 'TRACE'],
                    )
                    solo_body.append(_data)
                elif d == 'output':
                    _data = get_input_tag(
                        tag,
                        tag,
                        data[d],
                        CONFIG_DEFAULT[c][d],
                    )
                    solo_body.append(_data)

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
                '/genshinuid/setCoreConfig',
                'post',
                api_input,
            ),
        )
    )
    page['body'] = body

    return page
