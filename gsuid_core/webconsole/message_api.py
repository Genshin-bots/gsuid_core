"""
Message APIs
提供消息推送相关的 RESTful APIs
包括批量消息推送功能
"""

import asyncio
from io import BytesIO
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime

import aiofiles
from bs4 import Tag, BeautifulSoup
from PIL import Image
from fastapi import Depends, Request, Response, UploadFile, BackgroundTasks
from fastapi.responses import StreamingResponse

from gsuid_core.gss import gss
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.data_store import image_res
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.database.models import CoreUser, CoreGroup
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

# 图片清理配置
is_clean_pic = pic_upload_config.get_config("EnableCleanPicSrv").data
pic_expire_time = pic_upload_config.get_config("ScheduledCleanPicSrv").data


@app.post("/api/BatchPush")
async def batch_push(request: Request, data: Dict, _: Dict = Depends(require_auth)):
    """
    批量消息推送接口
    支持解析 HTML（提取 <p> 和 <img>），并向特定 Bot 下的
    "所有用户(ALLUSER)"、"所有群(ALLGROUP)"或指定的群/用户循环发送群发消息
    """
    send_msg = data["push_text"]
    soup = BeautifulSoup(send_msg, "lxml")

    msg: List[Message] = []
    text_list: List[Tag] = list(soup.find_all("p"))  # type: ignore
    for text in text_list:
        msg.append(MessageSegment.text(str(text)[3:-4] + "\n"))

    img_tag: List[Tag] = list(soup.find_all("img"))  # type: ignore
    for img in img_tag:
        src: str = img.get("src")  # type: ignore
        width: str = img.get("width")  # type: ignore
        height: str = img.get("height")  # type: ignore

        base64_data = "base64://" + src.split(",")[-1]

        msg.append(MessageSegment.image(base64_data))
        msg.append(MessageSegment.image_size((int(width), int(height))))

    send_target: List[str] = data["push_tag"].split(",")
    push_bots: List[str] = data["push_bot"].split(",")
    user_sends: Dict[str, List[str]] = {}
    group_sends: Dict[str, List[str]] = {}

    if "ALLUSER" in send_target:
        all_user = await CoreUser.get_all_user()
        if all_user:
            for user in all_user:
                if user.bot_id not in user_sends:
                    user_sends[user.bot_id] = [user.user_id]
                else:
                    if user.user_id not in user_sends[user.bot_id]:
                        user_sends[user.bot_id].append(user.user_id)
        send_target.remove("ALLUSER")

    if "ALLGROUP" in send_target:
        all_group = await CoreGroup.get_all_group()
        if all_group:
            for group in all_group:
                if group.bot_id not in group_sends:
                    group_sends[group.bot_id] = [group.group_id]
                else:
                    if group.group_id not in group_sends[group.bot_id]:
                        group_sends[group.bot_id].append(group.group_id)
        send_target.remove("ALLGROUP")

    for _target in send_target:
        if "|" not in _target:
            continue
        targets = _target.split("|")
        target, bot_id = targets[0], targets[1]
        if target.startswith("g:"):
            group_id = target.split(":")[1]
            if bot_id not in group_sends:
                group_sends[bot_id] = [group_id]
            else:
                if group_id not in group_sends[bot_id]:
                    group_sends[bot_id].append(group_id)
        else:
            user_id = target.split(":")[1]
            if bot_id not in user_sends:
                user_sends[bot_id] = [user_id]
            else:
                if user_id not in user_sends[bot_id]:
                    user_sends[bot_id].append(user_id)

    s = [group_sends, user_sends]
    for BOT_ID in gss.active_bot:
        if BOT_ID not in push_bots:
            continue
        for index, sends in enumerate(s):
            send_type = "group" if index == 0 else "direct"
            for bot_id in sends:
                for uuid in sends[bot_id]:
                    if index == 0:
                        msg.append(Message("group", uuid))
                    await gss.active_bot[BOT_ID].target_send(
                        msg,
                        send_type,
                        uuid,
                        bot_id,
                        "",
                        "",
                    )

    return {"status": 0, "msg": "推送成功！", "data": "推送成功！"}


# ===================
# 图片文件管理接口
# ===================


@app.post("/api/uploadImage/{suffix}/{filename}/{UPLOAD_PATH:path}")
async def upload_image(
    request: Request,
    UPLOAD_PATH: str,
    file: UploadFile,
    filename: Optional[str] = None,
    suffix: Optional[str] = None,
    _: Dict = Depends(require_auth),
):
    """
    通用图片文件上传接口
    允许向服务器指定的物理路径（UPLOAD_PATH）上传并保存图片文件
    """
    path = Path(UPLOAD_PATH)
    # 利用uuid保存图片
    file_name = file.filename
    if not filename:
        if file_name:
            file_name = file_name.split(".")[-1]
            file_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}.{file_name}"
        else:
            file_name = "image.jpg"
    else:
        if suffix:
            file_name = f"{filename}.{suffix}"
        else:
            file_name = f"{filename}.jpg"

    file_path = path / file_name
    if not file_path.parent.exists():
        file_path.parent.mkdir(parents=True)
    async with aiofiles.open(file_path, "wb") as f:
        content = await file.read()
        await f.write(content)

    return {"status": 0, "msg": "上传成功", "data": {"filename": file_name}}


@app.get("/api/getImage/{suffix}/{filename}/{IMAGE_PATH:path}")
async def get_image(
    request: Request,
    IMAGE_PATH: str,
    filename: str,
    suffix: str = "str",
    _: Dict = Depends(require_auth),
):
    """
    通用图片文件读取接口
    从指定的物理路径（IMAGE_PATH）读取并返回图片流
    """
    path = Path(IMAGE_PATH)
    file_path = path / f"{filename}.{suffix}"
    if not file_path.exists():
        return Response(status_code=404)

    # 返回URL
    return Response(
        content=file_path.read_bytes(),
        media_type="image/jpeg",
    )


# ===================
# 图片资源读取及"阅后即焚"接口
# ===================


async def delete_image(image_path: Path):
    """异步定时删除图片"""
    await asyncio.sleep(int(pic_expire_time))
    if image_path.exists():
        image_path.unlink()


@app.head("/api/image/{image_id}")
@app.get("/api/image/{image_id}")
async def get_resource_image(
    image_id: str,
    background_tasks: BackgroundTasks,
):
    """
    图片资源读取及"阅后即焚"接口
    专门用于从机器人的 image_res 缓存目录获取图片返回，
    并且内置了异步定时删除（阅后即焚）功能（基于配置项 is_clean_pic）
    """
    path = image_res / image_id
    if not path.exists() and "." not in image_id:
        path = image_res / f"{image_id}.jpg"

    if not path.exists():
        return Response(status_code=404)

    # 根据实际图片格式返回正确的媒体类型
    image = Image.open(path)
    suffix = path.suffix.lower()

    if suffix == ".gif":
        media_type = "image/gif"
        # GIF直接读取原始字节
        image_bytes = path.read_bytes()
    else:
        media_type = "image/jpeg"
        # 转换为 JPEG
        image_bytes_io = BytesIO()
        image.convert("RGB").save(image_bytes_io, format="JPEG")
        image_bytes_io.seek(0)
        image_bytes = image_bytes_io.getvalue()

    response = StreamingResponse(
        iter([image_bytes]),
        media_type=media_type,
    )
    if is_clean_pic:
        asyncio.create_task(delete_image(path))
    return response
