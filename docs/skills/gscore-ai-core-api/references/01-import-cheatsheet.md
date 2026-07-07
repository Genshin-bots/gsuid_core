# 一、模块导入速查

> **核心模块路径**：`gsuid_core/ai_core/`
>
> 本章把 AI Core 给插件暴露的所有 public API 按模块归类，**复制即用**。
> 大多数插件只需要 import 极少量符号；这里列全是为方便查阅。

## 1.1 工具注册装饰器

```python
from gsuid_core.ai_core.register import (
    ai_tools,                # 工具注册装饰器
    ai_entity,               # 知识库注册
    ai_alias,                # 别名注册
    ai_image,                # 图片实体注册
    add_manual_knowledge,    # 手动知识添加
    update_manual_knowledge, # 手动知识更新
    delete_manual_knowledge, # 手动知识删除
    get_manual_entities,     # 获取所有手动知识
    get_manual_entity,       # 获取指定手动知识
    get_registered_tools,    # 获取所有已注册工具（按分类）
    get_all_tools,           # 获取所有已注册工具（平铺结构）
)
```

详见 [§6 知识库与别名](./06-knowledge-and-alias.md)、[§10 注册表 + 类型](./10-registry-and-types.md)。

## 1.2 触发器 → AI 工具桥接

```python
from gsuid_core.ai_core.trigger_bridge import (
    ai_return,               # 在触发器函数内向 AI 返回纯文本中间结果
)
```

详见 [§4 触发器桥接](./04-trigger-bridge.md)。

## 1.3 Agent 创建

```python
from gsuid_core.ai_core.gs_agent import (
    create_agent,           # 创建临时 Agent
)

from gsuid_core.ai_core.rag.tools import (
    get_main_agent_tools,   # 获取主Agent工具列表
)
```

详见 [§5 `create_agent` 与 Agent 架构](./05-create-agent.md)。

## 1.4 AI 聊天入口

```python
from gsuid_core.ai_core.handle_ai import handle_ai_chat
```

详见 [§5.4 `handle_ai_chat`](./05-create-agent.md)。

## 1.5 工具上下文与 PydanticAI

```python
from gsuid_core.ai_core.models import ToolContext

from pydantic_ai import RunContext
```

详见 [§10.1 `ToolContext`](./10-registry-and-types.md)、[§2.3 函数签名模式](./02-ai-tools-decorator.md)。

## 1.6 数据模型

```python
from gsuid_core.ai_core.models import (
    KnowledgeBase,       # 知识库基类
    KnowledgePoint,      # 知识点（插件注册）
    ManualKnowledgeBase, # 手动知识
    ImageEntity,         # 图片实体
    ToolBase,            # 工具元数据
)
```

详见 [§10.2–10.6 类型定义](./10-registry-and-types.md)。

## 1.7 MCP 工具集成

```python
from gsuid_core.ai_core.mcp import (
    MCPClient,               # MCP 客户端
    MCPToolInfo,             # MCP 工具信息
    MCPToolResult,           # MCP 工具调用结果
    MCPConfig,               # MCP 配置数据类
    MCPToolDefinition,       # MCP 工具定义
    MCPConfigManager,        # MCP 配置管理器
    mcp_config_manager,      # 全局 MCP 配置管理器单例
    MCP_PRESETS,             # MCP 预设配置列表
    parse_mcp_tool_id,       # 解析 MCP 工具 ID
    format_mcp_tool_id,      # 格式化 MCP 工具 ID
    register_all_mcp_tools,  # 注册所有 MCP 工具
    register_single_mcp_server,  # 注册单个 MCP 服务器
    unregister_mcp_server,   # 注销 MCP 服务器
    get_mcp_server,          # 获取 MCP Server 实例
    get_mcp_trigger_count,   # 获取 MCP 触发器注册数量
)

from gsuid_core.ai_core.mcp.mcp_tool_caller import (
    call_mcp_tool,           # 通用 MCP 工具调用
)

from gsuid_core.ai_core.mcp.mcp_tools_config import (
    mcp_tools_config,        # MCP 工具配置（websearch/image_understand）
)
```

