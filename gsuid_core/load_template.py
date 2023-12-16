import re
import json
from typing import Dict, List, TypedDict

from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.data_store import get_res_path
from gsuid_core.message_models import Button, ButtonType

buttons_template_path = get_res_path(['template', 'buttons'])
markdown_template_path = get_res_path(['template', 'markdown'])
custom_buttons_template = get_res_path(['template', 'custom_buttons'])


class MarkdownTemplates(TypedDict):
    template_id: str
    para: List[str]


button_templates: Dict[str, ButtonType] = {}
markdown_templates: Dict[str, MarkdownTemplates] = {}
custom_buttons: Dict[str, Message] = {}


def template_button_to_buttons(button_data: Dict):
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
    return btl


def parse_button(buttons):
    fake_buttons = []
    for i in buttons:
        if isinstance(i, Button):
            fake_buttons.append(i)
        elif isinstance(i, List):
            fake_buttons.extend(i)
    return fake_buttons


try:
    for button_template in buttons_template_path.iterdir():
        with open(button_template, 'r', encoding='UTF-8') as f:
            button_data = json.load(f)
            btl = template_button_to_buttons(button_data)
            button_templates[button_template.stem] = btl

    for markdown_template in markdown_template_path.iterdir():
        with open(markdown_template, 'r') as file:
            file_content = file.read()
            para_list = re.findall(r'{{([^\n{}]+)}}', file_content)
            new_text = re.sub(r'{{([^\n{}]+)}}', '$$', file_content.strip())
            rep = (
                r'('
                + (
                    new_text.replace('(', r'\(')
                    .replace(')', r'\)')
                    .replace(']', r'\]')
                    .replace('[', r'\[')
                    .replace('\n', r')?\n?(')
                )
                + r')?'
            )

            for para in para_list:
                rep = rep.replace(
                    '$$', rf'(?P<{para.replace(".","")}>[\s\S]+)', 1
                )

            markdown_templates[rep] = {
                'template_id': markdown_template.stem,
                'para': [i[1:] for i in para_list],
            }

    for custom_button in custom_buttons_template.iterdir():
        with open(custom_button, 'r', encoding='UTF-8') as f:
            button_data = json.load(f)

        if 'id' in button_data and button_data['id']:
            custom_buttons[custom_button.stem] = Message(
                type='template_buttons', data=button_data['id']
            )
        elif 'custom_button' in button_data and button_data['custom_button']:
            custom_buttons[custom_button.stem] = Message(
                type='buttons', data=button_data['custom_button']
            )

except Exception as e:
    logger.warning('[启动] [加载模板] 加载失败...检查模板文件..')
    logger.error(e)
