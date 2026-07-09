"""
Meme Management APIs
提供表情包管理相关的 RESTful APIs

包括表情包列表查询、详情获取、图片获取、更新、移动、删除、
手动上传、重新打标、统计概览、批量删除、批量导出/导入等功能。
"""

import io
import json
import zipfile
from typing import Any, Dict, List, Optional
from pathlib import Path
from datetime import datetime, timezone

from fastapi import File, Form, Depends, UploadFile
from pydantic import Field, BaseModel
from fastapi.responses import StreamingResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.meme.tagger import enqueue_tag, is_tagging_enabled
from gsuid_core.ai_core.meme.library import (
    MemeLibrary,
    _read_file,
    get_memes_base_path,
)
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

from ._api_tags import MEME

# ─────────────────────────────────────────────
# Pydantic 请求模型
# ─────────────────────────────────────────────


class MemeUpdateRequest(BaseModel):
    """更新表情包标签/描述请求"""

    description: Optional[str] = Field(None, max_length=500, description="描述文本")
    emotion_tags: Optional[List[str]] = Field(None, description="情绪标签列表")
    scene_tags: Optional[List[str]] = Field(None, description="场景标签列表")
    custom_tags: Optional[List[str]] = Field(None, description="自定义标签列表")
    persona_hint: Optional[str] = Field(None, max_length=64, description="归属提示")
    status: Optional[str] = Field(
        None,
        max_length=32,
        description=(
            "显式修改状态（如人工复核后从 rejected 恢复为 manual）；不传时 rejected 保持不变，其余状态置为 manual"
        ),
    )


# 允许通过 API 显式设置的状态
_VALID_MEME_STATUSES = ("pending", "tagged", "manual", "pending_manual", "rejected")
# 可进入向量索引（可被检索）的状态
_INDEXABLE_STATUSES = ("tagged", "manual")

# 预保留的 meme_id 名：与 `/api/meme/...` 静态子路径同名
# （例如 `/api/meme/personas`、`/api/meme/stats`）。用于在所有以 `{meme_id}`
# 为路径参数的路由（详情/编辑/删除/移动/重打标/图片）中防御性阻断
# 路由匹配顺序造成的静默吞招。与 mcp_config_api.py 的
# `MCP_STATIC_ROUTES` 同源。
# 实际优先依靠路由声明顺序（静态路由必须先于 `{meme_id}` 声明），
# 本集合作为最后一道防线以防未来版本调整匹配策略。
_RESERVED_MEME_IDS = frozenset(
    {
        "personas",
        "list",
        "stats",
        "image",
        "upload",
        "export",
        "import",
        "batch_delete",
        "purge_rejected",
        "batch_retag_pending",
    }
)


def _folder_for_persona(persona_hint: str) -> str:
    """根据 persona_hint 推导归属文件夹（folder 是 persona 路由的代理）"""
    if not persona_hint or persona_hint == "common":
        return "common"
    return f"persona_{persona_hint}"


class MemeMoveRequest(BaseModel):
    """移动表情包请求"""

    target_folder: str = Field(..., min_length=1, max_length=128, description="目标文件夹名")


class MemeBatchDeleteRequest(BaseModel):
    """批量删除表情包请求"""

    meme_ids: List[str] = Field(..., min_length=1, description="要删除的表情包 ID 列表")


class MemeBatchExportRequest(BaseModel):
    """批量导出表情包请求"""

    meme_ids: Optional[List[str]] = Field(None, description="要导出的表情包 ID 列表，为空则导出全部")
    folder: Optional[str] = Field(None, description="按文件夹导出（与 meme_ids 互斥，优先使用 meme_ids）")


# ── .meme 包格式常量 ──
MEME_FORMAT_VERSION = "1.0"
MEME_MANIFEST_FILE = "manifest.json"
MEME_METADATA_FILE = "metadata.json"
MEME_FILES_DIR = "files"


# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────


def _record_to_dict(record: AiMemeRecord) -> Dict[str, Any]:
    """将 AiMemeRecord 转换为 API 响应字典"""
    return {
        "meme_id": record.meme_id,
        "file_path": record.file_path,
        "file_size": record.file_size,
        "file_mime": record.file_mime,
        "width": record.width,
        "height": record.height,
        "source_group": record.source_group,
        "folder": record.folder,
        "persona_hint": record.persona_hint,
        "emotion_tags": record.emotion_tags,
        "scene_tags": record.scene_tags,
        "description": record.description,
        "custom_tags": record.custom_tags,
        "status": record.status,
        "nsfw_score": record.nsfw_score,
        "use_count": record.use_count,
        "last_used_at": str(record.last_used_at) if record.last_used_at else None,
        "last_used_group": record.last_used_group,
        "created_at": str(record.created_at),
        "tagged_at": str(record.tagged_at) if record.tagged_at else None,
        "updated_at": str(record.updated_at),
    }


