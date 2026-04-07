"""System Prompt 管理模块

提供System Prompt的存储、检索和管理的完整功能。

包含功能：
- JSON文件持久化存储
- Qdrant向量数据库检索
- 插件注册接口
- 统一检索API

Usage:
    from gsuid_core.ai_core.system_prompt import (
        SystemPrompt,
        get_all_prompts,
        get_prompt_by_id,
        add_prompt,
        update_prompt,
        delete_prompt,
        search_system_prompt,
        get_best_match,
        init_system_prompt_collection,
        sync_to_vector_store,
    )
"""

# 数据模型
from .models import SystemPrompt

# 检索接口
from .search import (
    get_best_match,
    search_system_prompt,
)

# 存储管理
from .storage import (
    add_prompt,
    delete_prompt,
    update_prompt,
    search_prompts,
    get_all_prompts,
    get_prompt_by_id,
)

# 默认数据
from .defaults import init_default_prompts

# 向量存储
from .vector_store import (
    search_by_vector,
    sync_to_vector_store,
    update_in_vector_store,
    delete_from_vector_store,
    init_system_prompt_collection,
)

__all__ = [
    # 模型
    "SystemPrompt",
    # 存储
    "get_all_prompts",
    "get_prompt_by_id",
    "add_prompt",
    "update_prompt",
    "delete_prompt",
    "search_prompts",
    # 向量存储
    "init_system_prompt_collection",
    "sync_to_vector_store",
    "search_by_vector",
    "delete_from_vector_store",
    "update_in_vector_store",
    # 检索
    "search_system_prompt",
    "get_best_match",
    # 默认数据
    "init_default_prompts",
]
