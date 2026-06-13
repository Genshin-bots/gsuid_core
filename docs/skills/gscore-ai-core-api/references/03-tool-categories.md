# 三、工具分类系统（category）

## 3.1 架构概览

工具注册表采用**分类字典**结构：

```python
# 内部结构
_TOOL_REGISTRY: Dict[str, Dict[str, ToolBase]] = {
    "self": {
        "query_user_favorability": ToolBase(...),
        "update_user_favorability": ToolBase(...),
        "create_subagent": ToolBase(...),
        "send_message_by_ai": ToolBase(...),
        "add_once_task": ToolBase(...),       # 定时任务"创建"入口，口语化触发
        "add_interval_task": ToolBase(...),   # 定时任务"创建"入口，口语化触发
    },
    "buildin": {
        "search_knowledge": ToolBase(...),
        "web_search_tool": ToolBase(...),
        "web_fetch_tool": ToolBase(...),
        "query_user_memory": ToolBase(...),
        "get_self_info": ToolBase(...),
        "state_get": ToolBase(...),
        "state_set": ToolBase(...),
        "state_delete": ToolBase(...),
        "state_list": ToolBase(...),
        "state_append": ToolBase(...),
    },
    "common": {
        "search_image": ToolBase(...),
        "get_self_persona_info": ToolBase(...),
        "set_user_favorability": ToolBase(...),
        "send_meme": ToolBase(...),
        "collect_meme": ToolBase(...),
        "search_meme": ToolBase(...),
        # 定时任务"管理"类——用户显式提需求时按需向量检索加载
        "list_scheduled_tasks": ToolBase(...),
        "query_scheduled_task": ToolBase(...),
        "modify_scheduled_task": ToolBase(...),
        "cancel_scheduled_task": ToolBase(...),
        "pause_scheduled_task": ToolBase(...),
        "resume_scheduled_task": ToolBase(...),
        "create_persistent_agent_tool": ToolBase(...),
        "send_agent_task_tool": ToolBase(...),
        "list_agents_tool": ToolBase(...),
        "stop_agent_tool": ToolBase(...),
    },
    "media": {
        "render_html_to_image": ToolBase(...),
        "render_markdown_to_image": ToolBase(...),
    },
    "default": {
        "execute_shell_command": ToolBase(...),
        "get_current_date": ToolBase(...),
        "read_file_content": ToolBase(...),
        "write_file_content": ToolBase(...),
        "execute_file": ToolBase(...),
        "diff_file_content": ToolBase(...),
        "list_directory": ToolBase(...),
    },
    "my_plugin": {
        "my_custom_tool": ToolBase(...),
    }
}
```

## 3.2 分类说明

| 分类名 | 说明 | 加载方式 |
|--------|------|-----------|
| `"self"` | 核心自我操作工具，只有主Agent能调用 | **保底**：无条件全部加载进主Agent |
| `"buildin"` | 框架基础工具（搜索/记忆/自我认知/持久状态等） | **保底**：无条件全部加载进主Agent |
| `"common"` | 通用工具，有选择地调用 | 向量检索按需加载 |
| `"media"` | 多媒体渲染工具 | 向量检索按需加载 |
| `"default"` | 子Agent工具，需通过 `create_subagent` 调用 | 子Agent向量检索按需加载 |
| `"mcp"` | MCP 外部工具，启动时自动注册 | 向量检索按需加载 |
| `"<自定义>"` | 插件自定义分类 | 向量检索按需加载 |

> **框架保底工具池**：`self` 与 `buildin` 两个分类构成"框架保底工具池"，
> `get_main_agent_tools()` 会把这两个分类下的工具**无条件全部加载**进主Agent，不受向量搜索影响。
> 一个工具是否属于保底池，**完全由它注册时声明的 `category` 决定**，框架内不存在硬编码的工具名单。
> 因此插件若希望某个工具成为主Agent的保底工具，注册时使用 `category="buildin"` 即可。

## 3.3 Agent 调用架构

```
┌─────────────────────────────────────────────────────┐
│              主Agent (Main Agent)                   │
│    使用 category="self", "buildin", "common", "media" │
│                                                     │
│  Self工具（保底，全部加载）:                          │
│  - query_user_favorability - update_user_favorability│
│  - create_subagent        - send_message_by_ai      │
│  - add_once_task          - add_interval_task        │
│                                                     │
│  Buildin工具（保底，全部加载）:                       │
│  - search_knowledge       - web_search_tool          │
│  - web_fetch_tool         - query_user_memory        │
│  - get_self_info                                     │
│  - state_get/set/delete/list/append                  │
│                                                     │
│  Common工具（向量检索按需加载）:                      │
│  - search_image           - get_self_persona_info    │
│  - set_user_favorability                             │
│  - send_meme              - collect_meme             │
│  - search_meme                                      │
│  - list_scheduled_tasks   - query_scheduled_task     │
│  - modify_scheduled_task  - cancel_scheduled_task    │
│  - pause_scheduled_task   - resume_scheduled_task    │
│  - create_persistent_agent - send_agent_task         │
│  - list_agents            - stop_agent               │
│                                                     │
│  Media工具:                                          │
│  - render_html_to_image   - render_markdown_to_image │
└─────────────────────────┬───────────────────────────┘
                          │ create_subagent()
                          ▼
┌─────────────────────────────────────────────────────┐
│              子Agent (Sub Agent)                    │
│          使用 category="default" 的工具              │
│                                                     │
│  - execute_shell_command  - get_current_date         │
│  - read_file_content     - write_file_content        │
│  - execute_file          - diff_file_content         │
│  - list_directory                                    │
└─────────────────────────────────────────────────────┘
```

## 3.4 插件工具分类建议

插件开发时，工具注册推荐使用：

```python
# 简单工具（通过子Agent调用）
@ai_tools(category="default")
async def my_simple_tool(query: str) -> str:
    """简单查询工具"""
    ...

# 核心工具（主Agent直接调用，需谨慎）
@ai_tools(category="common")
async def my_core_tool(ctx: RunContext[ToolContext], uid: str) -> str:
    """核心工具，主Agent直接调用"""
    ...

# 插件专属分类
@ai_tools(category="genshin")
async def genshin_query(ctx: RunContext[ToolContext], character: str) -> str:
    """原神角色查询"""
    ...
```

关于保底池的"主Agent 工具集 = 保底池 + 语境池 + 查询池"组合详见 [§5.3 `get_main_agent_tools`](./05-create-agent.md)。
