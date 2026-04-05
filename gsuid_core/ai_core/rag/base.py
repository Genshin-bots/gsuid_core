"""RAG模块基础功能 - 共享常量和工具函数"""

import os
import json
import uuid
import hashlib
from typing import TYPE_CHECKING, Final, Union

from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.ai_config import ai_config, rerank_model_config, local_embedding_config

# ============== 向量库配置 ==============
DIMENSION: Final[int] = 512

# Embedding模型相关
EMBEDDING_MODEL_NAME: Final[str] = local_embedding_config.get_config("embedding_model_name").data
MODELS_CACHE = AI_CORE_PATH / "models_cache"
DB_PATH = AI_CORE_PATH / "local_qdrant_db"

# Reranker模型相关
RERANK_MODELS_CACHE = AI_CORE_PATH / "rerank_models_cache"
RERANKER_MODEL_NAME: Final[str] = rerank_model_config.get_config("rerank_model_name").data

# ============== Collection名称 ==============
TOOLS_COLLECTION_NAME: Final[str] = "bot_tools"
KNOWLEDGE_COLLECTION_NAME: Final[str] = "knowledge"


# ============== 配置开关（动态读取，避免模块加载时配置文件不存在导致默认值错误） ==============
def is_enable_ai() -> bool:
    return ai_config.get_config("enable").data


def is_enable_rerank() -> bool:
    return ai_config.get_config("enable_rerank").data


# ============== 全局变量 ==============
if TYPE_CHECKING:
    from fastembed import TextEmbedding
    from qdrant_client import AsyncQdrantClient

embedding_model: "Union[TextEmbedding, None]" = None
client: "Union[AsyncQdrantClient, None]" = None


def init_embedding_model():
    """初始化Embedding模型和Qdrant客户端"""
    global embedding_model, client

    if not is_enable_ai():
        return

    # 防止重复初始化，导致Qdrant文件锁冲突
    if client is not None:
        return

    from fastembed import TextEmbedding
    from qdrant_client import AsyncQdrantClient

    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "60"

    embedding_model = TextEmbedding(
        model_name=EMBEDDING_MODEL_NAME,
        cache_dir=str(MODELS_CACHE),
        threads=2,
    )
    client = AsyncQdrantClient(path=str(DB_PATH))


def get_point_id(id_str: str) -> str:
    """生成向量化存储的唯一ID

    使用UUID5和DNS命名空间生成确定性的UUID，
    相同id_str始终生成相同的UUID，确保幂等性。

    Args:
        id_str: 唯一标识符字符串

    Returns:
        唯一的UUID字符串
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, id_str))


def calculate_hash(content: dict) -> str:
    """计算内容字典的MD5哈希

    用于检测内容是否有变更，支持知识库增量更新判断。
    排序键以确保相同内容产生相同的哈希值。

    Args:
        content: 要计算哈希的内容字典

    Returns:
        MD5哈希值（32位十六进制字符串）
    """
    json_str = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(json_str.encode("utf-8")).hexdigest()