详见 [§11.1 MCP 工具集成](./11-mcp-image-search-and-meme.md)。

## 1.8 Image Understand 图片理解

```python
from gsuid_core.ai_core.image_understand import (
    understand_image,        # 统一图片理解接口
)
```

详见 [§11.2 Image Understand](./11-mcp-image-search-and-meme.md)。

## 1.9 Web Search 统一搜索

```python
from gsuid_core.ai_core.web_search.search import (
    web_search,              # 统一搜索接口
    web_search_with_context, # 带上下文的搜索接口
)
```

详见 [§11.3 Web Search](./11-mcp-image-search-and-meme.md)。

## 1.10 内置工具（按 category 分类）

```python
from gsuid_core.ai_core.buildin_tools import (
    # --- Self 工具 (category="self") --- 保底池，无条件全部加载
    # 只有主Agent能调用，用于核心操作
    query_user_favorability,    # 查询用户好感度
    update_user_favorability,   # 更新用户好感度（增量）
    create_subagent,            # 创建子Agent完成特定任务
    send_message_by_ai,         # 发送消息给用户
    add_once_task,              # 添加一次性定时任务（创建入口，口语化触发）
    add_interval_task,          # 添加循环任务（创建入口，口语化触发）

    # --- Buildin 工具 (category="buildin") --- 保底池，无条件全部加载
    # 主Agent调用时也会加载，直接调用不会拒绝
    search_knowledge,           # 知识库检索
    web_search_tool,            # Web搜索
    web_fetch_tool,             # 网页抓取（转Markdown）
    query_user_memory,          # 查询用户记忆
    get_self_info,              # 获取完整自我认知（身份/能力边界/主人）
    state_get,                  # 读取通用持久状态
    state_set,                  # 写入通用持久状态
    state_delete,               # 删除通用持久状态
    state_list,                 # 列出通用持久状态键
    state_append,               # 向列表型持久状态追加元素

    # --- Common 工具 (category="common") ---
    # 不属于保底池，向量检索按需加载，用户明确需要相关功能时才出现
    search_image,               # 图片检索
    get_self_persona_info,      # 获取自身Persona资源信息
    set_user_favorability,      # 设置用户好感度（绝对值）
    send_meme,                  # 发送表情包
    collect_meme,               # 收藏表情包
    search_meme,                # 搜索表情包
    list_scheduled_tasks,       # 列出所有定时任务（管理类）
    query_scheduled_task,       # 查询任务详情（管理类）
    modify_scheduled_task,      # 修改任务（管理类）
    cancel_scheduled_task,      # 取消任务（管理类）
    pause_scheduled_task,       # 暂停任务（管理类）
    resume_scheduled_task,      # 恢复任务（管理类）
    create_persistent_agent_tool,  # 创建持久化子Agent
    send_agent_task_tool,       # 向持久化Agent发送任务
    list_agents_tool,           # 列出所有活跃的持久化Agent
    stop_agent_tool,            # 停止指定的持久化Agent

    # --- Media 工具 (category="media") ---
    # 多媒体渲染工具
    render_html_to_image,       # 将HTML渲染为图片
    render_markdown_to_image,   # 将Markdown渲染为图片

    # --- Default 工具 (category="default") ---
    # 通过 create_subagent 调用，用于文件操作、代码执行等
    execute_shell_command,      # 执行系统命令
    get_current_date,           # 获取当前日期时间
    read_file_content,          # 读取文件
    write_file_content,         # 写入文件
    execute_file,               # 执行脚本
    diff_file_content,          # 文件对比
    list_directory,             # 列出目录

    # --- 动态工具发现（未注册为AI工具，仅可手动调用） ---
    discover_tools,             # 发现可能需要的新工具
    list_available_tools,       # 列出可用工具
)
```

