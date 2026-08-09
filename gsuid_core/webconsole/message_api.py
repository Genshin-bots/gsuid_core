"""
Message APIs
提供消息推送相关的 RESTful APIs
包括批量消息推送功能
"""

import asyncio
from io import BytesIO
from typing import Any, Dict, List, Tuple, Optional, TypedDict
from pathlib import Path
from datetime import datetime

import aiofiles
from PIL import Image
from fastapi import Query, Depends, Request, Response, UploadFile, BackgroundTasks
from sqlalchemy.exc import SQLAlchemyError
from fastapi.responses import StreamingResponse

from gsuid_core.gss import gss
from gsuid_core.i18n import t
from gsuid_core.logger import logger
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


def _parse_push_bot_list(raw: Any) -> List[str]:
    """解析 push_bot：空字符串 / 缺省 = 全部 active bot；否则按逗号拆分。"""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _parse_push_bot_self_ids(raw: Any) -> List[str]:
    """解析 push_bot_self_id：支持单值或逗号分隔多值；空 = 不限制（传空串给适配器）。"""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _append_send_target(
    bucket: Dict[str, Dict[str, List[str]]],
    platform_bot_id: str,
    bot_self_id: str,
    target_id: str,
) -> None:
    """按 (platform_bot_id, bot_self_id) 聚合发送目标，去重 target_id。"""
    if platform_bot_id not in bucket:
        bucket[platform_bot_id] = {}
    if bot_self_id not in bucket[platform_bot_id]:
        bucket[platform_bot_id][bot_self_id] = []
    if target_id not in bucket[platform_bot_id][bot_self_id]:
        bucket[platform_bot_id][bot_self_id].append(target_id)


# kind, target_id, platform_bot_id, bot_self_id(None=回落全局)
_TargetToken = Tuple[str, str, str, Optional[str]]


def _parse_target_token(token: str) -> Optional[_TargetToken]:
    """解析 g:/u: 两段或三段 push_tag token；无第三段时 bot_self_id 为 None。"""
    token = token.strip()
    if not token or "|" not in token:
        return None
    parts = token.split("|")
    if len(parts) < 2:
        return None
    target, platform_bot_id = parts[0].strip(), parts[1].strip()
    bot_self_id: Optional[str] = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else None
    if not target or not platform_bot_id:
        return None
    if target.startswith("g:"):
        group_id = target.split(":", 1)[1].strip()
        if not group_id:
            return None
        return ("group", group_id, platform_bot_id, bot_self_id)
    if target.startswith("u:"):
        user_id = target.split(":", 1)[1].strip()
        if not user_id:
            return None
        return ("user", user_id, platform_bot_id, bot_self_id)
    # 兼容极老格式：无 g:/u: 前缀时按用户处理
    return ("user", target, platform_bot_id, bot_self_id)


