"""Live Chat 状态持久化 API（控制台实时聊天）。

目录结构（``data/webconsole_live_chat/``）：

```
identity.json                 # 扮演身份
index.json                    # activeId + 会话元数据列表（不含 messages）
conversations/
  {sha256(id)[:32]}.json      # 单个会话全文（含 messages）；文件名碰撞安全
```

端点：
- GET    /api/live-chat/state                 组装完整状态
- PUT    /api/live-chat/state                 整包拆分写入
- PUT    /api/live-chat/identity              只写身份
- PUT    /api/live-chat/index                 只写索引（activeId + 元数据）
- PUT    /api/live-chat/conversations/{id}    只写单个会话
- DELETE /api/live-chat/conversations/{id}    删除单个会话
"""

from __future__ import annotations

import json
import asyncio
import hashlib
from typing import Any, Dict, List, Optional
from pathlib import Path

from fastapi import Depends
from pydantic import Field, BaseModel, field_validator
from boltons.fileutils import atomic_save

from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.data_store import (
    LIVE_CHAT_DIR,
    LIVE_CHAT_CONVS_DIR,
    LIVE_CHAT_INDEX_PATH,
    LIVE_CHAT_IDENTITY_PATH,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import LIVE_CHAT

_MAX_MESSAGES_PER_CONV = 200
_MAX_CONVERSATIONS = 100
_READ_ERRORS = (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError)
_WRITE_ERRORS = (OSError, TypeError, ValueError)

_lock = asyncio.Lock()


# ============================================================
# Models
# ============================================================


class LiveChatIdentityModel(BaseModel):
    userId: str = Field(default="master", max_length=128)
    nickname: str = Field(default="Master", max_length=128)
    avatar: str = Field(default="", max_length=2048)
    botSelfId: str = Field(default="webconsole_bot", max_length=128)


class LiveChatConversationMetaModel(BaseModel):
    """侧栏索引元数据（无 messages）。"""

    id: str = Field(min_length=1, max_length=256)
    type: str = Field(default="direct")
    targetId: str = Field(default="", max_length=256)
    name: str = Field(default="", max_length=256)
    updatedAt: int = Field(default=0)
    lastPreview: Optional[str] = Field(default=None, max_length=512)

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v: str) -> str:
        if v not in {"group", "direct"}:
            return "direct"
        return v


class LiveChatConversationModel(LiveChatConversationMetaModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list)


class LiveChatStateModel(BaseModel):
    identity: LiveChatIdentityModel = Field(default_factory=LiveChatIdentityModel)
    conversations: List[LiveChatConversationModel] = Field(default_factory=list)
    activeId: Optional[str] = Field(default=None, max_length=256)


class LiveChatIndexModel(BaseModel):
    """仅索引：不含 messages，供侧栏列表快速读写。"""

    activeId: Optional[str] = Field(default=None, max_length=256)
    conversations: List[LiveChatConversationMetaModel] = Field(default_factory=list)


DEFAULT_IDENTITY: Dict[str, Any] = {
    "userId": "master",
    "nickname": "Master",
    "avatar": "",
    "botSelfId": "webconsole_bot",
}

DEFAULT_INDEX: Dict[str, Any] = {
    "activeId": None,
    "conversations": [],
}


# ============================================================
# Path / IO helpers（同步实现 + to_thread）
# ============================================================


def _safe_filename(conv_id: str) -> str:
    """会话 id → 碰撞安全文件名（sha256 前 32 hex）。"""
    return hashlib.sha256(conv_id.encode("utf-8")).hexdigest()[:32]


def _conv_path(conv_id: str) -> Path:
    return LIVE_CHAT_CONVS_DIR / f"{_safe_filename(conv_id)}.json"


@to_thread
def _ensure_dirs() -> None:
    LIVE_CHAT_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_CHAT_CONVS_DIR.mkdir(parents=True, exist_ok=True)


@to_thread
def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_save(str(path), text_mode=True, file_perms=None, overwrite=True) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@to_thread
def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@to_thread
def _path_exists(path: Path) -> bool:
    return path.exists()


