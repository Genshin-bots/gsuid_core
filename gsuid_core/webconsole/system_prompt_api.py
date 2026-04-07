"""
AI System Prompt APIs

提供 System Prompt 管理相关的 RESTful APIs，
包括System Prompt的增删改查、分页列表、搜索等。
"""

import uuid
from typing import Any, Dict, Optional

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.system_prompt import (
    SystemPrompt,
    add_prompt,
    delete_prompt,
    update_prompt,
    get_all_prompts,
    get_prompt_by_id,
    search_system_prompt,
    sync_to_vector_store,
    update_in_vector_store,
    delete_from_vector_store,
)


class SystemPromptCreate(BaseModel):
    """System Prompt 新增请求模型"""

    title: str
    desc: str
    content: str
    tags: list[str]


class SystemPromptUpdate(BaseModel):
    """System Prompt 更新请求模型"""

    title: str = ""
    desc: str = ""
    content: str = ""
    tags: list[str] = []


@app.get("/api/ai/system_prompt/list")
async def get_system_prompt_list(
    offset: int = 0,
    limit: int = 20,
    page: int = 1,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取System Prompt列表（分页）

    Args:
        offset: 起始偏移，默认0（会被page参数覆盖）
        limit: 每页数量，默认20
        page: 页码，从1开始

    Returns:
        status: 0成功，1失败
        data: 包含Prompt列表、总数和分页信息
    """
    if page > 1:
        offset = (page - 1) * limit

    all_prompts = get_all_prompts()
    total = len(all_prompts)

    # 分页
    prompts = all_prompts[offset : offset + limit]

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "list": prompts,
            "total": total,
            "offset": offset,
            "limit": limit,
            "page": page,
            "page_size": limit,
        },
    }


@app.get("/api/ai/system_prompt/search")
async def search_system_prompt_api(
    query: str,
    tags: Optional[str] = None,
    limit: int = 10,
    use_vector: bool = True,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    搜索System Prompt

    Args:
        query: 查询文本
        tags: 可选，逗号分隔的标签列表
        limit: 返回数量限制，默认10
        use_vector: 是否使用向量检索，默认True

    Returns:
        status: 0成功，1失败
        data: 匹配的Prompt列表
    """
    tag_list = None
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    results = await search_system_prompt(
        query=query,
        tags=tag_list,
        limit=limit,
        use_vector=use_vector,
    )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "results": results,
            "count": len(results),
            "query": query,
        },
    }


@app.get("/api/ai/system_prompt/{prompt_id}")
async def get_system_prompt_detail(
    prompt_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定System Prompt的详细信息

    Args:
        prompt_id: Prompt ID

    Returns:
        status: 0成功，1失败
        data: Prompt详情
    """
    prompt = get_prompt_by_id(prompt_id)

    if prompt is None:
        return {
            "status": 1,
            "msg": f"System Prompt '{prompt_id}' not found",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": prompt,
    }


@app.post("/api/ai/system_prompt")
async def create_system_prompt(
    prompt_data: SystemPromptCreate,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    新增System Prompt

    Args:
        prompt_data: Prompt信息

    Returns:
        status: 0成功，1失败
        data: 新增结果
    """
    # 生成ID
    prompt_dict = prompt_data.model_dump()
    prompt_dict["id"] = str(uuid.uuid4())

    # 保存到JSON文件
    success = add_prompt(SystemPrompt(**prompt_dict))
    if not success:
        return {
            "status": 1,
            "msg": "Failed to add System Prompt (ID may already exist)",
            "data": None,
        }

    # 同步到向量库
    await sync_to_vector_store([prompt_dict])

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": prompt_dict["id"],
            "title": prompt_data.title,
        },
    }


@app.put("/api/ai/system_prompt/{prompt_id}")
async def update_system_prompt(
    prompt_id: str,
    updates: SystemPromptUpdate,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新System Prompt

    Args:
        prompt_id: Prompt ID
        updates: 要更新的字段

    Returns:
        status: 0成功，1失败
        data: 更新结果
    """
    # 过滤掉空字符串和空列表字段
    update_dict = updates.model_dump()
    update_dict = {k: v for k, v in update_dict.items() if v != "" and v != []}

    if not update_dict:
        return {
            "status": 1,
            "msg": "No valid updates provided",
            "data": None,
        }

    # 更新JSON文件
    success = update_prompt(prompt_id, update_dict)
    if not success:
        return {
            "status": 1,
            "msg": f"System Prompt '{prompt_id}' not found or update failed",
            "data": None,
        }

    # 获取更新后的完整数据
    updated_prompt = get_prompt_by_id(prompt_id)
    if updated_prompt:
        # 同步到向量库
        await update_in_vector_store(dict(updated_prompt))

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": prompt_id,
        },
    }


@app.delete("/api/ai/system_prompt/{prompt_id}")
async def delete_system_prompt(
    prompt_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除System Prompt

    Args:
        prompt_id: Prompt ID

    Returns:
        status: 0成功，1失败
        data: 删除结果
    """
    # 从JSON文件删除
    success = delete_prompt(prompt_id)
    if not success:
        return {
            "status": 1,
            "msg": f"System Prompt '{prompt_id}' not found or delete failed",
            "data": None,
        }

    # 从向量库删除
    await delete_from_vector_store(prompt_id)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": prompt_id,
        },
    }