@app.post("/api/BatchPush", summary="批量推送", tags=MESSAGE)
async def batch_push(request: Request, data: Dict[str, Any], _: Dict[str, Any] = Depends(require_auth)):
    """
    批量消息推送接口
    支持解析 HTML（提取 <p> 和 <img>），并向特定 Bot 下的
    "所有用户(ALLUSER)"、"所有群(ALLGROUP)"或指定的群/用户循环发送群发消息。

    请求字段：
    - ``push_text``: HTML 正文
    - ``push_tag``: 逗号分隔目标。格式：
      - ``ALLUSER`` / ``ALLGROUP``
      - ``g:{group_id}|{bot_id}`` 或 ``g:{group_id}|{bot_id}|{bot_self_id}``
      - ``u:{user_id}|{bot_id}`` 或 ``u:{user_id}|{bot_id}|{bot_self_id}``
    - ``push_bot``: 逗号分隔的 WS_BOT_ID（``gss.active_bot`` 的 key）；空 = 全部 active bot
    - ``push_bot_self_id``: 可选。逗号分隔的 bot_self_id；用于精准指定机器人账号。
      当 push_tag 某条目未写第三段时，会按此列表展开（多值则每个 self_id 各发一遍）。
      空则传空串给适配器（兼容旧行为）。
    """
    from bs4 import Tag, BeautifulSoup  # 仅本接口用，+约5MB，按需导入

    send_msg = data["push_text"]
    soup = BeautifulSoup(send_msg, "lxml")

    base_msg: List[Message] = []
    text_list = list(soup.find_all("p"))
    for text in text_list:
        base_msg.append(MessageSegment.text(str(text)[3:-4] + "\n"))

    img_tag = list(soup.find_all("img"))
    for img in img_tag:
        if not isinstance(img, Tag):
            continue
        src = img.get("src")
        width = img.get("width")
        height = img.get("height")
        # bs4 属性取值为 str | list[str] | None，非 str（缺失/多值）直接跳过该图
        if not (isinstance(src, str) and isinstance(width, str) and isinstance(height, str)):
            continue

        base64_data = "base64://" + src.split(",")[-1]

        base_msg.append(MessageSegment.image(base64_data))
        base_msg.append(MessageSegment.image_size((int(width), int(height))))

    send_target: List[str] = [x.strip() for x in str(data.get("push_tag", "")).split(",") if x.strip()]
    push_bots = _parse_push_bot_list(data.get("push_bot", ""))
    default_self_ids = _parse_push_bot_self_ids(data.get("push_bot_self_id", ""))
    # 未指定时用 [""] 保持旧行为：target_send(..., bot_self_id="")
    fallback_self_ids = default_self_ids if default_self_ids else [""]

    # platform_bot_id -> bot_self_id -> [target_id, ...]
    user_sends: Dict[str, Dict[str, List[str]]] = {}
    group_sends: Dict[str, Dict[str, List[str]]] = {}

    if "ALLUSER" in send_target:
        all_user = await CoreUser.get_all_user()
        if all_user:
            for user in all_user:
                for self_id in fallback_self_ids:
                    _append_send_target(user_sends, user.bot_id, self_id, user.user_id)
        send_target = [x for x in send_target if x != "ALLUSER"]

    if "ALLGROUP" in send_target:
        all_group = await CoreGroup.get_all_group()
        if all_group:
            for group in all_group:
                for self_id in fallback_self_ids:
                    _append_send_target(group_sends, group.bot_id, self_id, group.group_id)
        send_target = [x for x in send_target if x != "ALLGROUP"]

    for _target in send_target:
        parsed = _parse_target_token(_target)
        if not parsed:
            continue
        kind, target_id, platform_bot_id, token_self_id = parsed
        self_ids = [token_self_id] if token_self_id is not None else fallback_self_ids
        bucket = group_sends if kind == "group" else user_sends
        for self_id in self_ids:
            _append_send_target(bucket, platform_bot_id, self_id, target_id)

    active = gss.active_bot or {}
    for ws_bot_id, bot in active.items():
        # 空 push_bots = 全部 active；否则仅命中列表中的 WS_BOT_ID
        if push_bots and ws_bot_id not in push_bots:
            continue
        for index, sends in enumerate((group_sends, user_sends)):
            send_type = "group" if index == 0 else "direct"
            for platform_bot_id, by_self in sends.items():
                for bot_self_id, target_ids in by_self.items():
                    for uuid in target_ids:
                        # 每条独立拷贝，避免往共享 list 追加 group 段污染后续发送
                        payload: List[Message] = list(base_msg)
                        if index == 0:
                            payload.append(Message("group", uuid))
                        await bot.target_send(
                            payload,
                            send_type,
                            uuid,
                            platform_bot_id,
                            bot_self_id or "",
                            "",
                        )

    return {"status": 0, "msg": t("msg.webconsole.batch_push.success"), "data": t("msg.webconsole.batch_push.success")}


class _BotSelfIdItem(TypedDict):
    bot_id: str
    bot_self_id: str
    label: str
    id: str


async def _collect_batch_push_bot_self_ids() -> List[_BotSelfIdItem]:
    """汇总 bot_self_id 实例：CoreDataSummary + 历史 session，按 bot_id+self_id 去重。"""
    seen: set[tuple[str, str]] = set()
    out: List[_BotSelfIdItem] = []

    def _add(platform_bot_id: str, bot_self_id: str) -> None:
        platform_bot_id = platform_bot_id.strip()
        bot_self_id = bot_self_id.strip()
        if not platform_bot_id or not bot_self_id:
            return
        key = (platform_bot_id, bot_self_id)
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "bot_id": platform_bot_id,
                "bot_self_id": bot_self_id,
                "label": f"{bot_self_id} ({platform_bot_id})",
                "id": f"{bot_self_id}:{platform_bot_id}",
            }
        )

    from gsuid_core.utils.database.global_val_models import CoreDataSummary

    try:
        rows = await CoreDataSummary.get_all_bots()
        for row in rows:
            # get_all_bots 返回 [{"bot_id","bot_self_id"}, ...]
            bot_id = row["bot_id"] if "bot_id" in row else ""
            bot_self_id = row["bot_self_id"] if "bot_self_id" in row else ""
            _add(str(bot_id or ""), str(bot_self_id or ""))
    except SQLAlchemyError as e:
        logger.warning(t("log.webconsole.batch_push_bot_self_db_fail", e=e))

    from gsuid_core.message_history import get_history_manager

    manager = get_history_manager()
    for ev in manager.list_sessions():
        _add(ev.bot_id or "", ev.bot_self_id or "")

    out.sort(key=lambda x: (x["bot_id"], x["bot_self_id"]))
    return out


