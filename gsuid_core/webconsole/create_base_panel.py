from typing import Dict, List


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


def get_container_panel(
    content: List[Dict],
):
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
