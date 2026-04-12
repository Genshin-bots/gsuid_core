"""
Buildin Tools 模块

系统内建AI工具模块，提供自主型AI常用的基础工具函数。

主Agent工具（category="buildin"，直接调用）：
- search_knowledge: 检索知识库内容，支持按类别/插件筛选
- web_search: Web搜索工具，基于Tavily API
- send_message_by_ai: 发送消息给用户
- query_user_favorability: 查询用户好感度信息
- query_user_memory: 查询用户记忆条数
- update_user_favorability: 更新用户好感度（增量）
- set_user_favorability: 设置用户好感度（绝对值）
- create_subagent: 创建子Agent完成特定任务
- get_self_persona_info: 获取自身Persona信息（配置/立绘/头像/音频）
- add_scheduled_task: 定时执行任务

子Agent工具（通过 create_subagent 调用）：
- execute_shell_command: 执行系统命令（需权限）
- get_current_date: 获取当前日期和时间
- read_file_content: 读取文件内容
- write_file_content: 写入文件内容
- execute_file: 执行脚本文件
- diff_file_content: 对比两个文件的差异
- list_directory: 列出目录内容

所有工具均使用 @ai_tools(category=...) 装饰器注册。
"""

# 工具装饰器
from gsuid_core.ai_core.register import ai_tools

# AI日期工具 - 获取当前日期时间
from gsuid_core.ai_core.buildin_tools.get_time import (
    get_current_date,
    _get_current_date,
)

# Subagent工具 - 创建子Agent完成特定任务
from gsuid_core.ai_core.buildin_tools.subagent import create_subagent

# 定时任务工具 - 定时执行任务
from gsuid_core.ai_core.buildin_tools.scheduler import add_scheduled_task

# 自我信息工具 - 获取自身Persona信息
from gsuid_core.ai_core.buildin_tools.self_info import get_self_persona_info

# RAG检索工具 - 知识库查询，支持类别/插件筛选
from gsuid_core.ai_core.buildin_tools.rag_search import search_knowledge

# Web搜索工具 - 基于Tavily的web搜索
from gsuid_core.ai_core.buildin_tools.web_search import web_search

# 文件管理工具 - 读写执行文件和diff对比
from gsuid_core.ai_core.buildin_tools.file_manager import (
    execute_file,
    list_directory,
    diff_file_content,
    read_file_content,
    write_file_content,
)

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
    # AI日期工具
    "get_current_date",
    "_get_current_date",
    # Subagent工具
    "create_subagent",
    # 自我信息工具
    "get_self_persona_info",
    # 文件管理工具
    "read_file_content",
    "write_file_content",
    "execute_file",
    "diff_file_content",
    "list_directory",
    # 定时任务工具
    "add_scheduled_task",
]
