"""
Message APIs
提供消息推送相关的 RESTful APIs
包括批量消息推送功能
"""

import asyncio
from io import BytesIO
from typing import Any, Dict, List, Optional
from pathlib import Path
from datetime import datetime

import aiofiles
from PIL import Image
from fastapi import Query, Depends, Request, Response, UploadFile, BackgroundTasks
from fastapi.responses import StreamingResponse

from gsuid_core.gss import gss
from gsuid_core.i18n import t
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.data_store import image_res
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.database.models import CoreUser, CoreGroup
from gsuid_core.utils.plugins_config.gs_config import pic_upload_config

from ._api_tags import MESSAGE

# 图片清理配置
is_clean_pic = pic_upload_config.get_config("EnableCleanPicSrv").data
pic_expire_time = pic_upload_config.get_config("ScheduledCleanPicSrv").data


@app.post("/api/BatchPush", summary="批量推送", tags=MESSAGE)
async def batch_push(request: Request, data: Dict[str, Any], _: Dict[str, Any] = Depends(require_auth)):
    """
    批量消息推送接口
    支持解析 HTML（提取 <p> 和 <img>），并向特定 Bot 下的
    "所有用户(ALLUSER)"、"所有群(ALLGROUP)"或指定的群/用户循环发送群发消息
    """
    from bs4 import Tag, BeautifulSoup  # 仅本接口用，+约5MB，按需导入

    send_msg = data["push_text"]
    soup = BeautifulSoup(send_msg, "lxml")

    msg: List[Message] = []
    text_list: List[Tag] = list(soup.find_all("p"))
    for text in text_list:
        msg.append(MessageSegment.text(str(text)[3:-4] + "\n"))

    img_tag: List[Tag] = list(soup.find_all("img"))
    for img in img_tag:
        src = img.get("src")
        width = img.get("width")
        height = img.get("height")
        # bs4 属性取值为 str | list[str] | None，非 str（缺失/多值）直接跳过该图
        if not (isinstance(src, str) and isinstance(width, str) and isinstance(height, str)):
            continue

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

    return {"status": 0, "msg": t("log.webconsole.batch_push.success"), "data": t("log.webconsole.batch_push.success")}


@app.get(
    "/api/BatchPush/targets",
    summary="拉取批量推送可选目标（分页+筛选）",
    tags=MESSAGE,
)
async def batch_push_targets(
    _: Dict[str, Any] = Depends(require_auth),
    bot_id: Optional[str] = Query(None, description="按 bot_id 过滤（空=全部）"),
    kind: Optional[str] = Query(None, description="类型筛选：all | group | user（默认 all）"),
    q: Optional[str] = Query(None, description="模糊搜索，匹配 label 或 value（不区分大小写）"),
    limit: int = Query(200, ge=1, le=1000, description="单页大小（1-1000）"),
    offset: int = Query(0, ge=0, description="页偏移"),
) -> Dict[str, Any]:
    """为 /batch-push 前端页面提供可选目标（分页+筛选）。

    返回：
    - `bots`：当前所有 active_bot（仅展示 WS_BOT_ID）
    - `items`：当前筛选条件下、按 (kind, bot_id, id) 稳定排序后的目标分页
    - `total` / `has_more`：用于前端分页 UI

    每条 item：
    - `kind`：`group` / `user` / `macro`
    - `bot_id`：所属 bot（宏为空字符串）
    - `label`：人类可读标签
    - `value`：后端拼接的 `g:{group_id}|{bot_id}` / `u:{user_id}|{bot_id}` 宏

    ALLGROUP / ALLUSER 宏仅在 `bot_id` 未指定时、`offset == 0` 时按需返回一次。
    在带 bot_id 过滤时隐藏宏（宏会展开到所有 bot，与当前筛选范围冲突）。
    """
    bots: List[Dict[str, Any]] = [{"bot_id": ws_bot_id, "name": str(ws_bot_id)} for ws_bot_id in (gss.active_bot or {})]

    # ---- 构建 groups / users 列表（去重 + 可选 bot_id / q 过滤）----
    group_items: List[Dict[str, Any]] = []
    q_lower = q.lower() if q else None
    all_group = await CoreGroup.get_all_group()
    if all_group:
        seen: set = set()
        for g in all_group:
            if bot_id and g.bot_id != bot_id:
                continue
            key = (g.bot_id, g.group_id)
            if key in seen:
                continue
            label = f"{g.bot_id} · {g.group_id}"
            value = f"g:{g.group_id}|{g.bot_id}"
            if q_lower and q_lower not in label.lower() and q_lower not in value.lower():
                continue
            seen.add(key)
            group_items.append({"kind": "group", "bot_id": g.bot_id, "label": label, "value": value})
    # 稳定排序：(bot_id, value)；后端无 created_at 字段，按 value 升序足够稳定
    group_items.sort(key=lambda x: (x["bot_id"], x["value"]))

    user_items: List[Dict[str, Any]] = []
    all_user = await CoreUser.get_all_user()
    if all_user:
        seen = set()
        for u in all_user:
            if bot_id and u.bot_id != bot_id:
                continue
            key = (u.bot_id, u.user_id)
            if key in seen:
                continue
            label = f"{u.bot_id} · {u.user_id}"
            value = f"u:{u.user_id}|{u.bot_id}"
            if q_lower and q_lower not in label.lower() and q_lower not in value.lower():
                continue
            seen.add(key)
            user_items.append({"kind": "user", "bot_id": u.bot_id, "label": label, "value": value})
    user_items.sort(key=lambda x: (x["bot_id"], x["value"]))

    # ---- 宏只在「无 bot 筛选 + 第一页」按 kind 返回一次----
    macros: List[Dict[str, Any]] = []
    if not bot_id and offset == 0:
        all_groups_label = t("log.webconsole.batch_push.all_groups")
        all_users_label = t("log.webconsole.batch_push.all_users")
        if kind in (None, "all", "group"):
            if not q_lower or q_lower in all_groups_label.lower():
                macros.append({"kind": "macro", "bot_id": "", "label": all_groups_label, "value": "ALLGROUP"})
        if kind in (None, "all", "user"):
            if not q_lower or q_lower in all_users_label.lower():
                macros.append({"kind": "macro", "bot_id": "", "label": all_users_label, "value": "ALLUSER"})

    # ---- 按 kind 拼接 + 分页----
    if kind == "group":
        all_items = macros + group_items
    elif kind == "user":
        all_items = macros + user_items
    else:
        all_items = macros + group_items + user_items

    total = len(all_items)
    page_items = all_items[offset : offset + limit]
    has_more = (offset + limit) < total

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "bots": bots,
            "items": page_items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
        },
    }


# ===================
# 图片文件管理接口
# ===================


@app.post("/api/uploadImage/{suffix}/{filename}/{UPLOAD_PATH:path}", summary="通用图片上传", tags=MESSAGE)
async def upload_image(
    request: Request,
    UPLOAD_PATH: str,
    file: UploadFile,
    filename: Optional[str] = None,
    suffix: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
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


@app.get("/api/getImage/{suffix}/{filename}/{IMAGE_PATH:path}", summary="通用图片读取", tags=MESSAGE)
async def get_image(
    request: Request,
    IMAGE_PATH: str,
    filename: str,
    suffix: str = "str",
    _: Dict[str, Any] = Depends(require_auth),
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
@app.get("/api/image/{image_id}", summary="图片资源读取（阅后即焚）", tags=MESSAGE)
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
