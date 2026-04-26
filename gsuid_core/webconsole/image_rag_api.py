"""
AI Image RAG APIs

提供 AI 图片向量检索相关的 RESTful APIs，包括图片上传、搜索、分页列表、删除等。
图片通过插件注册或前端上传，存储在独立的向量集合中，支持语义搜索。
"""

import uuid
import shutil
from typing import Any, Dict, List, Optional
from pathlib import Path

from fastapi import File, Form, Depends, UploadFile
from pydantic import BaseModel

from gsuid_core.ai_core.resource import local_embedding_images
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.rag.image_rag import (
    search_images,
    get_image_list,
    delete_image_from_db,
    add_manual_image_to_db,
    get_image_path_by_query,
)


class ImageSearchRequest(BaseModel):
    """图片搜索请求模型"""

    query: str
    limit: int = 10
    plugin_filter: Optional[List[str]] = None


class ImageCreateRequest(BaseModel):
    """图片入库请求模型"""

    id: Optional[str] = None
    plugin: str = "manual"
    path: str
    tags: List[str]
    content: str = ""


@app.get("/api/ai/images/list")
async def get_image_rag_list(
    offset: int = 0,
    limit: int = 20,
    plugin: Optional[str] = None,
    page: int = 1,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取图片列表（分页）

    Args:
        offset: 起始偏移，默认0（会被page参数覆盖）
        limit: 每页数量，默认20
        plugin: 可选，按插件名过滤
        page: 页码，从1开始，例如page=2表示第二页（offset=20）

    Returns:
        status: 0成功，1失败
        data: 包含图片列表、总数和分页信息
    """
    # 如果指定了page参数，计算offset
    if page > 1:
        offset = (page - 1) * limit

    # 构建插件过滤列表
    plugin_filter = [plugin] if plugin else None

    result = await get_image_list(
        offset=offset,
        limit=limit,
        plugin_filter=plugin_filter,
    )

    # 添加page信息到返回结果
    result["page"] = page
    result["page_size"] = limit

    return {
        "status": 0,
        "msg": "ok",
        "data": result,
    }


@app.get("/api/ai/images/search")
async def search_image_rag(
    query: str,
    limit: int = 10,
    plugin: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    搜索图片

    根据查询文本语义搜索匹配的图片。

    Args:
        query: 查询文本（描述想要找的图片内容）
        limit: 返回数量限制，默认10
        plugin: 可选，按插件名过滤

    Returns:
        status: 0成功，1失败
        data: 匹配的图片列表
    """
    plugin_filter = [plugin] if plugin else None

    results = await search_images(
        query=query,
        limit=limit,
        plugin_filter=plugin_filter,
    )

    # 转换结果为可序列化的格式
    serialized_results = []
    for point in results:
        if point.payload:
            serialized_results.append(
                {
                    "id": point.payload.get("id"),
                    "plugin": point.payload.get("plugin"),
                    "path": point.payload.get("path"),
                    "tags": point.payload.get("tags", []),
                    "content": point.payload.get("content", ""),
                    "score": point.score,
                }
            )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "results": serialized_results,
            "count": len(serialized_results),
            "query": query,
        },
    }


@app.get("/api/ai/images/path")
async def get_image_path(
    query: str,
    plugin: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取最佳匹配的图片路径

    根据查询文本返回最匹配的图片路径。

    Args:
        query: 查询文本
        plugin: 可选，按插件名过滤

    Returns:
        status: 0成功（找到图片），1失败（未找到）
        data: 图片路径
    """
    plugin_filter = [plugin] if plugin else None

    path = await get_image_path_by_query(
        query=query,
        plugin_filter=plugin_filter,
    )

    if path is None:
        return {
            "status": 1,
            "msg": "No matching image found",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "path": path,
        },
    }


@app.delete("/api/ai/images/{entity_id}")
async def delete_image_rag(
    entity_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除图片

    从向量数据库中删除指定的图片条目。

    Args:
        entity_id: 图片 ID

    Returns:
        status: 0成功，1失败
        data: 删除结果
    """
    success = await delete_image_from_db(entity_id)

    if not success:
        return {
            "status": 1,
            "msg": f"Image '{entity_id}' not found or delete failed",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": entity_id,
        },
    }


@app.post("/api/ai/images/upload")
async def upload_image(
    file: UploadFile = File(...),
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    上传图片到 local_embedding_images 目录

    Args:
        file: 图片文件

    Returns:
        status: 0成功，1失败
        data: 包含保存的文件路径
    """
    try:
        # 确保目录存在
        local_embedding_images.mkdir(parents=True, exist_ok=True)

        # 生成唯一文件名
        file_ext = Path(file.filename or "image.png").suffix
        unique_filename = f"{uuid.uuid4()}{file_ext}"
        file_path = local_embedding_images / unique_filename

        # 保存文件
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 返回相对路径
        relative_path = str(file_path.relative_to(local_embedding_images.parent.parent))

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "filename": unique_filename,
                "path": str(file_path),
                "relative_path": relative_path,
            },
        }

    except Exception as e:
        return {
            "status": 1,
            "msg": f"Upload failed: {str(e)}",
            "data": None,
        }


@app.post("/api/ai/images")
async def create_image_entity(
    id: Optional[str] = Form(None),
    plugin: str = Form("manual"),
    path: str = Form(...),
    tags: str = Form(...),
    content: str = Form(""),
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    创建图片实体并入库

    将图片信息（路径、标签、描述）添加到向量数据库，支持语义搜索。

    Args:
        id: 可选，图片ID，不传则自动生成
        plugin: 插件名，默认"manual"
        path: 图片文件路径（上传后返回的path）
        tags: 标签，多个标签用逗号分隔，如"胡桃,原神,角色"
        content: 详细描述文本

    Returns:
        status: 0成功，1失败
        data: 包含创建的图片ID
    """
    try:
        # 生成ID
        entity_id = id or str(uuid.uuid4())

        # 解析标签
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

        # 构建图片实体
        image_entity = {
            "id": entity_id,
            "plugin": plugin,
            "path": path,
            "tags": tag_list,
            "content": content,
            "source": "manual",
        }

        # 添加到向量数据库
        success = await add_manual_image_to_db(image_entity)

        if not success:
            return {
                "status": 1,
                "msg": "Failed to add image to vector database",
                "data": None,
            }

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "id": entity_id,
                "path": path,
                "tags": tag_list,
            },
        }

    except Exception as e:
        return {
            "status": 1,
            "msg": f"Create image entity failed: {str(e)}",
            "data": None,
        }
