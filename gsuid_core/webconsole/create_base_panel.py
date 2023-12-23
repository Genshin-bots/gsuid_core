from typing import Dict, List, Optional


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


def get_select_panel(label: str, name: str, value: str, options: List[str]):
    return {
        'type': 'select',
        'label': label,
        'name': name,
        'options': [{'label': option, 'value': option} for option in options],
        'id': 'u:8050095a7c1d',
        'value': value,
    }


def get_input_tag(label: str, name: str, value: List[str], options: List[str]):
    return {
        "type": "input-tag",
        "label": label,
        "name": name,
        "options": [{'label': option, 'value': option} for option in options],
        "id": "u:85ecb7894ccc",
        "optionsTip": "最近您使用的标签",
        "autoFill": {},
        "value": ','.join(value),
    }


def get_divider():
    return {'type': 'divider', 'id': 'u:65e1334b3abe'}


def get_grid_panel(content: List[Dict]):
    _data = [{'body': [i]} for i in content]
    data = {
        'type': 'grid',
        'columns': _data,
        'id': 'u:18d6cb8e78bb',
    }
    return data


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