每个工具的**完整签名**见 [§7 内置工具大全](./07-builtin-tools.md)。

## 1.11 RAG / 向量检索

```python
from gsuid_core.ai_core.rag import (
    init_embedding_model,
    sync_knowledge,
    query_knowledge,
    sync_images,
    search_images,
    search_and_load_image,
    init_knowledge_collection,
    init_image_collection,
    get_reranker,
    rerank_results,
)
```

> 嵌入 Provider 扩展点见 [§12 嵌入 Provider 注册表](./12-embedding-provider.md)。

## 1.12 Persona 角色系统

```python
from gsuid_core.ai_core.persona import (
    Persona,
    PersonaMetadata,
    PersonaFiles,
    build_persona_prompt,
    load_persona,
    save_persona,
    list_available_personas,
    get_persona_metadata,
    get_persona_image_path,
    get_persona_avatar_path,
    get_persona_audio_path,
    persona_config_manager,
)
```

详见 [§8.1 Persona 角色系统](./08-persona-and-memory.md)。

## 1.13 Memory 记忆系统

```python
from gsuid_core.ai_core.memory import (
    memory_config,
    ScopeType,
    make_scope_key,
    observe,
    get_observation_queue,
    ObservationRecord,
    dual_route_retrieve,
    MemoryContext,
    get_ingestion_worker,
)
```

详见 [§8.2 Memory 记忆系统](./08-persona-and-memory.md)。

## 1.14 Statistics 统计系统

```python
from gsuid_core.ai_core.statistics import (
    statistics_manager,         # 统计管理器单例，所有 record_* 方法都在此对象上
)
```

## 1.15 Scheduled Task 模型

```python
from gsuid_core.ai_core.scheduled_task import AIScheduledTask
```

详见 [§9 Scheduled Task 定时任务](./09-scheduled-tasks.md)。

## 1.11 AgentNode 统一节点层（能力代理 / 编排）

```python
from gsuid_core.ai_core.agent_node import (
    AgentNode,                  # 统一节点数据类（persona 与能力代理同构）
    register_agent_node,        # 注册节点（插件业务节点入口）
    unregister_agent_node,      # 移除节点
    get_node,                   # 按 node_id 查（含 persona 投影回落）
    list_nodes,                 # 列出节点（include_persona=True 并入 persona 投影）
    resolve_node,               # 自然语言 hint → node_id
    TASK_BASICS_PACK,           # "task_basics" 工具能力族名（建议业务节点必挂）
    DYNAMIC_PACK,               # "dynamic" 五层自动装配族名
    register_tool_pack,         # 注册自定义静态工具能力族
)

# task-mode 运行入口（Kanban 调度器调用；插件一般不直接调）
from gsuid_core.ai_core.capability_agents import run_capability_agent

# 旧 API 兼容层（下个大版本移除，勿在新代码使用）
from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,     # 【废弃】旧画像 dataclass（自动转 AgentNode）
    register_capability_agent,  # 【废弃】兼容注册入口
)
```

## 1.12 统一审批中心

```python
from gsuid_core.ai_core.approval import (
    submit,                     # 提交审批 / 交互请求
    resolve,                    # 裁决（定位 + 权限校验 + 领域回调）
    register_approval_category, # 注册自定义审批领域（on_resolve 回调）
    set_full_access,            # 「完全访问」豁免开关（仅 user 级 approval）
    is_full_access,
    has_pending,                # 内存快判（visible_when 谓词用）
    AIApprovalRequest,          # 审批请求表模型
)
```

LLM 侧配套工具（`buildin_tools/approval_tools.py`，随框架注册）：
`respond_approval` / `list_pending_approvals`（统一转达 / 列表）、
`ask_user` / `request_user_approval` / `request_master_approval`（审批交互能力族）。
工具强制审批用 `@ai_tools(approval="user"|"master")` 声明，见 [§2](./02-ai-tools-decorator.md)。
