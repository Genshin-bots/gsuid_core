from typing import List, Literal

from gsuid_core.sv import SL


def get_sv_panel(
    name: str = '',
    pm: int = 3,
    priority: int = 5,
    enabled: bool = True,
    area: Literal['GROUP', 'DIRECT', 'ALL'] = 'ALL',
    black_list: List = [],
):
    api = f'/genshinuid/setSV/{name}'
    card = {
        "type": "service",
        "body": {
            'type': 'card',
            'header': {'title': '', 'subTitle': ''},
            'body': [
                {
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
                },
                {
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
                                        {'label': '超级管理员', 'value': '1'},
                                        {'label': '管理员', 'value': '2'},
                                        {'label': '正常', 'value': '3'},
                                        {'label': '几乎所有人', 'value': '4'},
                                        {'label': '所有人', 'value': '5'},
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
                                    'options': [
                                        {'label': '全局', 'value': 'ALL'},
                                        {'label': '仅限私聊', 'value': 'DIRECT'},
                                        {'label': '仅限群聊', 'value': 'GROUP'},
                                    ],
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
                },
                {
                    'type': 'flex',
                    'className': 'p-1',
                    'items': [
                        {
                            'type': 'container',
                            'size': 'xs',
                            'body': [
                                {
                                    'type': 'input-text',
                                    'label': '黑名单（以;为分割）',
                                    'name': 'black_list',
                                    'id': 'u:ab168d425936',
                                    'value': ';'.join(black_list),
                                }
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
                },
            ],
            'actions': [
                {
                    'type': 'button',
                    'label': '确认修改',
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

    return card


def get_sv_page():
    page = {
        'type': 'page',
        'title': '功能管理',
        'body': [],
        'id': 'u:a9be7e0dc676',
    }
    for sv_name in SL.lst:
        sv = SL.lst[sv_name]
        panel = get_sv_panel(
            sv.name,
            sv.pm,
            sv.priority,
            sv.enabled,
            sv.area,  # type:ignore
            sv.black_list,
        )
        page['body'].append(panel)

    return page
