from pathlib import Path
from typing import Dict, List, Union, Literal, Optional


def get_image_input(label: str, name: str, UPLOAD_PATH: Union[Path, str]):
    return {
        "type": "input-image",
        "name": name,
        "label": label,
        "mode": "horizontal",
        "labelAlign": "left",
        "accept": "image/jpeg, image/jpg, image/png",
        "receiver": f'/genshinuid/uploadImage/{UPLOAD_PATH}',
        "multiple": False,
        "autoUpload": False,
        "joinValues": False,
        "onEvent": {
            "success": {
                "actions": [
                    {
                        "actionType": "toast",
                        "args": {
                            "msgType": "info",
                            "msg": "「${event.data.path}」上传成功",
                        },
                    }
                ]
            }
        },
    }


def get_service(body: List[Dict]):
    return {
        'type': 'service',
        'body': body,
        'id': 'u:4c2981f6a055',
    }


def get_input_number(
    label: str, name: str, value: int, max_value: Optional[int] = None
):
    data = {
        'type': 'input-number',
        'label': label,
        'name': name,
        'keyboard': True,
        'id': 'u:0b72c9b8086d',
        'step': 1,
        'value': value,
    }
    if max_value is not None:
        data['max'] = max_value
    return data


def get_input_image_panel(label: str, name: str):
    return {
        'type': 'input-image',
        'label': label,
        'name': name,
        'autoUpload': True,
        'proxy': True,
        'uploadType': 'fileReceptor',
        'imageClassName': 'r w-full',
        'id': 'u:1a381f9ccb8c',
        'accept': '.jpeg, .jpg, .png, .gif',
        'multiple': False,
        'hideUploadButton': False,
        'fixedSize': False,
    }


def get_api(url: str, method: str, data: List[str]):
    return {
        'ignoreError': False,
        'outputVar': 'responseResult',
        'actionType': 'ajax',
        'options': {},
        'api': {
            'url': url,
            'method': method,
            'requestAdaptor': '',
            'adaptor': '',
            'messages': {},
            'dataType': 'json',
            'data': {item: f'${item}' for item in data},
        },
    }


def get_button(title: str, api: Optional[Dict] = None):
    data = {
        'type': 'button',
        'label': title,
        'onEvent': {'click': {'actions': []}},
        'id': 'u:2784abaa9455',
        'block': True,
        'messages': {
            'success': '成功！',
            'failed': '失败...请检查后台...',
        },
    }
    if api:
        data['onEvent']['click']['actions'].append(api)
    return data


def get_switch_panel(label: str, name: str, value: bool):
    return {
        'type': 'switch',
        'label': label,
        'option': '',
        'name': name,
        'falseValue': False,
        'trueValue': True,
        'id': 'u:d0bc78558aa9',
        'value': value,
    }


def get_text_panel(label: str, name: str, value: str):
    return {
        'type': 'input-text',
        'label': label,
        'name': name,
        'id': 'u:2de3dcaddcc1',
        'value': value,
    }


def get_alert(
    message: str,
    level: Literal['success', 'warning', 'info', 'danger'] = 'info',
):
    return {
        'type': 'alert',
        'body': message,
        'level': level,
        'showCloseButton': True,
        'showIcon': True,
        'className': 'mb-2',
    }


def get_select_panel(label: str, name: str, value: str, options: List[str]):
    return {
        'type': 'input-text',
        'label': label,
        'name': name,
        'options': [{'label': option, 'value': option} for option in options],
        'id': 'u:8050095a7c1d',
        'value': value,
    }


def get_input_tag(label: str, name: str, value: List[str], options: List[str]):
    values = []
    for i in value:
        if isinstance(i, int):
            _data = str(i)
        else:
            _data = i
        values.append(_data)
    return {
        'type': 'input-tag',
        'label': label,
        'name': name,
        'options': [{'label': option, 'value': option} for option in options],
        'id': 'u:85ecb7894ccc',
        'optionsTip': '最近您使用的标签',
        'autoFill': {},
        'value': ','.join(values),
    }


def get_divider():
    return {'type': 'divider', 'id': 'u:65e1334b3abe'}


def get_empty():
    return {'body': [], 'id': 'u:d0bf1032034b'}


def get_grid_panel(content: List[Dict]):
    _data = []
    for i in content:
        if i == {'body': [], 'id': 'u:d0bf1032034b'} or i == {}:
            _data.append({'body': [], 'id': 'u:d0bf1032034b'})
        else:
            _data.append({'body': [i]})

    data = {
        'type': 'grid',
        'columns': _data,
        'id': 'u:18d6cb8e78bb',
    }
    return data


def get_property(items: Union[List[Dict], Dict[str, str]], column: int = 2):
    if isinstance(items, dict):
        _items = [{'label': item, 'content': items[item]} for item in items]
    else:
        _items = items

    data = {
        'type': 'property',
        'column': column,
        'items': _items,
    }
    return data


def get_tag(
    label: str,
    color: Literal[
        'inactive', 'processing', 'success', 'error', 'active', 'warning'
    ] = 'processing',
    displaymode: Literal['normal', 'rounded'] = 'normal',
):
    return {
        'type': 'tag',
        'label': label,
        'displayMode': displaymode,
        'color': color,
    }


def get_tpl(label: str = '', value: str = ''):
    return {
        'type': 'tpl',
        'tpl': value,
        'inline': False,
        'label': label,
    }


def get_card(title: str, content: List[Dict]):
    data = get_service(
        [
            {
                'type': 'card',
                'header': {'title': title, 'subTitle': ''},
                'body': content,
                'id': 'u:69b06813bfbe',
            }
        ]
    )
    return data


def get_tab(title: str, bodys: List[Dict]):
    return {'title': title, 'body': bodys}


def get_tabs(tabs: List[Dict]):
    return {
        'type': 'tabs',
        'swipeable': True,
        'tabs': tabs,
    }


def get_container_panel(content: List[Dict]):
    return {
        'type': 'flex',
        'className': 'p-1',
        'justify': 'space-evenly',
        'alignItems': 'center',
        'items': [
            {
                'type': 'container',
                'body': [content[0]] if len(content) >= 1 else [],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '0 0 200px',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:7433706a00d0',
            },
            {
                'type': 'container',
                'body': [content[1]] if len(content) >= 2 else [],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '0 0 200px',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:5baaf2f4b281',
            },
            {
                'type': 'container',
                'body': [content[2]] if len(content) >= 3 else [],
                'size': 'xs',
                'style': {
                    'position': 'static',
                    'display': 'block',
                    'flex': '0 0 200px',
                    'flexGrow': 1,
                    'flexBasis': 'auto',
                },
                'wrapperBody': False,
                'isFixedHeight': False,
                'isFixedWidth': False,
                'id': 'u:0f837da20702',
            },
        ],
    }