@app.get(
    "/api/BatchPush/targets",
    summary="拉取批量推送可选目标（分页+筛选）",
    tags=MESSAGE,
)
async def batch_push_targets(
    _: Dict[str, Any] = Depends(require_auth),
    bot_id: Optional[str] = Query(None, description="按平台 bot_id 过滤（空=全部）"),
    bot_self_id: Optional[str] = Query(
        None,
        description="按 bot_self_id 过滤（仅影响返回的 bot_self_ids 子集；目标表无此字段）",
    ),
    kind: Optional[str] = Query(None, description="类型筛选：all | group | user（默认 all）"),
    q: Optional[str] = Query(None, description="模糊搜索，匹配 label 或 value（不区分大小写）"),
    limit: int = Query(200, ge=1, le=1000, description="单页大小（1-1000）"),
    offset: int = Query(0, ge=0, description="页偏移"),
) -> Dict[str, Any]:
    """为 /batch-push 前端页面提供可选目标（分页+筛选）。

    返回：
    - `bots`：当前所有 active_bot（WS 连接维度，用于 push_bot）
    - `bot_self_ids`：已知机器人账号实例（bot_id + bot_self_id），用于精准指定 push_bot_self_id
    - `items`：当前筛选条件下、按 (kind, bot_id, id) 稳定排序后的目标分页
    - `total` / `has_more`：用于前端分页 UI

    每条 item：
    - `kind`：`group` / `user` / `macro`
    - `bot_id`：所属平台 bot（宏为空字符串）
    - `label`：人类可读标签
    - `value`：后端拼接的 `g:{group_id}|{bot_id}` / `u:{user_id}|{bot_id}` 宏
      （提交时前端可再追加 `|{bot_self_id}` 第三段做单条覆盖）

    ALLGROUP / ALLUSER 宏仅在 `bot_id` 未指定时、`offset == 0` 时按需返回一次。
    在带 bot_id 过滤时隐藏宏（宏会展开到所有 bot，与当前筛选范围冲突）。
    """
    bots: List[Dict[str, Any]] = []
    for ws_bot_id, bot in (gss.active_bot or {}).items():
        bots.append(
            {
                "bot_id": ws_bot_id,
                "name": str(ws_bot_id),
                "ws_bot_id": ws_bot_id,
                # _Bot.bot_id 即 WS key；平台 bot_id 在事件侧，这里无法可靠推断
                "connected": ws_bot_id in gss.active_ws,
            }
        )

    # bot_self_ids 始终返回全集（仅在显式传 bot_self_id 时收窄），
    # 避免「选中某平台账号 → bot_id 筛选 → 下拉只剩该平台 → 无法切到其它平台」的死锁。
    # items 的平台过滤仍由 bot_id 控制。
    bot_self_ids = await _collect_batch_push_bot_self_ids()
    if bot_self_id:
        bot_self_ids = [x for x in bot_self_ids if x["bot_self_id"] == bot_self_id]

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
            group_items.append(
                {
                    "kind": "group",
                    "bot_id": g.bot_id,
                    "bot_self_id": "",
                    "label": label,
                    "value": value,
                }
            )
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
            user_items.append(
                {
                    "kind": "user",
                    "bot_id": u.bot_id,
                    "bot_self_id": "",
                    "label": label,
                    "value": value,
                }
            )
    user_items.sort(key=lambda x: (x["bot_id"], x["value"]))

    # ---- 宏只在「无 bot 筛选 + 第一页」按 kind 返回一次----
    macros: List[Dict[str, Any]] = []
    if not bot_id and offset == 0:
        all_groups_label = t("msg.webconsole.batch_push.all_groups")
        all_users_label = t("msg.webconsole.batch_push.all_users")
        if kind in (None, "all", "group"):
            if not q_lower or q_lower in all_groups_label.lower():
                macros.append(
                    {
                        "kind": "macro",
                        "bot_id": "",
                        "bot_self_id": "",
                        "label": all_groups_label,
                        "value": "ALLGROUP",
                    }
                )
        if kind in (None, "all", "user"):
            if not q_lower or q_lower in all_users_label.lower():
                macros.append(
                    {
                        "kind": "macro",
                        "bot_id": "",
                        "bot_self_id": "",
                        "label": all_users_label,
                        "value": "ALLUSER",
                    }
                )

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
            "bot_self_ids": bot_self_ids,
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