@to_thread
def _unlink_path(path: Path) -> None:
    if path.exists():
        path.unlink()


@to_thread
def _list_conv_json_names() -> List[str]:
    if not LIVE_CHAT_CONVS_DIR.is_dir():
        return []
    return [p.name for p in LIVE_CHAT_CONVS_DIR.glob("*.json")]


# ============================================================
# Normalize（磁盘脏数据边界：isinstance 后直接访问）
# ============================================================


def _normalize_identity(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(DEFAULT_IDENTITY)
    out = dict(DEFAULT_IDENTITY)
    for k in DEFAULT_IDENTITY:
        if k in raw and raw[k] is not None:
            out[k] = str(raw[k])[: (2048 if k == "avatar" else 128)]
    return out


def _meta_from_conv(c: Dict[str, Any]) -> Dict[str, Any]:
    """从完整会话抽出索引元数据（无 messages）。"""
    raw_type = c["type"] if "type" in c else "direct"
    ctype = raw_type if raw_type in {"group", "direct"} else "direct"
    cid = str(c["id"])[:256] if "id" in c and c["id"] is not None else ""
    target = str(c["targetId"])[:256] if "targetId" in c and c["targetId"] is not None else ""
    name_raw = c["name"] if "name" in c and c["name"] is not None else target
    updated = c["updatedAt"] if "updatedAt" in c and c["updatedAt"] is not None else 0
    preview: Optional[str]
    if "lastPreview" in c and c["lastPreview"] is not None:
        preview = str(c["lastPreview"])[:512]
    else:
        preview = None
    return {
        "id": cid,
        "type": ctype,
        "targetId": target,
        "name": str(name_raw)[:256],
        "updatedAt": int(updated),
        "lastPreview": preview,
    }


def _normalize_conversation(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or "id" not in raw or not raw["id"]:
        return None
    msgs = raw["messages"] if "messages" in raw and isinstance(raw["messages"], list) else []
    if len(msgs) > _MAX_MESSAGES_PER_CONV:
        msgs = msgs[-_MAX_MESSAGES_PER_CONV:]
    meta = _meta_from_conv(raw)
    if not meta["id"]:
        return None
    return {**meta, "messages": msgs}


def _normalize_index(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return dict(DEFAULT_INDEX)
    items: List[Dict[str, Any]] = []
    convs_in = raw["conversations"] if "conversations" in raw else None
    if isinstance(convs_in, list):
        for c in convs_in[:_MAX_CONVERSATIONS]:
            if not isinstance(c, dict) or "id" not in c or not c["id"]:
                continue
            items.append(_meta_from_conv(c))
    active: Optional[str]
    if "activeId" in raw and raw["activeId"] is not None:
        active = str(raw["activeId"])[:256]
    else:
        active = None
    id_set = {m["id"] for m in items}
    if active and active not in id_set:
        active = items[0]["id"] if items else None
    return {"activeId": active, "conversations": items}


# ============================================================
# Load / Save（调用方持 _lock）
# ============================================================


async def load_identity() -> Dict[str, Any]:
    if not await _path_exists(LIVE_CHAT_IDENTITY_PATH):
        return dict(DEFAULT_IDENTITY)
    try:
        return _normalize_identity(await _read_json(LIVE_CHAT_IDENTITY_PATH))
    except _READ_ERRORS as e:
        logger.warning("[live-chat] read identity fail: %s", e)
        return dict(DEFAULT_IDENTITY)


async def save_identity(identity: Dict[str, Any]) -> bool:
    try:
        await _atomic_write_json(LIVE_CHAT_IDENTITY_PATH, _normalize_identity(identity))
        return True
    except _WRITE_ERRORS as e:
        logger.warning("[live-chat] save identity fail: %s", e)
        return False


async def load_index() -> Dict[str, Any]:
    if not await _path_exists(LIVE_CHAT_INDEX_PATH):
        return dict(DEFAULT_INDEX)
    try:
        return _normalize_index(await _read_json(LIVE_CHAT_INDEX_PATH))
    except _READ_ERRORS as e:
        logger.warning("[live-chat] read index fail: %s", e)
        return dict(DEFAULT_INDEX)


async def save_index(index: Dict[str, Any]) -> bool:
    try:
        await _atomic_write_json(LIVE_CHAT_INDEX_PATH, _normalize_index(index))
        return True
    except _WRITE_ERRORS as e:
        logger.warning("[live-chat] save index fail: %s", e)
        return False


async def load_conversation(conv_id: str) -> Optional[Dict[str, Any]]:
    path = _conv_path(conv_id)
    try:
        if not await _path_exists(path):
            return None
        return _normalize_conversation(await _read_json(path))
    except _READ_ERRORS as e:
        logger.warning("[live-chat] read conv %s fail: %s", conv_id, e)
        return None


async def save_conversation(conv: Dict[str, Any]) -> bool:
    try:
        normalized = _normalize_conversation(conv)
        if normalized is None:
            return False
        await _atomic_write_json(_conv_path(normalized["id"]), normalized)
        return True
    except _WRITE_ERRORS as e:
        logger.warning("[live-chat] save conv fail: %s", e)
        return False


async def delete_conversation(conv_id: str) -> bool:
    """先更新 index，再删会话文件；任一步失败返回 False。"""
    try:
        idx = await load_index()
        convs = idx["conversations"] if "conversations" in idx else []
        new_list = [c for c in convs if isinstance(c, dict) and c["id"] != conv_id]
        idx["conversations"] = new_list
        active = idx["activeId"] if "activeId" in idx else None
        if active == conv_id:
            idx["activeId"] = new_list[0]["id"] if new_list else None
        if not await save_index(idx):
            return False

        path = _conv_path(conv_id)
        if await _path_exists(path):
            await _unlink_path(path)
        return True
    except _READ_ERRORS + _WRITE_ERRORS as e:
        logger.warning("[live-chat] delete conv fail: %s", e)
        return False


async def load_live_chat_state() -> Dict[str, Any]:
    """组装完整状态：identity + index 元数据 + 各会话 messages。"""
    await _ensure_dirs()

    identity = await load_identity()
    index = await load_index()
    conversations: List[Dict[str, Any]] = []

    raw_convs = index["conversations"] if "conversations" in index else []
    for meta in raw_convs:
        if not isinstance(meta, dict) or "id" not in meta or not meta["id"]:
            continue
        cid = str(meta["id"])
        full = await load_conversation(cid)
        if full is not None:
            merged = {
                **full,
                "name": meta["name"] if "name" in meta and meta["name"] else full["name"],
                "updatedAt": (meta["updatedAt"] if "updatedAt" in meta and meta["updatedAt"] else full["updatedAt"]),
                "lastPreview": (
                    meta["lastPreview"]
                    if "lastPreview" in meta and meta["lastPreview"] is not None
                    else full["lastPreview"]
                ),
                "type": meta["type"] if "type" in meta and meta["type"] else full["type"],
                "targetId": (meta["targetId"] if "targetId" in meta and meta["targetId"] else full["targetId"]),
            }
            conversations.append(merged)
        else:
            conversations.append({**_meta_from_conv(meta), "messages": []})

    active = index["activeId"] if "activeId" in index else None
    if active and not any(c["id"] == active for c in conversations):
        active = conversations[0]["id"] if conversations else None

    return {
        "identity": identity,
        "conversations": conversations,
        "activeId": active,
    }


async def save_live_chat_state(state: Dict[str, Any]) -> bool:
    """写序：会话文件 → index → identity；最后清理孤儿会话文件。"""
    try:
        await _ensure_dirs()

        identity = _normalize_identity(state["identity"] if "identity" in state else None)
        convs_raw = (
            state["conversations"] if "conversations" in state and isinstance(state["conversations"], list) else []
        )
        conversations: List[Dict[str, Any]] = []
        for c in convs_raw[:_MAX_CONVERSATIONS]:
            n = _normalize_conversation(c)
            if n is not None:
                conversations.append(n)

        if "activeId" in state and state["activeId"] is not None:
            active: Optional[str] = str(state["activeId"])[:256]
        else:
            active = None
        id_set = {c["id"] for c in conversations}
        if active and active not in id_set:
            active = conversations[0]["id"] if conversations else None

        # 1) 每个会话
        keep_names: set[str] = set()
        for c in conversations:
            fname = f"{_safe_filename(c['id'])}.json"
            keep_names.add(fname)
            await _atomic_write_json(LIVE_CHAT_CONVS_DIR / fname, c)

        # 2) index
        index = {
            "activeId": active,
            "conversations": [_meta_from_conv(c) for c in conversations],
        }
        await _atomic_write_json(LIVE_CHAT_INDEX_PATH, index)

        # 3) identity
        await _atomic_write_json(LIVE_CHAT_IDENTITY_PATH, identity)

        # 4) 删孤儿
        for name in await _list_conv_json_names():
            if name not in keep_names:
                try:
                    await _unlink_path(LIVE_CHAT_CONVS_DIR / name)
                except OSError as e:
                    logger.warning("[live-chat] unlink orphan %s: %s", name, e)
        return True
    except _READ_ERRORS + _WRITE_ERRORS as e:
        logger.warning("[live-chat] save state fail: %s", e)
        return False


# ============================================================
# Routes
# ============================================================


@app.get("/api/live-chat/state", summary="读取 Live Chat 完整状态", tags=LIVE_CHAT)
async def get_live_chat_state(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    async with _lock:
        data = await load_live_chat_state()
    return {"status": 0, "msg": "ok", "data": data}


@app.put("/api/live-chat/state", summary="保存 Live Chat 完整状态（拆分多文件）", tags=LIVE_CHAT)
async def put_live_chat_state(
    body: LiveChatStateModel,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    payload = body.model_dump()
    async with _lock:
        ok = await save_live_chat_state(payload)
        if not ok:
            return {"status": 1, "msg": "保存失败"}
        data = await load_live_chat_state()
    return {"status": 0, "msg": "ok", "data": data}


@app.put("/api/live-chat/identity", summary="只保存身份", tags=LIVE_CHAT)
async def put_live_chat_identity(
    body: LiveChatIdentityModel,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    async with _lock:
        ok = await save_identity(body.model_dump())
        if not ok:
            return {"status": 1, "msg": "保存失败"}
        data = await load_identity()
    return {"status": 0, "msg": "ok", "data": data}


@app.put("/api/live-chat/index", summary="只保存索引（侧栏元数据 + activeId）", tags=LIVE_CHAT)
async def put_live_chat_index(
    body: LiveChatIndexModel,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    async with _lock:
        ok = await save_index(body.model_dump())
        if not ok:
            return {"status": 1, "msg": "保存失败"}
        data = await load_index()
    return {"status": 0, "msg": "ok", "data": data}


@app.put(
    "/api/live-chat/conversations/{conv_id}",
    summary="保存单个会话（含 messages）",
    tags=LIVE_CHAT,
)
async def put_live_chat_conversation(
    conv_id: str,
    body: LiveChatConversationModel,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    data = body.model_dump()
    data["id"] = conv_id
    async with _lock:
        if not await save_conversation(data):
            return {"status": 1, "msg": "保存失败"}
        idx = await load_index()
        meta = _meta_from_conv(data)
        found = False
        new_list: List[Dict[str, Any]] = []
        for c in idx["conversations"] if "conversations" in idx else []:
            if not isinstance(c, dict):
                continue
            if c["id"] == conv_id:
                new_list.append(meta)
                found = True
            else:
                new_list.append(c)
        if not found:
            new_list.insert(0, meta)
        idx["conversations"] = new_list[:_MAX_CONVERSATIONS]
        if not await save_index(idx):
            return {"status": 1, "msg": "会话已写但索引更新失败"}
        saved = await load_conversation(conv_id)
    return {"status": 0, "msg": "ok", "data": saved}


@app.delete(
    "/api/live-chat/conversations/{conv_id}",
    summary="删除单个会话",
    tags=LIVE_CHAT,
)
async def delete_live_chat_conversation(
    conv_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    async with _lock:
        ok = await delete_conversation(conv_id)
    if not ok:
        return {"status": 1, "msg": "删除失败"}
    return {"status": 0, "msg": "ok", "data": None}
