"""
Buildin Tools 模块 —— 框架内置 AI 工具集中入口

本模块**只负责导入并 re-export 工具函数**，让其在框架启动时被 ``@ai_tools(...)``
装饰器执行、登记到全局 ``_TOOL_REGISTRY``。真正的工具实现散落在各子文件
（``file_manager.py`` / ``command_executor.py`` / ``scheduler.py`` 等）。

## 一、工具分类（category）与"框架保底池"的关系

工具是否属于"框架保底"完全由注册时声明的 ``category`` 字符串决定，
**不存在任何硬编码的工具名单**：

- ``get_main_agent_tools()``     → 加载 ``self`` + ``buildin`` 两个分类（**保底池**）。
- ``search_tools(query=...)``    → 在 ``common`` / ``media`` / ``default`` 与插件
                                   注册的 ``by_trigger`` 等分类里做向量检索按需加载。
- ``create_subagent`` 默认子代理 → 默认装配 ``default`` 分类 + ``buildin`` 部分。

要让一个新工具成为保底工具，注册时写 ``category="self"`` 或 ``category="buildin"``。
要让新工具仅在向量检索命中时出现，留 ``category="common"`` / ``"media"`` /
``"default"`` 即可。

## 二、按 category 列出所有内置工具

下表是当前 ``buildin_tools/*.py`` + ``state_store/*.py`` + ``planning/kanban_tools.py``
注册到全局注册表的完整工具清单，**真实分类来源是装饰器声明**——若实现里改了
分类而本文档没同步，请以 ``register.py`` 的 ``_TOOL_REGISTRY`` 为准。

### 2.1 ``category="self"`` —— 仅主人格保底（不会装配进能力代理）
这些是"只能由主人格直接调用"的工具：副作用强、面向用户、或会引发任务编排。

| 工具 | 来源 | 说明 |
|---|---|---|
| ``create_subagent`` | ``subagent.py`` | 派一个子 Agent 跑即时多步任务（不进 Kanban 任务树） |
| ``send_message_by_ai`` | ``message_sender.py`` | 主动以当前人格口吻发消息给主人（**仅主人格可用，能力代理禁用**） |
| ``query_user_favorability`` | ``database_query.py`` | 查询好感度（主人格读自身状态） |
| ``update_user_favorability`` | ``favorability_manager.py`` | 增量更新好感度 |
| ``add_once_task`` | ``scheduler.py`` | 注册一次性定时任务（口语触发，需常驻主人格手边） |
| ``add_interval_task`` | ``scheduler.py`` | 注册周期定时任务（同上） |
| ``evaluate_agent_mesh_capability`` | ``planning/kanban_tools.py`` | Kanban 任务树前置评估，**仅主人格调** |

### 2.2 ``category="buildin"`` —— 主人格 + 能力代理都保底
"任何任务都可能需要"的基础能力。能力代理实例化时也会通过 ``_ALWAYS_TOOLS`` 拿到大部分。

- ``search_knowledge``（``rag_search.py``）：向量检索知识库
- ``web_search_tool``（``web_search.py``）：Tavily web 搜索
- ``web_fetch_tool``（``web_fetch.py``）：抓取网页并转 Markdown
- ``query_user_memory``（``database_query.py``）：查询用户多群组记忆
- ``get_self_info``（``self_info.py``）：取完整自我认知（身份 / 能力 / 主人）
- ``state_get`` / ``state_set`` / ``state_delete`` / ``state_list`` /
  ``state_append``（``state_store/tools.py``）：通用持久键值状态
- ``record_put`` / ``record_get`` / ``record_list`` / ``record_delete`` /
  ``record_summary``（``state_store/record_tools.py``）：通用结构化集合
- ``register_kanban_task``（``planning/kanban_tools.py``）：创建 Kanban 任务树
- ``respawn_subtask`` / ``fail_task_tree`` / ``respond_subtask_approval``
  （``planning/kanban_tools.py``）：任务树重派 / 终结 / 审批
- ``artifact_put`` / ``artifact_get`` / ``artifact_list`` / ``artifact_get_recent``
  （``planning/kanban_tools.py``）：任务节点 artifact 增查

### 2.3 ``category="common"`` —— 向量检索按需加载（非保底）
用户明确表达需求时才会被 ``search_tools()`` 命中并加载。

- ``search_image``（``rag_search.py``）：图片资源向量检索
- ``get_self_persona_info``（``self_info.py``）：查 Persona 资源
- ``update_self_note``（``self_info.py``）：写 self_note
  （``capability_domain="自我认知"``）
- ``set_user_favorability``（``favorability_manager.py``）：绝对值设置好感度
- ``send_meme`` / ``collect_meme`` / ``search_meme``（``meme_tools.py``）：
  表情包发送 / 收藏 / 检索
- ``list_scheduled_tasks`` / ``query_scheduled_task`` / ``modify_scheduled_task``
  （``scheduler.py``）：定时任务管理（只读 / 改）按需
- ``cancel_scheduled_task`` / ``pause_scheduled_task`` / ``resume_scheduled_task``
  （``scheduler.py``）：定时任务停 / 起按需

### 2.4 ``category="media"`` —— 向量检索按需（图文渲染）
| 工具 | 来源 | 说明 |
|---|---|---|
| ``render_html_to_image`` | ``html_render_tools.py`` | HTML 模板 → 图片（webconsole 复用浏览器） |
| ``render_markdown_to_image`` | ``html_render_tools.py`` | Markdown → 图片 |

### 2.5 ``category="default"`` —— 沙盒 / 子 Agent 专用
``@ai_tools()`` 不传 category 即落入 ``"default"``。这些工具不在保底池，
但会被 ``create_subagent`` 装配 + 由能力代理（``code_agent`` 等）通过显式
白名单引用。

- ``read_file_content`` / ``write_file_content`` / ``diff_file_content`` /
  ``list_directory`` / ``execute_file``（``file_manager.py``）：
  Artifact Workspace 沙盒文件操作
- ``execute_shell_command``（``command_executor.py``）：沙盒 shell
  （``check_pm`` 权限校验）
- ``_get_current_date``（``get_time.py``）：当前日期时间（注册名带下划线前缀）

> 注：``dynamic_tool_discovery.py`` 里的 ``discover_tools`` / ``list_available_tools``
> 当前注释掉了 ``@ai_tools``，未在注册表中——通过 Python import 暴露给框架内部使用。

## 三、能力代理的"永远工具"

``capability_agents/runner.py::_ALWAYS_TOOLS`` 是能力代理被实例化时框架无条件
追加的工具白名单（即便画像 ``tool_names`` 忘写也不会丢这些基础能力）：

``artifact_put`` / ``artifact_get`` / ``artifact_list`` + ``state_set`` /
``state_get`` / ``state_append`` / ``state_list`` + ``search_knowledge`` +
``web_search_tool`` / ``web_fetch_tool``。

注意：``send_message_by_ai`` 不在此列——能力代理只对主人格交付结果，由
``kanban_executor._persona_relay`` 用主人格口吻转译后送达，不持有"直接和主人对话"的下行通道。
"""

