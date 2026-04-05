"""
Buildin Tools 模块

系统内建AI工具模块，提供自主型AI常用的基础工具函数。

包含工具：
- search_knowledge: 检索知识库内容，支持按类别/插件筛选
- web_search: Web搜索工具，基于Tavily API
- send_text_message: 发送文本消息给用户
- send_image_message: 发送图片消息给用户
- query_user_favorability: 查询用户好感度信息
- query_user_memory: 查询用户记忆条数
- update_user_favorability: 更新用户好感度（增量）
- set_user_favorability: 设置用户好感度（绝对值）
- execute_shell_command: 执行系统命令

所有工具均使用 @ai_tools 装饰器注册。
"""

# 工具装饰器
from gsuid_core.ai_core.register import ai_tools

# RAG检索工具 - 知识库查询，支持类别/插件筛选
from gsuid_core.ai_core.buildin_tools.rag_search import search_knowledge

# Web搜索工具 - 基于Tavily的web搜索
from gsuid_core.ai_core.buildin_tools.web_search import web_search

# 数据库查询工具 - 查询用户数据
from gsuid_core.ai_core.buildin_tools.database_query import (
    query_user_memory,
    query_user_favorability,
)

# 消息发送工具 - 主动发送消息
from gsuid_core.ai_core.buildin_tools.message_sender import (
    send_message_by_ai,
)

# 命令执行工具 - 执行系统命令
from gsuid_core.ai_core.buildin_tools.command_executor import execute_shell_command

# 好感度管理工具 - 管理用户好感度
from gsuid_core.ai_core.buildin_tools.favorability_manager import (
    set_user_favorability,
    update_user_favorability,
)

__all__ = [
    # 工具装饰器
    "ai_tools",
    # RAG检索工具
    "search_knowledge",
    # Web搜索工具
    "web_search",
    # 消息发送工具
    "send_message_by_ai",
    # 命令执行工具
    "execute_shell_command",
    # 数据库查询工具
    "query_user_favorability",
    "query_user_memory",
    # 好感度管理工具
    "update_user_favorability",
    "set_user_favorability",
]
