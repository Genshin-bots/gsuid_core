from .create_base_panel import get_input_image_panel


def get_intput_image_page():
    return {
        "type": "page",
        "title": "上传图片",
        "body": [get_input_image_panel("上传背景图", "image")],
        "id": "u:cace1c585efd",
    }