# 工具装饰器
from gsuid_core.ai_core.register import ai_tools

# 通用持久状态存储工具 - 跨会话的结构化键值存储
from gsuid_core.ai_core.state_store import (
    state_get,
    state_set,
    record_get,
    record_put,
    state_list,
    record_list,
    state_append,
    state_delete,
    record_append,
    record_delete,
    record_update,
    record_summary,
)

# Kanban 任务编排工具 - 多代理协作任务树
from gsuid_core.ai_core.planning.kanban_tools import (
    artifact_get,
    artifact_put,
    artifact_list,
    fail_task_tree,
    respawn_subtask,
    artifact_get_recent,
    register_kanban_task,
    respond_subtask_approval,
    evaluate_agent_mesh_capability,
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

# R2（C5 落地后）：移除 agent_mesh 的"假持久化" PersistentAgent 及其 4 个工具。
# 跨天 / 步骤化长任务改由 ai_core/planning 的真持久化三表 + 定时唤醒承担。
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
    # 通用结构化集合工具（具名集合 + 记录原语）
    "record_put",
    "record_get",
    "record_list",
    "record_append",
    "record_update",
    "record_delete",
    "record_summary",
    # HTML渲染工具
    "render_html_to_image",
    "render_markdown_to_image",
    # Kanban 任务编排工具
    "evaluate_agent_mesh_capability",
    "register_kanban_task",
    "respawn_subtask",
    "fail_task_tree",
    "respond_subtask_approval",
    "artifact_put",
    "artifact_get",
    "artifact_list",
    "artifact_get_recent",
]
