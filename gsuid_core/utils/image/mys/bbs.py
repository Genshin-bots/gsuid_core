import textwrap
from typing import Any, Dict, List, Union

from bs4 import BeautifulSoup, element
from PIL import Image, ImageDraw

from gsuid_core.logger import logger
from gsuid_core.utils.error_reply import get_error
from gsuid_core.utils.fonts.fonts import core_font as cf
from gsuid_core.utils.image.utils import download_pic_to_image
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.image.image_tools import get_div

from ...api.mys_api import mys_api


async def get_post_img(post_id: str) -> Union[str, bytes]:
    logger.info("[GsCore] 正在尝试获取米游社帖子...")
    data = await mys_api.get_bbs_post_detail(post_id)
    if isinstance(data, int):
        return get_error(data)
    logger.success("[GsCore] 获取米游社帖子成功!进入处理流程...")
    post = data["post"]["post"]["content"]
    soup = BeautifulSoup(post, "lxml")
    img = await soup_to_img(soup)
    return img


async def process_tag(
    elements: List[Dict[str, Any]],
    point: int,
    tag: element.Tag,
):
    space = 15
    _type = _data = None

    logger.trace(f"[GsCore] 正在处理TAG: {tag.name}")

    if tag.name == "img":
        img_url = tag.get("src")
        if isinstance(img_url, str):
            if not img_url.startswith("https://mihoyo-community-web"):
                img = await download_pic_to_image(img_url)
                new_h = int((930 / img.size[0]) * img.size[1])
                img = img.resize((930, new_h))
                point += new_h
                _type = "image"
                _data = img
    elif tag.name and tag.name.startswith("h") and tag.name != "html":
        text = tag.get_text(strip=True)
        line = len(textwrap.wrap(text, width=14))
        point += 70 * line if line >= 1 else 70
        _type = "title"
        _data = text
    elif tag.name == "div" and tag.has_attr("class"):
        if "ql-divider" in tag["class"]:
            tag_img = tag.find("img")
            if isinstance(tag_img, element.Tag):
                img_url = tag_img.get("src")
                if img_url:
                    point += 60
                    _type = "div"
                    _data = "div"
    elif tag.name == "p":
        text = tag.get_text(strip=True)
        if text:
            line = len(textwrap.wrap(text, width=30))
            point += 30 * line if line >= 1 else 30
            _type = "text"
            _data = text

    if _data is not None and _type is not None:
        if elements:
            pre_pos = elements[-1]["next_pos"]
        else:
            pre_pos = 0
        elements.append(
            {
                "type": _type,
                "data": _data,
                "pos": pre_pos,
                "next_pos": point,
            }
        )
        point += space

    return point, elements


async def soup_to_img(soup: BeautifulSoup):
    elements = []
    point = 0
    div = get_div()

    logger.info("[GsCore] 开始解析帖子内容...")
    for tag in soup.descendants:
        point, elements = await process_tag(
            elements,
            point,
            tag,  # type: ignore
        )
    logger.success("[GsCore] 帖子解析完成!进入图片处理流程...")

    img = Image.new("RGB", (1000, point), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in elements:
        if i["type"] == "image":
            img.paste(i["data"], (35, i["pos"]))
        elif i["type"] == "title":
            draw.text(
                (35, i["pos"]),
                i["data"],
                font=cf(60),
                fill=(0, 0, 0),
            )
        elif i["type"] == "text":
            wrapped_text = textwrap.wrap(i["data"], width=30)
            for index, line in enumerate(wrapped_text):
                draw.text(
                    (35, i["pos"] + index * 30),
                    line,
                    font=cf(30),
                    fill=(0, 0, 0),
                )
        elif i["type"] == "div":
            img.paste(div, (0, i["pos"]), div)

    logger.success("[GsCore] 图片处理完成!")

    return await convert_img(img)
