import re
import json
from typing import Dict, List, Union, TypedDict

from gsuid_core.message_models import Button
from gsuid_core.data_store import get_res_path

buttons_template_path = get_res_path(['template', 'buttons'])
markdown_template_path = get_res_path(['template', 'markdown'])


class MarkdownTemplates(TypedDict):
    template_id: str
    para: List[str]


button_templates: Dict[
    str, Union[List[str], List[Button], List[List[str]], List[List[Button]]]
] = {}
markdown_templates: Dict[str, MarkdownTemplates] = {}

for button_template in buttons_template_path.iterdir():
    with open(button_template, 'r', encoding='UTF-8') as f:
        button_data = json.load(f)
        btl = []
        for buttons in button_data['rows']:
            btr = []
            for button in buttons['buttons']:
                bt = Button(
                    button["render_data"]["label"],
                    button['action']['data'],
                    button["render_data"]["visited_label"],
                )
                btr.append(bt)
            btl.append(btr)
            btr = []
        button_templates[button_template.stem] = btl

for markdown_template in markdown_template_path.iterdir():
    with open(markdown_template, 'r') as file:
        file_content = file.read()
        para_list = re.findall(r'{{([^\n{}]+)}}', file_content)
        new_text = re.sub(r'{{([^\n{}]+)}}', '$$', file_content.strip())
        rep = (
            new_text.replace('(', r'\(')
            .replace(')', r'\)')
            .replace('$$', r'([\s\S]+)')
        )

        markdown_templates[rep] = {
            'template_id': markdown_template.stem,
            'para': [i[1:] for i in para_list],
        }


def parse_button(buttons):
    fake_buttons = []
    for i in buttons:
        if isinstance(i, Button):
            fake_buttons.append(i)
        elif isinstance(i, List):
            fake_buttons.extend(i)
    return fake_buttons
