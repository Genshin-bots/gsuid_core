"""
Buildin Tools 模块

系统内建AI工具模块，提供自主型AI常用的基础工具函数。

## 工具分类与"框架保底工具池"

工具是否属于"框架保底工具"完全由注册时声明的 `category` 决定，**不存在任何硬编码的工具名单**。
`self` 与 `buildin` 两个分类即"框架保底工具池"——`get_main_agent_tools()` 会把这两个分类下的
工具无条件全部加载进主Agent，不受向量搜索影响。其余分类（`common` / `media` / `default` /
插件的 `by_trigger` 等）通过 `search_tools()` 向量检索按需加载。

因此，若希望某个新工具成为保底工具，只需注册时使用 `category="self"` 或 `category="buildin"`。

### Self工具, 只有主Agent能调用 (category="self") —— 保底
- query_user_favorability: 查询用户好感度
- update_user_favorability: 更新用户好感度（增量）
- create_subagent: 创建子Agent完成特定任务
- send_message_by_ai: 发送消息给用户
- add_once_task: 添加一次性定时任务（创建入口，口语化触发，需常驻保底池）
- add_interval_task: 添加循环任务（创建入口，口语化触发，需常驻保底池）

### 框架基础工具 (category="buildin") —— 保底
主Agent无条件全部加载，覆盖搜索、记忆、自我认知、持久状态等"任何任务都可能需要"的能力。
- search_knowledge: 检索知识库内容
- web_search_tool: Web搜索工具
- web_fetch_tool: 网页抓取工具（将网页转换为Markdown）
- query_user_memory: 查询用户记忆
- get_self_info: 获取完整自我认知（身份/能力边界/主人）
- state_get / state_set / state_delete / state_list / state_append: 通用持久状态存储

### 通常工具 (category="common")
不属于保底池，通过向量检索按需加载，当用户明确需要相关功能时才会出现在工具列表中。
- search_image: 检索图片资源
- get_self_persona_info: 获取自身Persona资源信息（立绘/头像/音频路径等）
- set_user_favorability: 设置用户好感度（绝对值）
- send_meme: 发送表情包
- collect_meme: 收藏表情包
- search_meme: 搜索表情包
- list_scheduled_tasks: 列出所有定时任务（管理类，用户显式提需求时按需检索）
- query_scheduled_task: 查询任务详情（管理类，用户显式提需求时按需检索）
- modify_scheduled_task: 修改任务（管理类，用户显式提需求时按需检索）
- cancel_scheduled_task: 取消任务（管理类，用户显式提需求时按需检索）
- pause_scheduled_task: 暂停任务（管理类，用户显式提需求时按需检索）
- resume_scheduled_task: 恢复任务（管理类，用户显式提需求时按需检索）
- create_persistent_agent_tool: 创建持久化子Agent
- send_agent_task_tool: 向持久化Agent发送任务
- list_agents_tool: 列出所有活跃的持久化Agent
- stop_agent_tool: 停止指定的持久化Agent

### 子Agent工具 (category="default")
通过 create_subagent 调用，用于文件操作、代码执行等。
- execute_shell_command: 执行系统命令
- get_current_date: 获取当前日期时间
- read_file_content: 读取文件内容
- write_file_content: 写入文件内容
- execute_file: 执行脚本文件
- diff_file_content: 对比文件差异
- list_directory: 列出目录内容

所有工具均使用 @ai_tools(category=...) 装饰器注册。
"""

# 工具装饰器
from gsuid_core.ai_core.register import ai_tools

# 通用持久状态存储工具 - 跨会话的结构化键值存储
from gsuid_core.ai_core.state_store import (
    state_get,
    state_set,
    state_list,
    state_append,
    state_delete,
)

# AI日期工具 - 获取当前日期时间
from gsuid_core.ai_core.buildin_tools.get_time import (
    get_current_date,
    _get_current_date,
)

# Subagent工具 - 创建子Agent完成特定任务
from gsuid_core.ai_core.buildin_tools.subagent import create_subagent

# 定时任务工具 - 管理定时/循环任务（增删改查启停）
from gsuid_core.ai_core.buildin_tools.scheduler import (
    add_once_task,
    add_interval_task,
    list_scheduled_tasks,
    pause_scheduled_task,
    query_scheduled_task,
    cancel_scheduled_task,
    modify_scheduled_task,
    resume_scheduled_task,
)

# 自我信息工具 - 获取自身Persona信息与完整自我认知
from gsuid_core.ai_core.buildin_tools.self_info import (
    get_self_info,
    get_self_persona_info,
)

# 网页抓取工具 - 抓取网页内容并转换为Markdown
from gsuid_core.ai_core.buildin_tools.web_fetch import web_fetch_tool

# 表情包工具 - 发送/收藏/搜索表情包
from gsuid_core.ai_core.buildin_tools.meme_tools import (
    send_meme,
    search_meme,
    collect_meme,
)

# RAG检索工具 - 知识库查询，支持类别/插件筛选
from gsuid_core.ai_core.buildin_tools.rag_search import (
    search_image,
    search_knowledge,
)

# Web搜索工具 - 基于Tavily的web搜索
from gsuid_core.ai_core.buildin_tools.web_search import web_search_tool

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

# Agent Mesh 工具 - 持久化 Agent 协作
from gsuid_core.ai_core.buildin_tools.agent_mesh_tools import (
    stop_agent_tool,
    list_agents_tool,
    send_agent_task_tool,
    create_persistent_agent_tool,
)

# 命令执行工具 - 执行系统命令
from gsuid_core.ai_core.buildin_tools.command_executor import execute_shell_command

# HTML渲染工具 - 将HTML/Markdown渲染为图片
from gsuid_core.ai_core.buildin_tools.html_render_tools import (
    render_html_to_image,
    render_markdown_to_image,
)

# 好感度管理工具 - 管理用户好感度
from gsuid_core.ai_core.buildin_tools.favorability_manager import (
    set_user_favorability,
    update_user_favorability,
)

# 动态工具发现 - 允许AI搜索和发现可能需要的新工具
from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import (
    discover_tools,
    list_available_tools,
)

__all__ = [
    # 工具装饰器
    "ai_tools",
    # RAG检索工具
    "search_knowledge",
    "search_image",
    # Web搜索工具
    "web_search_tool",
    # 网页抓取工具
    "web_fetch_tool",
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
    # 表情包工具
    "send_meme",
    "collect_meme",
    "search_meme",
    # AI日期工具
    "get_current_date",
    "_get_current_date",
    # Subagent工具
    "create_subagent",
    # 自我信息工具
    "get_self_persona_info",
    "get_self_info",
    # 文件管理工具
    "read_file_content",
    "write_file_content",
    "execute_file",
    "diff_file_content",
    "list_directory",
    # 定时任务工具
    "add_once_task",
    "add_interval_task",
    "list_scheduled_tasks",
    "query_scheduled_task",
    "modify_scheduled_task",
    "cancel_scheduled_task",
    "pause_scheduled_task",
    "resume_scheduled_task",
    # 动态工具发现
    "discover_tools",
    "list_available_tools",
    # 通用持久状态存储工具
    "state_get",
    "state_set",
    "state_delete",
    "state_list",
    "state_append",
    # Agent Mesh 工具
    "create_persistent_agent_tool",
    "send_agent_task_tool",
    "list_agents_tool",
    "stop_agent_tool",
    # HTML渲染工具
    "render_html_to_image",
    "render_markdown_to_image",
]
