"""System Prompt 存储管理 - 文件夹存储（每个Prompt独立JSON文件）"""

import json
from typing import List, Optional
from pathlib import Path

from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH

from .models import SystemPrompt

# 存储文件夹路径
STORAGE_DIR = AI_CORE_PATH / "system_prompt"


def _ensure_storage_dir() -> Path:
    """确保存储目录存在"""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return STORAGE_DIR


def _get_prompt_file_path(prompt_id: str) -> Path:
    """获取Prompt文件的完整路径"""
    return _ensure_storage_dir() / f"{prompt_id}.json"


def _load_prompt_from_file(file_path: Path) -> Optional[SystemPrompt]:
    """从单个JSON文件加载System Prompt"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return SystemPrompt(**data)
    except Exception as e:
        logger.error(f"❌ [SystemPrompt] 加载文件失败 {file_path}: {e}")
        return None


def _save_prompt_to_file(prompt: SystemPrompt) -> bool:
    """保存单个System Prompt到JSON文件"""
    try:
        file_path = _get_prompt_file_path(prompt["id"])
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(prompt, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"❌ [SystemPrompt] 保存文件失败: {e}")
        return False


def _delete_prompt_file(prompt_id: str) -> bool:
    """删除Prompt对应的JSON文件"""
    try:
        file_path = _get_prompt_file_path(prompt_id)
        if file_path.exists():
            file_path.unlink()
        return True
    except Exception as e:
        logger.error(f"❌ [SystemPrompt] 删除文件失败: {e}")
        return False


def get_all_prompts() -> List[SystemPrompt]:
    """获取所有System Prompt"""
    prompts = []
    storage_dir = _ensure_storage_dir()

    # 遍历所有.json文件
    for file_path in storage_dir.glob("*.json"):
        prompt = _load_prompt_from_file(file_path)
        if prompt:
            prompts.append(prompt)

    return prompts


def get_prompt_by_id(prompt_id: str) -> Optional[SystemPrompt]:
    """根据ID获取单个System Prompt"""
    file_path = _get_prompt_file_path(prompt_id)
    if not file_path.exists():
        return None
    return _load_prompt_from_file(file_path)


def add_prompt(prompt: SystemPrompt) -> bool:
    """添加新的System Prompt

    Args:
        prompt: SystemPrompt数据

    Returns:
        bool: 是否成功添加（如果ID或title已存在则返回False）
    """
    # 检查ID或title是否已存在
    existing_prompts = get_all_prompts()
    for existing in existing_prompts:
        if existing["id"] == prompt["id"]:
            logger.warning(f"⚠️ [SystemPrompt] ID已存在: {prompt['id']}")
            return False
        if existing["title"] == prompt["title"]:
            logger.warning(f"⚠️ [SystemPrompt] Title已存在: {prompt['title']}")
            return False

    # 保存到文件
    if _save_prompt_to_file(prompt):
        logger.info(f"✅ [SystemPrompt] 添加成功: {prompt['title']} ({prompt['id']})")
        return True
    return False


def update_prompt(prompt_id: str, updates: dict) -> bool:
    """更新System Prompt

    Args:
        prompt_id: 要更新的Prompt ID
        updates: 要更新的字段

    Returns:
        bool: 是否成功更新
    """
    # 获取现有Prompt
    existing = get_prompt_by_id(prompt_id)
    if not existing:
        logger.warning(f"⚠️ [SystemPrompt] 要更新的ID不存在: {prompt_id}")
        return False

    # 如果要更新title，检查是否与其他Prompt冲突
    new_title = updates.get("title")
    if new_title and new_title != existing["title"]:
        all_prompts = get_all_prompts()
        for p in all_prompts:
            if p["id"] != prompt_id and p["title"] == new_title:
                logger.warning(f"⚠️ [SystemPrompt] Title已存在: {new_title}")
                return False

    # 不允许修改id
    updates.pop("id", None)

    # 合并更新
    updated_prompt = SystemPrompt(**{**existing, **updates})

    # 保存更新后的文件
    if _save_prompt_to_file(updated_prompt):
        logger.info(f"✅ [SystemPrompt] 更新成功: {prompt_id}")
        return True
    return False


def delete_prompt(prompt_id: str) -> bool:
    """删除System Prompt

    Args:
        prompt_id: 要删除的Prompt ID

    Returns:
        bool: 是否成功删除
    """
    # 检查是否存在
    if not get_prompt_by_id(prompt_id):
        logger.warning(f"⚠️ [SystemPrompt] 要删除的ID不存在: {prompt_id}")
        return False

    # 删除文件
    if _delete_prompt_file(prompt_id):
        logger.info(f"✅ [SystemPrompt] 删除成功: {prompt_id}")
        return True
    return False


def search_prompts(
    query: str,
    tags: Optional[List[str]] = None,
    limit: int = 5,
) -> List[SystemPrompt]:
    """简单检索 - 按标题/desc/tags模糊匹配

    注意：此函数用于非向量检索场景。
    向量检索请使用 vector_store.py 中的 search_by_vector。

    Args:
        query: 查询文本
        tags: 可选，按标签过滤
        limit: 返回数量限制

    Returns:
        匹配的System Prompt列表
    """
    all_prompts = get_all_prompts()
    results = []

    query_lower = query.lower()

    for prompt in all_prompts:
        # 按标签过滤
        if tags:
            prompt_tags = [t.lower() for t in prompt.get("tags", [])]
            if not any(t in prompt_tags for t in [tag.lower() for tag in tags]):
                continue

        # 匹配标题、描述或标签
        if (
            query_lower in prompt["title"].lower()
            or query_lower in prompt["desc"].lower()
            or any(query_lower in tag.lower() for tag in prompt.get("tags", []))
        ):
            results.append(prompt)

    return results[:limit]
