import re
import json
import asyncio
from typing import Any, Optional, Sequence

from PIL import Image
from json_repair import repair_json
from pydantic_ai.messages import ImageUrl, UserContent

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.configs.ai_config import openai_config

# AI服务配置
model_support: list[str] = openai_config.get_config("model_support").data


def extract_json_from_text(raw_text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw_text).strip()
    cleaned = repair_json(cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*?\]", cleaned, re.DOTALL)
        if match:
            stripped = match.group(0).strip()
            cl = repair_json(stripped)
            data = json.loads(cl)
        else:
            raise
    return data


async def handle_tool_result(bot: Optional[Bot], result: Any, max_length: int = 4000) -> str:
    """
    序列化工具执行结果, 当函数返回Message对象时调用Bot.send方法发送, 并将序列化后的字符串返回方便AI识别。

    Args:
        bot: Bot 对象
        result: 工具函数返回的结果
        max_length: 最大返回长度，超长会被截断

    Returns:
        序列化的字符串
    """
    if isinstance(result, Message):
        a = "生成内容成功!"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关消息！"
        else:
            a += ", 由于没有Bot对象, 未发送相关消息！"
        return a
    elif isinstance(result, str):
        res_str = result
    elif isinstance(result, dict):
        res_str = json.dumps(result, ensure_ascii=False)
    elif isinstance(result, Image.Image):
        img_bytes = await convert_img(result)
        a = f"生成了图片资源, 资源ID: {RM.register(img_bytes)}"
        if bot is not None:
            await bot.send(img_bytes)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, bytes):
        a = f"生成了某项资源, 资源ID: {RM.register(result)}"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, list):
        res_str = json.dumps(result, ensure_ascii=False)
    elif hasattr(result, "model_dump_json"):
        # Pydantic v2
        res_str = result.model_dump_json()
    elif hasattr(result, "json"):
        # Pydantic v1
        res_str = result.json()
    else:
        res_str = str(result)

    # 截断过长的返回值，防止 Token 爆炸
    if len(res_str) > max_length:
        return res_str[:max_length] + f"\n...[系统截断: 省略后 {len(res_str) - max_length} 字符]"
    return res_str


def prepare_content_payload(
    ev: Event,
) -> Sequence[UserContent]:
    """
    准备消息内容列表给AI看, 包含文本、图片ID、文件内容、事件对象

    Args:
        text: 文本内容
        image_ids: 图片 ID 列表
        files_content: 文件内容
        ev: 事件对象

    Returns:
        content payload 列表
    """
    content_payload: Sequence[UserContent] = []
    text = f"--- 当前用户ID: {getattr(ev, 'user_id', 'unknown')} ---\n"

    if not ev.text:
        text += "用户没有发送文本内容。"
    else:
        text += ev.text.strip()

    # 预处理, 将用户发送的文本/AT/图片ID等信息整合到一个字符串中, 方便AI处理
    for i in ev.image_id_list:
        text += f"\n--- 用户上传图片ID: {i} ---\n"

    for at in ev.at_list:
        text += f"\n--- 提及用户(@用户): {at} ---\n"

    text += f"\n--- 当前群ID: {getattr(ev, 'group_id', 'unknown')} ---\n"

    # 处理用户文本消息
    if "text" in model_support:
        content_payload.append(text)

    # 处理用户图片消息
    if "image" in model_support:
        for i in ev.image_list:
            if isinstance(i, str):
                if i.startswith(("http", "https")):
                    img_url = i
                else:
                    # 转为DataURI
                    if i.startswith("base64://"):
                        img_url = f"data:image/png;base64,{i[10:]}"
                    elif i.startswith("data:image/"):
                        img_url = i
                    else:
                        img_url = f"data:image/png;base64,{i}"

                content_payload.append(ImageUrl(url=img_url))
            else:
                logger.warning(f"无法处理图片ID: {i}")

    return content_payload


async def send_chat_result(bot: Bot, chat_result: str):
    """
    解析并发送 chat_result，支持：
    - 按换行分割多条消息
    - @用户ID 语法 → MessageSegment.at(user_id)
    """
    if not chat_result:
        return

    # 按换行分割为多条消息
    lines = chat_result.split("\n")

    for line in lines:
        if not line.strip():
            continue

        # 解析 @user_id 语法，转换为消息段列表
        segments = _parse_at_segments(line)

        # 模拟打字延迟（基于纯文本长度）
        plain_text = re.sub(r"@\d+", "", line)
        await asyncio.sleep(len(plain_text) / 7)

        await bot.send(segments)


def _parse_at_segments(text: str) -> list[Message]:
    """
    将含有 @用户ID 的文本解析为 MessageSegment 列表。

    规则：
    - @后跟纯数字（QQ号格式）才会被解析为 at segment
    - 其余文本保持为 text segment
    - 示例输入："好哦 @444835641 你来看"
    - 示例输出：[Text("好哦 "), At(444835641), Text(" 你来看")]
    """
    # 匹配 @数字，前后允许空格（空格属于分隔符，不计入文本内容）
    pattern = re.compile(r"\s*@(\d+)\s*")
    segments: list[Message] = []
    last_end = 0

    for match in pattern.finditer(text):
        # 匹配前的普通文本
        before = text[last_end : match.start()]
        if before:
            segments.append(MessageSegment.text(before))

        # @ 片段
        user_id = match.group(1)
        segments.append(MessageSegment.at(user_id))

        last_end = match.end()

    # 剩余文本
    tail = text[last_end:]
    if tail:
        segments.append(MessageSegment.text(tail))

    # 如果没有任何 @ 匹配，直接返回原始字符串（兼容旧调用）
    if not segments:
        return [MessageSegment.text(text)]

    return segments
