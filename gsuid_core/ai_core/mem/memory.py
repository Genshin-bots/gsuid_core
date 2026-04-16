"""Mem模块 - 封装memv库

直接使用memv的开箱即用功能，配置通过ai_config和rag/base提供。
"""

from memv import Memory as MemvMemory
from memv.llm import PydanticAIAdapter
from memv.embeddings import FastEmbedAdapter

from gsuid_core.ai_core.rag.base import EMBEDDING_MODEL_NAME
from gsuid_core.ai_core.resource import MEM_DB_URL
from gsuid_core.ai_core.configs.models import get_openai_chat_model


def _create_memory() -> "MemvMemory":
    """创建memv实例（单例）"""
    return MemvMemory(
        db_url=MEM_DB_URL,
        embedding_client=FastEmbedAdapter(model=EMBEDDING_MODEL_NAME),
        llm_client=PydanticAIAdapter(
            model=get_openai_chat_model(),
        ),
        auto_process=True,
        batch_threshold=10,
    )


memory_client = _create_memory()