# ─────────────────────────────────────────────
# 1. 列表查询
# ─────────────────────────────────────────────


@app.get("/api/meme/list", summary="列表查询", tags=MEME)
async def get_meme_list(
    folder: Optional[str] = None,
    persona_hint: Optional[str] = None,
    status: Optional[str] = None,
    sort: str = "created_at_desc",
    page: int = 1,
    page_size: int = 20,
    q: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    列表查询表情包

    Args:
        folder: 文件夹过滤（如 `common` / `persona_xxx`），与 `persona_hint` 互斥，优先使用 `folder`
        persona_hint: 按人格分类过滤（如 `common` / 某 persona 名）。
            传 `persona_hint` 时，底层换算为对应 folder（`common` / `persona_{name}`）
            走按人格查询路径；
        status: 状态过滤
        sort: 排序方式
        page: 页码
        page_size: 每页数量
        q: 搜索关键词（语义向量检索）。与 `folder` / `persona_hint` 同时存在时，
            检索结果会被限制在指定 folder 内，避免跨 persona 串味。

    Returns:
        status: 0成功，1失败
        data: 包含 records、total、page、page_size 的分页结果
    """
    try:
        # folder 与 persona_hint 互斥优先：folder 优先；未给 folder 时按 persona_hint 换算
        effective_folder = folder
        if effective_folder is None and persona_hint is not None:
            effective_folder = _folder_for_persona(persona_hint)

        if q:
            # 语义向量检索
            records = await MemeLibrary.search_by_text(
                q,
                folder=effective_folder,
                top_k=page_size * 5,
            )
            # 手动分页
            total = len(records)
            start = (page - 1) * page_size
            end = start + page_size
            page_records = records[start:end]
        elif effective_folder:
            # 按文件夹查询（folder 与 persona_hint 都路由到这里）
            page_records, total = await AiMemeRecord.get_by_folder(
                folder=effective_folder,
                status=status,
                sort=sort,
                page=page,
                page_size=page_size,
            )
        else:
            # 查询所有记录
            page_records, total = await AiMemeRecord.get_all_records(
                status=status,
                sort=sort,
                page=page,
                page_size=page_size,
            )

        records_data = [_record_to_dict(r) for r in page_records]

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "records": records_data,
                "total": total,
                "page": page,
                "page_size": page_size,
            },
        }
    except Exception as e:
        return {"status": 1, "msg": f"查询失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 1b. 按人格分类列表（前端“按人格分类”入口用）
# ─────────────────────────────────────────────
#
# 【路由顺序重要】本端点必须声明在 `/api/meme/{meme_id}` 之前。
# Starlette / FastAPI 按声明顺序匹配路由，单段静态路径 `/api/meme/personas`
# 如果声明在后，会被 `/api/meme/{meme_id}` 吞掉并被当作 `meme_id="personas"`
# 去查询表情包记录，从而返回“表情包不存在”。同问题在 mcp_config_api.py
# 中以 `MCP_STATIC_ROUTES` 白名单与路由顺序记录过。
# （保留在 1b 节仅为可读性，实际位置是控制：声明顺序先于 2）


@app.get("/api/meme/personas", summary="a. 按人格分类列表（前端“按人格分类”入口）", tags=MEME)
async def get_meme_personas(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取表情包库内的所有人格分类及数量

    返回按表情包数量降序、人格名升序排列的列表：
    [
        {"persona_hint": "common", "count": 300, "folder": "common"},
        {"persona_hint": "早柚",   "count": 100, "folder": "persona_早柚"},
        ...
    ]

    实用场景：前端管理页顶部提供一个“按人格分类”的过滤器 / 侧边栏，
    调用本接口拿到所有出现过的人格分类及其数量，再点击某项调用
    `GET /api/meme/list?persona_hint=...` 获取该人格下的表情包列表。

    Returns:
        status: 0成功，1失败
        data: 人格分类列表
    """
    try:
        personas = await AiMemeRecord.get_distinct_personas()
        return {"status": 0, "msg": "ok", "data": personas}
    except Exception as e:
        return {"status": 1, "msg": f"获取人格分类失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 2. 获取单条记录详情
# ─────────────────────────────────────────────


@app.get("/api/meme/{meme_id}", summary="获取单条记录详情", tags=MEME)
async def get_meme_detail(
    meme_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取单条表情包详情

    Args:
        meme_id: 表情包 ID（sha256 前 16 位）

    Returns:
        status: 0成功，1失败
        data: 表情包详情
    """
    try:
        # 防御性保护：预保留名不当作 meme_id 查询
        if meme_id in _RESERVED_MEME_IDS:
            return {
                "status": 1,
                "msg": f"预保留路径名: {meme_id}（不属于任何表情包）",
                "data": None,
            }
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if not record:
            return {"status": 1, "msg": "表情包不存在", "data": None}

        return {"status": 0, "msg": "ok", "data": _record_to_dict(record)}
    except Exception as e:
        return {"status": 1, "msg": f"查询失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 3. 获取原始图片文件
# ─────────────────────────────────────────────


@app.get("/api/meme/image/{meme_id}", summary="获取原始图片文件", tags=MEME)
async def get_meme_image(
    meme_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> StreamingResponse:
    """
    获取原始图片文件

    Args:
        meme_id: 表情包 ID（sha256 前 16 位）

    Returns:
        图片二进制流，Content-Type 为图片 MIME 类型
    """
    try:
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if not record:
            return StreamingResponse(
                io.BytesIO(b"meme not found"),
                status_code=404,
                media_type="text/plain",
            )

        file_path = get_memes_base_path() / record.file_path
        image_data = await _read_file(file_path)
        if not image_data:
            return StreamingResponse(
                io.BytesIO(b"file not found"),
                status_code=404,
                media_type="text/plain",
            )

        return StreamingResponse(
            io.BytesIO(image_data),
            media_type=record.file_mime,
            headers={"Content-Disposition": f"inline; filename={record.meme_id}"},
        )
    except Exception as e:
        return StreamingResponse(
            io.BytesIO(f"error: {e}".encode()),
            status_code=500,
            media_type="text/plain",
        )


# ─────────────────────────────────────────────
# 4. 更新标签/描述/归属
# ─────────────────────────────────────────────


@app.put("/api/meme/{meme_id}", summary="更新标签/描述/归属", tags=MEME)
async def update_meme(
    meme_id: str,
    req: MemeUpdateRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新表情包标签/描述/归属

    - 编辑 persona_hint 会联动移动文件到对应归属文件夹并重新同步向量索引
      （检索按 folder 过滤，仅改 persona_hint 字段对检索无效）；
    - rejected 状态不会因编辑标签被静默解除，恢复需显式传 status 字段。

    Args:
        meme_id: 表情包 ID
        req: 更新请求体

    Returns:
        status: 0成功，1失败
    """
    try:
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if not record:
            return {"status": 1, "msg": "表情包不存在", "data": None}

        # 状态变更必须显式：rejected（如 NSFW 被拒）的图编辑标签不再被静默提升
        if req.status is not None:
            if req.status not in _VALID_MEME_STATUSES:
                return {"status": 1, "msg": f"无效的状态: {req.status}", "data": None}
            new_status: Optional[str] = req.status
        elif record.status == "rejected":
            new_status = None
        else:
            new_status = "manual"

        success = await MemeLibrary.update_tags(
            meme_id=meme_id,
            description=req.description,
            emotion_tags=req.emotion_tags,
            scene_tags=req.scene_tags,
            custom_tags=req.custom_tags,
            persona_hint=req.persona_hint,
            status=new_status,
        )
        if not success:
            return {"status": 1, "msg": "更新失败（没有需要更新的字段）", "data": None}

        final_status = new_status or record.status

        if final_status == "rejected":
            # 显式置为 rejected：移动到 rejected 文件夹并移出向量索引
            if record.folder != "rejected":
                await MemeLibrary.move_file(meme_id, "rejected")
            await MemeLibrary.remove_from_index(meme_id)
            return {"status": 0, "msg": "更新成功", "data": None}

        if final_status in _INDEXABLE_STATUSES:
            # persona_hint 编辑联动 folder；从 inbox/rejected 恢复的图也归位到归属文件夹
            hint = req.persona_hint if req.persona_hint is not None else record.persona_hint
            expected_folder = _folder_for_persona(hint)
            persona_changed = req.persona_hint is not None and req.persona_hint != record.persona_hint
            need_move = (persona_changed or record.folder in ("inbox", "rejected")) and record.folder != expected_folder
            if need_move:
                # move_file 内部会更新 persona_hint 并重新同步 Qdrant
                await MemeLibrary.move_file(meme_id, expected_folder)
            else:
                # 未移动文件时补一次同步，保证最新标签/描述写入向量索引
                updated_record = await AiMemeRecord.get_by_meme_id(meme_id)
                if updated_record:
                    await MemeLibrary.sync_to_qdrant(updated_record)

        return {"status": 0, "msg": "更新成功", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"更新失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 5. 移动表情包到目标文件夹
# ─────────────────────────────────────────────


@app.post("/api/meme/{meme_id}/move", summary="移动表情包到目标文件夹", tags=MEME)
async def move_meme(
    meme_id: str,
    req: MemeMoveRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    移动表情包到目标文件夹

    Args:
        meme_id: 表情包 ID
        req: 移动请求体

    Returns:
        status: 0成功，1失败
    """
    try:
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if not record:
            return {"status": 1, "msg": "表情包不存在", "data": None}

        success = await MemeLibrary.move_file(meme_id, req.target_folder)
        if not success:
            return {"status": 1, "msg": "移动失败", "data": None}

        return {"status": 0, "msg": f"已移动到 {req.target_folder}", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"移动失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 6. 删除表情包
# ─────────────────────────────────────────────


@app.delete("/api/meme/{meme_id}", summary="删除表情包", tags=MEME)
async def delete_meme(
    meme_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除表情包（文件+记录）

    Args:
        meme_id: 表情包 ID

    Returns:
        status: 0成功，1失败
    """
    try:
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if not record:
            return {"status": 1, "msg": "表情包不存在", "data": None}

        success = await MemeLibrary.delete_meme(meme_id)
        if not success:
            return {"status": 1, "msg": "删除失败", "data": None}

        return {"status": 0, "msg": "删除成功", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"删除失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 7. 手动上传表情包
# ─────────────────────────────────────────────


@app.post("/api/meme/upload", summary="手动上传表情包", tags=MEME)
async def upload_meme(
    file: UploadFile = File(...),
    folder: str = Form("common"),
    auto_tag: bool = Form(True),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    手动上传表情包

    指定的目标文件夹会被尊重：VLM 打标只补充描述/标签，
    不会再按 persona_hint 把图移出用户指定的文件夹。
    如需由 VLM 自动决定归属，请将 folder 设为 "inbox"。

    Args:
        file: 图片文件
        folder: 目标文件夹（"inbox" 表示交给 VLM 打标后自动归属）
        auto_tag: 是否自动触发 VLM 打标

    Returns:
        status: 0成功，1失败
        data: 包含 meme_id 的上传结果
    """
    try:
        from PIL import Image

        # 读取文件内容
        image_data = await file.read()

        # 获取 MIME 类型
        file_mime = file.content_type or "image/jpeg"

        # 获取图片尺寸
        img = Image.open(io.BytesIO(image_data))
        width, height = img.size

        # 保存到库中（save_raw 保存到 inbox，后续可移动）
        record = await MemeLibrary.save_raw(
            image_data=image_data,
            file_mime=file_mime,
            width=width,
            height=height,
            source_group="manual",
        )

        if not record:
            return {"status": 1, "msg": "保存失败（可能已存在）", "data": None}

        # 如果指定了非 inbox 文件夹，移动过去
        # （move_file 会同步推导 persona_hint，打标时不会再被 VLM 覆盖归属）
        if folder != "inbox":
            await MemeLibrary.move_file(record.meme_id, folder)

        # 更新状态
        if auto_tag:
            await AiMemeRecord.update_record(record.meme_id, {"status": "pending"})
            await enqueue_tag(record.meme_id)
        else:
            await AiMemeRecord.update_record(record.meme_id, {"status": "manual"})

        return {
            "status": 0,
            "msg": "上传成功",
            "data": {"meme_id": record.meme_id},
        }
    except Exception as e:
        return {"status": 1, "msg": f"上传失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 8. 重新触发 VLM 打标
# ─────────────────────────────────────────────


@app.post("/api/meme/{meme_id}/retag", summary="重新触发 VLM 打标", tags=MEME)
async def retag_meme(
    meme_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    重新触发 VLM 打标

    Args:
        meme_id: 表情包 ID

    Returns:
        status: 0成功，1失败
    """
    try:
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if not record:
            return {"status": 1, "msg": "表情包不存在", "data": None}

        # 重置状态为待打标（即使 VLM 打标当前关闭也保留 pending，开启后会自动补打标）
        await AiMemeRecord.update_record(meme_id, {"status": "pending"})

        if not is_tagging_enabled():
            return {
                "status": 0,
                "msg": "VLM 打标当前已关闭，已标记为待打标，开启 meme_vlm_enable 后将自动处理",
                "data": None,
            }

        # 加入打标队列
        await enqueue_tag(meme_id)

        return {"status": 0, "msg": "已加入打标队列", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"操作失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 9. 统计概览
# ─────────────────────────────────────────────


@app.get("/api/meme/stats", summary="统计概览", tags=MEME)
async def get_meme_stats(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取表情包统计概览

    Returns:
        status: 0成功，1失败
        data: 统计信息
    """
    try:
        stats = await AiMemeRecord.get_stats()
        return {"status": 0, "msg": "ok", "data": stats}
    except Exception as e:
        return {"status": 1, "msg": f"获取统计失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 10. 批量删除表情包
# ─────────────────────────────────────────────


@app.post("/api/meme/batch_delete", summary="批量删除表情包", tags=MEME)
async def batch_delete_memes(
    req: MemeBatchDeleteRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    批量删除表情包（文件+记录）

    Args:
        req: 批量删除请求体，包含 meme_ids 列表

    Returns:
        status: 0成功，1部分失败
        data: 包含成功/失败详情
    """
    success_ids: List[str] = []
    failed_ids: List[Dict[str, Any]] = []

    for meme_id in req.meme_ids:
        try:
            record = await AiMemeRecord.get_by_meme_id(meme_id)
            if not record:
                failed_ids.append({"meme_id": meme_id, "reason": "不存在"})
                continue

            ok = await MemeLibrary.delete_meme(meme_id)
            if ok:
                success_ids.append(meme_id)
            else:
                failed_ids.append({"meme_id": meme_id, "reason": "删除失败"})
        except Exception as e:
            failed_ids.append({"meme_id": meme_id, "reason": str(e)})

    if not failed_ids:
        return {
            "status": 0,
            "msg": f"批量删除成功，共删除 {len(success_ids)} 个",
            "data": {"success_count": len(success_ids), "failed": []},
        }
    else:
        return {
            "status": 1,
            "msg": f"删除完成：成功 {len(success_ids)} 个，失败 {len(failed_ids)} 个",
            "data": {"success_count": len(success_ids), "failed": failed_ids},
        }


# ─────────────────────────────────────────────
# 10b. 清除所有已拒绝的表情包
# ─────────────────────────────────────────────


@app.post("/api/meme/purge_rejected", summary="b. 清除所有已拒绝的表情包", tags=MEME)
async def purge_rejected_memes(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    批量删除所有状态为 rejected 的表情包（源文件+数据库记录+Qdrant 向量）

    Returns:
        status: 0成功，1部分失败
        data: 包含成功/失败详情
    """
    try:
        # 获取所有 rejected 状态的记录（不分页）
        records = await AiMemeRecord.get_all_by_status(status="rejected")

        if not records:
            return {"status": 0, "msg": "没有已拒绝的表情包", "data": {"purged_count": 0, "failed": []}}

        success_ids: List[str] = []
        failed_items: List[Dict[str, Any]] = []

        for record in records:
            try:
                ok = await MemeLibrary.delete_meme(record.meme_id)
                if ok:
                    success_ids.append(record.meme_id)
                else:
                    failed_items.append({"meme_id": record.meme_id, "reason": "删除失败"})
            except Exception as e:
                failed_items.append({"meme_id": record.meme_id, "reason": str(e)})

        if not failed_items:
            return {
                "status": 0,
                "msg": f"已清除 {len(success_ids)} 个已拒绝的表情包",
                "data": {"purged_count": len(success_ids), "failed": []},
            }
        else:
            return {
                "status": 1,
                "msg": f"清除完成：成功 {len(success_ids)} 个，失败 {len(failed_items)} 个",
                "data": {"purged_count": len(success_ids), "failed": failed_items},
            }
    except Exception as e:
        return {"status": 1, "msg": f"清除失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 10c. 批量重新打标（待手动处理状态）
# ─────────────────────────────────────────────


@app.post("/api/meme/batch_retag_pending", summary="c. 批量重新打标（待手动处理状态）", tags=MEME)
async def batch_retag_pending(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    批量重新触发 VLM 打标，针对所有状态为 pending_manual 的表情包

    将 pending_manual 状态的记录重置为 pending，并逐条加入打标队列。

    Returns:
        status: 0成功，1部分失败
        data: 包含入队成功/失败详情
    """
    try:
        # 获取所有 pending_manual 状态的记录（不分页）
        records = await AiMemeRecord.get_all_by_status(status="pending_manual")

        if not records:
            return {
                "status": 0,
                "msg": "没有待手动处理的表情包",
                "data": {"retag_count": 0, "failed": []},
            }

        # VLM 打标关闭时，仅重置为 pending，由开启后台扫描自动补打标
        if not is_tagging_enabled():
            for record in records:
                await AiMemeRecord.update_record(record.meme_id, {"status": "pending"})
            return {
                "status": 0,
                "msg": (
                    f"VLM 打标当前已关闭，已将 {len(records)} 个表情包标记为待打标，开启 meme_vlm_enable 后将自动处理"
                ),
                "data": {"retag_count": len(records), "failed": []},
            }

        success_ids: List[str] = []
        failed_items: List[Dict[str, Any]] = []

        for record in records:
            try:
                # 重置状态为待打标
                await AiMemeRecord.update_record(record.meme_id, {"status": "pending"})
                # 加入打标队列
                await enqueue_tag(record.meme_id)
                success_ids.append(record.meme_id)
            except Exception as e:
                failed_items.append({"meme_id": record.meme_id, "reason": str(e)})

        if not failed_items:
            return {
                "status": 0,
                "msg": f"已将 {len(success_ids)} 个待手动处理的表情包加入打标队列",
                "data": {"retag_count": len(success_ids), "failed": []},
            }
        else:
            return {
                "status": 1,
                "msg": f"操作完成：成功 {len(success_ids)} 个，失败 {len(failed_items)} 个",
                "data": {"retag_count": len(success_ids), "failed": failed_items},
            }
    except Exception as e:
        return {"status": 1, "msg": f"操作失败: {e}", "data": None}


# ─────────────────────────────────────────────
# 11. 批量导出表情包（.meme 格式）
# ─────────────────────────────────────────────


def _record_to_export_dict(record: AiMemeRecord) -> Dict[str, Any]:
    """将 AiMemeRecord 转换为导出用的字典（去除运行时字段）"""
    return {
        "meme_id": record.meme_id,
        "file_path": record.file_path,
        "file_size": record.file_size,
        "file_mime": record.file_mime,
        "width": record.width,
        "height": record.height,
        "folder": record.folder,
        "persona_hint": record.persona_hint,
        "emotion_tags": record.emotion_tags,
        "scene_tags": record.scene_tags,
        "description": record.description,
        "custom_tags": record.custom_tags,
        "status": record.status,
        "nsfw_score": record.nsfw_score,
    }


@app.post("/api/meme/export", summary="批量导出表情包（.meme 格式）", tags=MEME)
async def export_memes(
    req: MemeBatchExportRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> StreamingResponse:
    """
    批量导出表情包为 .meme 格式文件（实际为 ZIP）

    .meme 包结构:
      manifest.json  - 版本与导出信息
      metadata.json  - 表情包元数据列表
      files/         - 表情包源文件目录

    Args:
        req: 导出请求体，可指定 meme_ids 或 folder

    Returns:
        .meme 文件二进制流
    """
    try:
        # ── 获取要导出的记录 ──
        if req.meme_ids:
            records = await AiMemeRecord.get_by_meme_ids(req.meme_ids)
        elif req.folder:
            # 按文件夹获取全部（不分页）
            records = await AiMemeRecord.get_all_by_folder(folder=req.folder)
        else:
            # 导出全部
            records = await AiMemeRecord.get_all_records_no_page()

        if not records:
            return StreamingResponse(
                io.BytesIO(json.dumps({"status": 1, "msg": "没有可导出的表情包"}).encode()),
                media_type="application/json",
                status_code=400,
            )

        # ── 构建 ZIP 到内存 ──
        buf = io.BytesIO()
        base_path = get_memes_base_path()

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # manifest.json
            manifest = {
                "version": MEME_FORMAT_VERSION,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "total_count": len(records),
            }
            zf.writestr(MEME_MANIFEST_FILE, json.dumps(manifest, ensure_ascii=False, indent=2))

            # metadata.json
            metadata = [_record_to_export_dict(r) for r in records]
            zf.writestr(MEME_METADATA_FILE, json.dumps(metadata, ensure_ascii=False, indent=2))

            # files/ - 写入源文件
            for record in records:
                file_path = base_path / record.file_path
                if file_path.exists():
                    file_data = file_path.read_bytes()
                    # ZIP 内路径: files/{meme_id}.{ext}
                    file_name = Path(record.file_path).name
                    zf.writestr(f"{MEME_FILES_DIR}/{file_name}", file_data)

        buf.seek(0)

        # 生成文件名
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"memes_{timestamp}.meme"

        return StreamingResponse(
            buf,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return StreamingResponse(
            io.BytesIO(json.dumps({"status": 1, "msg": f"导出失败: {e}"}).encode()),
            media_type="application/json",
            status_code=500,
        )


# ─────────────────────────────────────────────
# 12. 导入 .meme 格式表情包
# ─────────────────────────────────────────────


@app.post("/api/meme/import", summary="导入 .meme 格式文件", tags=MEME)
async def import_memes(
    file: UploadFile = File(..., description=".meme 格式文件"),
    skip_existing: bool = Form(True, description="是否跳过已存在的表情包"),
    auto_tag: bool = Form(False, description="是否对新导入的表情包触发 VLM 打标"),
    persona_hint: str = Form(
        "common",
        max_length=64,
        description=(
            "指定导入后的归属人格（如 'common' 或某 persona 名，不含 'persona_' 前缀）。"
            "会覆盖 metadata.json 中的 folder / persona_hint 字段，"
            "使所有导入项落到该人格对应的 folder 下；缺省 'common' 代表通用表情包。"
            "前端可调用 GET /api/meme/personas 拿到可选人格列表。"
        ),
    ),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    导入 .meme 格式表情包文件

    .meme 包结构:
      manifest.json  - 版本与导出信息
      metadata.json  - 表情包元数据列表
      files/         - 表情包源文件目录

    导入逻辑:
      1. 解析 manifest.json 校验版本
      2. 读取 metadata.json 获取元数据
      3. 逐条处理：若 meme_id 已存在且 skip_existing=True 则跳过
      4. 从 files/ 读取源文件，保存到 `persona_hint` 推导出的 folder（与
         `MemeLibrary.move_file` 的 folder ↔ persona_hint 双向一致规则一致）
      5. 写入数据库记录，可选触发 VLM 打标

    Args:
        file: .meme 格式文件（ZIP）
        skip_existing: 是否跳过已存在的表情包
        auto_tag: 是否触发 VLM 打标
        persona_hint: 导入后的归属人格标识，缺省 'common'；
            会同时覆盖 metadata.json 中每条的 folder 与 persona_hint
            （后端负责保证两者一致），以保证导入的项与 persona_hint 路由一致。

    Returns:
        status: 0成功，1部分失败
        data: 导入统计信息
    """
    try:
        file_data = await file.read()

        # 校验是否为有效 ZIP
        if not zipfile.is_zipfile(io.BytesIO(file_data)):
            return {"status": 1, "msg": "无效的 .meme 文件（非 ZIP 格式）", "data": None}

        imported_ids: List[str] = []
        skipped_ids: List[str] = []
        failed_items: List[Dict[str, Any]] = []

        base_path = get_memes_base_path()

        with zipfile.ZipFile(io.BytesIO(file_data), "r") as zf:
            # ── 校验 manifest ──
            if MEME_MANIFEST_FILE not in zf.namelist():
                return {"status": 1, "msg": "无效的 .meme 文件（缺少 manifest.json）", "data": None}

            manifest = json.loads(zf.read(MEME_MANIFEST_FILE))
            version = manifest.get("version", "1.0")
            # 版本兼容检查（当前仅支持 1.x）
            if not version.startswith("1."):
                return {
                    "status": 1,
                    "msg": f"不支持的 .meme 格式版本: {version}",
                    "data": None,
                }

            # ── 读取 metadata ──
            if MEME_METADATA_FILE not in zf.namelist():
                return {"status": 1, "msg": "无效的 .meme 文件（缺少 metadata.json）", "data": None}

            metadata_list = json.loads(zf.read(MEME_METADATA_FILE))

            # ── 解析全局 persona_hint：与 `_folder_for_persona` 保持一致
            # （与 MemeLibrary.move_file 的 folder ↔ persona_hint
            # 双向一致规则同源）。空字符串 / 缺省一律走 "common"。 ──
            target_persona_hint = (persona_hint or "common").strip() or "common"
            target_folder = _folder_for_persona(target_persona_hint)

            # ── 逐条导入（每条独立保护，单条失败不中断整体导入） ──
            for meta in metadata_list:
                meme_id = meta.get("meme_id", "")
                if not meme_id:
                    failed_items.append({"meme_id": "", "reason": "缺少 meme_id"})
                    continue

                try:
                    exists = await AiMemeRecord.exists_by_meme_id(meme_id)
                    if exists and skip_existing:
                        skipped_ids.append(meme_id)
                        continue

                    # 读取源文件
                    file_name = Path(meta.get("file_path", "")).name
                    file_key = f"{MEME_FILES_DIR}/{file_name}"
                    if file_key not in zf.namelist():
                        failed_items.append({"meme_id": meme_id, "reason": f"包中缺少源文件: {file_key}"})
                        continue

                    image_data = zf.read(file_key)
                    file_mime = meta.get("file_mime", "image/jpeg")
                    width = meta.get("width", 0)
                    height = meta.get("height", 0)
                    # 优先级：表单传入的 persona_hint 推导的 folder > metadata.json 中的 folder
                    # 避免“原导出处为 persona_A、却以 persona_B 导入”的不一致场景。
                    folder = target_folder

                    # 保存文件到目标文件夹
                    target_folder_path = base_path / folder
                    target_folder_path.mkdir(parents=True, exist_ok=True)

                    # 确定文件扩展名
                    ext = Path(file_name).suffix or ".jpg"
                    target_file_path = target_folder_path / f"{meme_id}{ext}"
                    target_file_path.write_bytes(image_data)

                    relative_path = f"{folder}/{meme_id}{ext}"

                    # auto_tag 时直接以 pending 入库，避免"先按导入状态 sync、
                    # 打标后再 sync"的双写与陈旧状态入索引
                    status = "pending" if auto_tag else meta.get("status", "manual")

                    record_fields: Dict[str, Any] = {
                        "file_path": relative_path,
                        "file_size": len(image_data),
                        "file_mime": file_mime,
                        "width": width,
                        "height": height,
                        "source_group": "import",
                        "folder": folder,
                        "persona_hint": target_persona_hint,
                        "emotion_tags": meta.get("emotion_tags", []),
                        "scene_tags": meta.get("scene_tags", []),
                        "description": meta.get("description", ""),
                        "custom_tags": meta.get("custom_tags", []),
                        "status": status,
                        "nsfw_score": meta.get("nsfw_score", 0.0),
                    }

                    if exists:
                        # skip_existing=False：覆盖更新既有记录，而非主键冲突中断
                        await AiMemeRecord.update_record(meme_id, record_fields)
                        record = await AiMemeRecord.get_by_meme_id(meme_id)
                    else:
                        record = AiMemeRecord(meme_id=meme_id, **record_fields)
                        await AiMemeRecord.insert_record(record)

                    if auto_tag:
                        # 打标完成后由 tagger 统一同步向量索引
                        await enqueue_tag(meme_id)
                    elif (
                        record is not None
                        and record.status in _INDEXABLE_STATUSES
                        and (record.description or record.all_tags)
                    ):
                        # 仅可检索状态入向量索引（rejected/pending 等不入）
                        try:
                            await MemeLibrary.sync_to_qdrant(record)
                        except Exception:
                            pass  # Qdrant 同步失败不影响导入

                    imported_ids.append(meme_id)
                except Exception as e:
                    failed_items.append({"meme_id": meme_id, "reason": str(e)})

        return {
            "status": 0,
            "msg": f"导入完成：成功 {len(imported_ids)} 个，跳过 {len(skipped_ids)} 个，失败 {len(failed_items)} 个",
            "data": {
                "imported_count": len(imported_ids),
                "skipped_count": len(skipped_ids),
                "imported_ids": imported_ids,
                "skipped_ids": skipped_ids,
                "failed": failed_items,
            },
        }
    except Exception as e:
        return {"status": 1, "msg": f"导入失败: {e}", "data": None}
