from typing import Dict, List, Literal

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.webconsole.create_base_panel import (
    get_tab,
    get_tabs,
    get_alert,
    get_empty,
    get_divider,
    get_input_tag,
    get_grid_panel,
)


def get_sv_panel(
    API: str = '/genshinuid/setSV',
    name: str = '',
    pm: int = 3,
    priority: int = 5,
    enabled: bool = True,
    area: Literal['GROUP', 'DIRECT', 'ALL'] = 'ALL',
    black_list: List = [],
    white_list: List = [],
    prefix: List = [],
    force_prefix: List[str] = [],
    disable_force_prefix: bool = False,
    allow_empty_prefix: bool = False,
    tl: Dict = {},
):
    api = f'{API}/{name}'
    if force_prefix:
        defalut_prefix = ','.join(force_prefix)
    else:
        defalut_prefix = '无自带前缀'

    area_options = [
        {'label': '全局', 'value': 'ALL'},
        {'label': '仅限私聊', 'value': 'DIRECT'},
        {'label': '仅限群聊', 'value': 'GROUP'},
    ]
    if API == '/genshinuid/setPlugins':
        area_options.append({'label': '按照服务设定', 'value': 'SV'})

    extra = {
        'type': 'flex',
        'className': 'p-1',
        'items': [
            {
                'type': 'container',
                'body': [
                    get_input_tag(
                        '自定义插件前缀(不影响插件自带前缀, 修改需重启)',
                        'prefix',
                        prefix,
                        [],
                    ),
                ],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:bafbdfce89c3',
            },
            {
                'type': 'container',
                'body': [
                    {
                        'type': 'switch',
                        'label': f'是否禁用插件自带前缀({defalut_prefix}) (修改需重启)',
                        'option': '开启/关闭功能',
                        'name': 'disable_force_prefix',
                        'falseValue': False,
                        'trueValue': True,
                        'id': 'u:d739bc85f366',
                        'value': disable_force_prefix,
                    },
                ],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:80670a4807f5',
            },
            {
                'type': 'container',
                'body': [
                    {
                        'type': 'switch',
                        'label': '是否允许空前缀 (修改需重启)',
                        'option': '开启/关闭功能',
                        'name': 'allow_empty_prefix',
                        'falseValue': False,
                        'trueValue': True,
                        'id': 'u:d739bc85f359',
                        'value': allow_empty_prefix,
                    },
                ],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:80670a4807f2',
            },
        ],
        'style': {'position': 'static'},
        'direction': 'row',
        'justify': 'flex-start',
        'alignItems': 'stretch',
        'id': 'u:2a2b198f141b',
        'label': '',
    }

    if tl:
        value = []
        for a in tl:
            tg = tl[a]
            for t in tg:
                value.append(t)

        mapping = {
            "type": "mapping",
            "value": value,
            "map": {
                a: f"<span class='label label-warning'>{a}</span>"
                for a in value
            },
        }
    else:
        mapping = {}

    switch = {
        'type': 'flex',
        'className': 'p-1',
        'items': [
            {
                'type': 'container',
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:bafbdfce89c2',
                'body': [
                    {
                        'type': 'tpl',
                        'tpl': name,
                        'inline': True,
                        'wrapperComponent': '',
                        'id': 'u:cd523cbd8f0c',
                        'style': {
                            'fontFamily': '',
                            'fontSize': 25,
                        },
                    },
                    {
                        'type': 'switch',
                        'label': '总开关',
                        'option': '开启/关闭功能',
                        'name': 'enabled',
                        'falseValue': False,
                        'trueValue': True,
                        'id': 'u:d739bc85f307',
                        'value': enabled,
                    },
                ],
            },
            {
                'type': 'container',
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:80670a4807f2',
            },
            {
                'type': 'container',
                'body': [],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:f24811f21e93',
            },
        ],
        'style': {'position': 'static'},
        'direction': 'row',
        'justify': 'flex-start',
        'alignItems': 'stretch',
        'id': 'u:2a2b198f141b',
        'label': '',
    }

    ol = {
        'type': 'flex',
        'className': 'p-1',
        'items': [
            {
                'type': 'container',
                'body': [
                    {
                        'type': 'select',
                        'label': '权限控制',
                        'name': 'pm',
                        'options': [
                            {'label': 'BOT主人', 'value': '0'},
                            {'label': '超级管理员', 'value': '1'},
                            {'label': '群主', 'value': '2'},
                            {'label': '管理员', 'value': '3'},
                            {'label': '频道管理员', 'value': '4'},
                            {
                                'label': '子频道管理员',
                                'value': '5',
                            },
                            {'label': '正常人', 'value': '6'},
                            {'label': '权限极低', 'value': '7'},
                            {'label': '黑名单', 'value': '8'},
                        ],
                        'id': 'u:c71f20b605d4',
                        'multiple': False,
                        'value': str(pm),
                    }
                ],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:bafbdfce89c2',
            },
            {
                'type': 'container',
                'body': [
                    {
                        'type': 'input-number',
                        'label': '命令优先级',
                        'name': 'priority',
                        'keyboard': True,
                        'id': 'u:0b72c9b8086d',
                        'step': 1,
                        'value': priority,
                    }
                ],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:80670a4807f2',
            },
            {
                'type': 'container',
                'body': [
                    {
                        'type': 'select',
                        'label': '作用范围',
                        'name': 'area',
                        'options': area_options,
                        'id': 'u:88e66f806556',
                        'multiple': False,
                        'value': area,
                    }
                ],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '1 1 auto',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:f24811f21e93',
            },
        ],
        'style': {'position': 'static'},
        'direction': 'row',
        'justify': 'flex-start',
        'alignItems': 'stretch',
        'id': 'u:2a2b198f141b',
        'label': '',
    }

    black = {
        'type': 'flex',
        'className': 'p-1',
        'items': [
            {
                'type': 'container',
                'size': 'xs',
                'body': [
                    get_input_tag('黑名单', 'black_list', black_list, [])
                ],
                'wrapperBody': False,
                'style': {'flex': '0 0 auto', 'display': 'block'},
                'id': 'u:48c938f71548',
            }
        ],
        'direction': 'column',
        'justify': 'center',
        'alignItems': 'stretch',
        'id': 'u:a7b2f1bbc0a8',
        'label': '',
    }

    white = {
        'type': 'flex',
        'className': 'p-1',
        'items': [
            {
                'type': 'container',
                'size': 'xs',
                'body': [
                    get_input_tag('白名单', 'white_list', white_list, [])
                ],
                'wrapperBody': False,
                'style': {'flex': '0 0 auto', 'display': 'block'},
                'id': 'u:48c938f71548',
            }
        ],
        'direction': 'column',
        'justify': 'center',
        'alignItems': 'stretch',
        'id': 'u:a7b2f1bbc0a8',
        'label': '',
    }
    card = {
        "type": "service",
        "body": {
            'type': 'card',
            'header': {'title': '', 'subTitle': ''},
            'body': [],
            'actions': [
                {
                    'type': 'button',
                    'label': '✅ 确认修改',
                    'id': 'u:5784cfaa5c0a',
                    'actionType': 'ajax',
                    'api': api,
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
        "id": "u:4c2981f6a055",
    }

    if API == '/genshinuid/setPlugins':
        card['body']['body'].extend([switch, ol, extra, black, white])
    else:
        card['body']['body'].extend(
            [mapping, get_divider(), switch, ol, black, white]
        )

    return card


def get_sv_body(sv_list: List[SV], plugins: Plugins):
    panels = []
    grids = []
    grids.append(
        get_alert('该设定卡片为总设定，以下服务的触发均需满足总设定条件')
    )
    grids.append(
        get_sv_panel(
            '/genshinuid/setPlugins',
            plugins.name,
            plugins.pm,
            plugins.priority,
            plugins.enabled,
            plugins.area,  # type:ignore
            plugins.black_list,
            plugins.white_list,
            plugins.prefix,
            plugins.force_prefix,
            plugins.disable_force_prefix,
            plugins.allow_empty_prefix,
        )
    )
    grids.append(get_divider())
    grids.append(get_alert('以下设定卡片为服务设定，控制单个服务的触发条件'))
    for sv in sv_list:
        panel = get_sv_panel(
            '/genshinuid/setSV',
            sv.name,
            sv.pm,
            sv.priority,
            sv.enabled,
            sv.area,  # type:ignore
            sv.black_list,
            sv.white_list,
            tl=sv.TL,
        )
        panels.append(panel)
        if len(panels) == 2:
            grids.append(get_grid_panel(panels))
            panels = []
    else:
        if panels != []:
            panels.append(get_empty())
            grids.append(get_grid_panel(panels))
            panels = []

    return grids


def get_ssv_page(sv_list: List[SV], plugins: Plugins):
    page = {
        'type': 'page',
        'title': '功能服务配置',
        'body': get_sv_body(sv_list, plugins),
        'id': 'u:a9be8e0dc67d',
    }
    return page


def get_sv_page():
    page = {
        'type': 'page',
        'title': '功能服务配置',
        'body': [],
        'id': 'u:a9be7e0dc676',
    }
    tabs = []
    for plugins in SL.detail_lst:
        sv_list = SL.detail_lst[plugins]
        grids = get_sv_body(sv_list, plugins)
        tabs.append(get_tab(plugins.name, grids))

    tabs = get_tabs(tabs)
    page['body'].append(tabs)
    return page
