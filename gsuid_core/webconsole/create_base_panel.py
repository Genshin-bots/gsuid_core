from typing import Dict, List


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
