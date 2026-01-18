from typing import Annotated

from msgspec import Meta

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import engine, get_simple_schema_info

from .register import ai_tools, get_registered_tools


@ai_tools
async def get_db_tables():
    """
    查看数据库所有表的名称。
    """
    schema = await get_simple_schema_info(engine)
    logger.trace(f"[AI System] 数据库表结构: {schema}")
    return list(schema.keys())


@ai_tools
async def get_db_schema(table_name: Annotated[str, Meta(description="表名")]):
    """
    查看数据库某张表的字段结构。
    """
    schema = await get_simple_schema_info(engine)
    logger.trace(f"[AI System] 数据库表结构: {schema}")
    return schema[table_name]


@ai_tools
async def find_tool(requirement: Annotated[str, Meta(description="你需要用工具完成什么任务？")]):
    """
    当现有工具无法满足需求时，使用此工具查找更多可用工具。
    """
    logger.info(f"[AI System] AI 正在查找工具: {requirement}")
    # 这里可以是简单的关键词匹配，也可以是向量检索
    found = []
    for name, data in get_registered_tools().items():
        if requirement in data["desc"] or requirement in name:
            found.append(data["schema"])  # 返回完整的 Schema 给 AI

    if not found:
        return "No relevant tools found."

    return f"Found tools: {[t['function']['name'] for t in found]}. You can now call them."
