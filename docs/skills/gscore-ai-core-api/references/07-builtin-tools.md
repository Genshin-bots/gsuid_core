# 七、内置工具大全

所有内置工具均已注册到全局工具注册表，可直接在插件中使用或让 AI 自动调用。完整 import 块见 [§1.10 内置工具](./01-import-cheatsheet.md)。

`category` 加载机制详见 [§3 工具分类系统](./03-tool-categories.md)。

---

## 7.1 Self 工具（`category="self"`）

只有主Agent能调用，用于核心自我操作。

### `query_user_favorability` — 查询用户好感度

```python
@ai_tools(category="self")
async def query_user_favorability(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,  # 用户ID，None时查询当前用户
) -> str
```

### `update_user_favorability` — 更新用户好感度（增量）

```python
@ai_tools(category="self")
async def update_user_favorability(
    ctx: RunContext[ToolContext],
    delta: int,                     # 好感度变化量（可为负数）
    user_id: Optional[str] = None,
) -> str
```

### `create_subagent` — 创建子Agent

```python
@ai_tools(category="self")
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,                      # 任务描述，请详细说明
    max_tokens: int = 35000,        # 子Agent最大输出 token 数
    max_iterations: int = 15,       # 子Agent最大迭代次数
    agent_profile: str = "",        # 可选：派给指定的无人格能力代理
) -> str
```

**工作流程**：
- `agent_profile` 留空（默认）：根据 `task` 向量检索工具，用内置 Plan-and-Solve
  System Prompt 创建临时泛化子 Agent，执行并返回结果。
- `agent_profile` 非空（自然语言描述，如"写代码""金融分析""调研"）：经
  `resolve_profile` 解析到对应的**无人格能力代理**执行（见 [§7.8](#78-能力代理capability-agent)），执行/表达
  分离，适合专业、易引发人格漂移的任务。

### `send_message_by_ai` — 主动发送消息

```python
@ai_tools(category="self")
async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    message_type: Literal["text", "image"],  # 消息类型
    text: Optional[str] = None,              # 文本内容
    image_id: Optional[str] = None,          # 图片资源ID
    user_id: Optional[str] = None,           # 指定目标用户ID（可选）
) -> str
```

### `add_once_task` — 添加一次性定时任务

```python
@ai_tools(category="self")
async def add_once_task(
    ctx: RunContext[ToolContext],
    run_time: str,               # 执行时间，格式 "YYYY-MM-DD HH:MM:SS"
    task_prompt: str,            # 任务描述
) -> str
```

### `add_interval_task` — 添加循环任务

```python
@ai_tools(category="self")
async def add_interval_task(
    ctx: RunContext[ToolContext],
    interval_value: int,         # 间隔值
    task_prompt: str,            # 任务描述
    interval_type: str = "minutes",  # 间隔类型: "minutes"/"hours"/"days"
    max_executions: int = 10,    # 最大执行次数（上限10）
) -> str
```

---

## 7.1.1 定时任务管理工具（`category="common"`）

定时任务的"管理"类工具（列出/查询/修改/取消/暂停/恢复）**不属于保底池**，由 `search_tools()`
按用户 query 向量检索按需加载——用户使用这些功能时通常会显式带任务 ID 或明确表达"取消任务""暂停任务"
等需求，向量命中率高。而"创建"入口 `add_once_task` / `add_interval_task` 因口语化触发（"每天下午三点
半给我推送新闻"）向量难命中，故保留在 `self` 保底池。

> 任务数据模型见 [§9 Scheduled Task](./09-scheduled-tasks.md)。

### `list_scheduled_tasks` — 列出所有定时任务

```python
@ai_tools(category="common")
async def list_scheduled_tasks(
    ctx: RunContext[ToolContext],
) -> str
```

### `query_scheduled_task` — 查询任务详情

```python
@ai_tools(category="common")
async def query_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

### `modify_scheduled_task` — 修改任务

```python
@ai_tools(category="common")
async def modify_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
    task_prompt: Optional[str] = None,    # 新的任务描述
    max_executions: Optional[int] = None, # 新的最大执行次数（仅循环任务）
) -> str
```

### `cancel_scheduled_task` — 取消任务

```python
@ai_tools(category="common")
async def cancel_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

### `pause_scheduled_task` — 暂停任务

```python
@ai_tools(category="common")
async def pause_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

### `resume_scheduled_task` — 恢复任务

```python
@ai_tools(category="common")
async def resume_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,                # 任务ID
) -> str
```

---

## 7.2 Buildin 工具（`category="buildin"`）—— 框架保底工具池

`buildin` 分类下的工具属于**框架保底工具池**，主Agent 无条件全部加载，不受向量搜索影响。

### `search_knowledge` — 知识库检索

```python
@ai_tools(category="buildin")
async def search_knowledge(
    ctx: RunContext[ToolContext],
    query: str,                      # 自然语言查询
    category: Optional[str] = None,  # 知识类别筛选（可选）
    plugin: Optional[str] = None,    # 插件来源筛选（可选）
    limit: int = 10,                 # 最大返回数量
    score_threshold: float = 0.45,   # 相似度阈值（0~1）
) -> str
```

### `web_search_tool` — Web 搜索

```python
@ai_tools(category="buildin")
async def web_search_tool(
    ctx: RunContext[ToolContext],
    query: str,          # 搜索关键词
    limit: int = 10,     # 最大结果数
) -> str
```

> **注意**：支持 Tavily / Exa / MCP 三种搜索提供方，通过 `ai_config.websearch_provider` 配置切换。使用 MCP 时需配置 `mcp_tools_config.websearch_mcp_tool_id`。

### `web_fetch_tool` — 网页抓取

```python
@ai_tools(category="buildin")
async def web_fetch_tool(
    ctx: RunContext[ToolContext],
    url: str,            # 要抓取的网页 URL
) -> str
```

### `query_user_memory` — 查询用户记忆

```python
@ai_tools(category="buildin")
async def query_user_memory(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,  # 用户ID，None时查询当前用户
) -> str
```

### `get_self_info` — 获取完整自我认知

```python
@ai_tools(category="buildin")
async def get_self_info(ctx: RunContext[ToolContext]) -> str
```

返回身份、运行框架、能力边界（按分类汇总的已注册工具）、主人列表、当前会话语境标签。
当用户问"你是谁""你能做什么""你的主人是谁"，或需要判断任务是否在能力范围内时调用。

### `state_get` / `state_set` / `state_delete` / `state_list` / `state_append` — 通用持久状态存储

框架级别的跨会话键值存储，让复杂任务（虚拟账户、任务进度、报名名单等）的结构化状态在会话结束后依然存活。属于框架保底工具，任何 session 默认注入。

```python
@ai_tools(category="buildin")
async def state_set(
    ctx: RunContext[ToolContext],
    key: str,                       # 键名，建议格式 "插件名:业务名"
    value: str,                     # JSON 字符串
    scope: str = "auto",            # 隔离范围，见下表
    ttl_days: Optional[int] = None, # 可选，保留天数
) -> str

@ai_tools(category="buildin")
async def state_get(ctx, key: str, scope: str = "auto") -> str

@ai_tools(category="buildin")
async def state_delete(ctx, key: str, scope: str = "auto") -> str

@ai_tools(category="buildin")
async def state_list(ctx, prefix: str = "", scope: str = "auto") -> str

@ai_tools(category="buildin")
async def state_append(
    ctx, key: str, item: str, scope: str = "auto",
    max_length: Optional[int] = None, ttl_days: Optional[int] = None,
) -> str   # 向列表型状态追加元素，原子操作，避免 get→改→set 的竞态
```

`scope` 取值：

| scope | 说明 |
|-------|------|
| `"auto"` | 按当前会话自动判断：群聊 → `group:{群ID}`，私聊 → `user:{用户ID}` |
| `"user:{id}"` | 指定用户的私有数据 |
| `"group:{id}"` | 指定群组的共享数据 |
| `"global"` | 全局共享数据 |

> 插件可直接复用底层 API（`from gsuid_core.ai_core.state_store import state_get_value, state_set_value, ...`）来构建有状态功能，无需关心存储细节。

---

## 7.3 Common 工具（`category="common"`）

不属于保底工具池，通过向量检索按需加载，当用户明确需要相关功能时才会出现在工具列表中。

### `search_image` — 图片检索

```python
@ai_tools(category="common")
async def search_image(
    ctx: RunContext[ToolContext],
    query: str,                      # 自然语言查询
    limit: int = 10,                 # 最大返回数量
    score_threshold: float = 0.45,   # 相似度阈值（0~1）
) -> str
```

### `get_self_persona_info` — 获取自身 Persona 资源信息

```python
@ai_tools(category="common")
async def get_self_persona_info(
    ctx: RunContext[ToolContext],
    info_type: Literal["config", "image", "avatar", "audio"],
    persona_name: str,              # Persona 名称
) -> str
```

**info_type 说明**：`"config"` 返回 config.json 配置；`"image"` / `"avatar"` / `"audio"` 返回对应资源文件路径。

> 完整的自我认知请使用保底工具 `get_self_info`；本工具仅用于获取具体的 Persona 资源文件路径。

### `set_user_favorability` — 设置用户好感度（绝对值）

```python
@ai_tools(category="common")
async def set_user_favorability(
    ctx: RunContext[ToolContext],
    value: int,                     # 好感度绝对值
    user_id: Optional[str] = None,
) -> str
```

### `send_meme` — 发送表情包

```python
@ai_tools(category="common")
async def send_meme(
    ctx: RunContext[ToolContext],
    mood: Optional[str] = None,    # 情绪标签
    scene: Optional[str] = None,   # 场景标签
) -> str
```

### `collect_meme` — 收藏表情包

```python
@ai_tools(category="common")
async def collect_meme(
    ctx: RunContext[ToolContext],
    url: str,                      # 图片URL
    tags: Optional[str] = None,   # 自定义标签
) -> str
```

### `search_meme` — 搜索表情包

```python
@ai_tools(category="common")
async def search_meme(
    ctx: RunContext[ToolContext],
    query: str,                    # 搜索关键词
) -> str
```

### `create_persistent_agent_tool` — 创建持久化子Agent

```python
@ai_tools(category="common")
async def create_persistent_agent_tool(
    ctx: RunContext[ToolContext],
    name: str,                     # Agent 名称
    system_prompt: str,            # 系统提示词
    idle_timeout_minutes: int = 60, # 空闲超时时间（分钟）
) -> str
```

### `send_agent_task_tool` — 向持久化Agent发送任务

```python
@ai_tools(category="common")
async def send_agent_task_tool(
    ctx: RunContext[ToolContext],
    agent_name: str,               # Agent 名称
    task: str,                     # 任务描述
) -> str
```

### `list_agents_tool` — 列出所有活跃的持久化Agent

```python
@ai_tools(category="common")
async def list_agents_tool(
    ctx: RunContext[ToolContext],
) -> str
```

### `stop_agent_tool` — 停止指定的持久化Agent

```python
@ai_tools(category="common")
async def stop_agent_tool(
    ctx: RunContext[ToolContext],
    agent_name: str,               # Agent 名称
) -> str
```

---

## 7.4 Media 工具（`category="media"`）

多媒体渲染工具，主Agent可调用。

### `render_html_to_image` — 将HTML渲染为图片

```python
@ai_tools(category="media")
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html: str,                     # HTML 内容
    width: int = 800,              # 渲染宽度
) -> str
```

### `render_markdown_to_image` — 将Markdown渲染为图片

```python
@ai_tools(category="media")
async def render_markdown_to_image(
    ctx: RunContext[ToolContext],
    markdown: str,                 # Markdown 内容
    width: int = 800,              # 渲染宽度
) -> str
```

---

## 7.5 Default 工具（`category="default"`）

通过 `create_subagent` 调用，用于文件操作、代码执行等。

### `execute_shell_command` — 执行系统命令

```python
@ai_tools(category="default")
async def execute_shell_command(
    ctx: RunContext[ToolContext],
    command: str,                   # 要执行的命令
    timeout: int = 30,              # 超时时间（秒）
) -> str
```

> ⚠️ **安全警告**：此工具执行系统命令，需要通过 `check_func` 严格控制权限，建议仅在沙箱环境中开放。

### `get_current_date` — 获取当前日期时间

```python
@ai_tools(category="default")
async def get_current_date(
    ctx: RunContext[ToolContext],
    timezone: str = "Asia/Shanghai",  # 时区
) -> str
```

### `read_file_content` — 读取文件

```python
@ai_tools(category="default")
async def read_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,  # 相对于 FILE_PATH 的路径，如 "data/config.json"
) -> str
```

> **安全**：有路径遍历攻击防护，只能读取 `FILE_PATH` 目录下的文件

### `write_file_content` — 写入文件

```python
@ai_tools(category="default")
async def write_file_content(
    ctx: RunContext[ToolContext],
    file_path: str,         # 相对于 FILE_PATH 的路径
    content: str,           # 要写入的内容
    overwrite: bool = True, # 是否覆盖已存在的文件
) -> str
```

### `execute_file` — 执行脚本

```python
@ai_tools(category="default")
async def execute_file(
    ctx: RunContext[ToolContext],
    file_path: str,     # 相对于 FILE_PATH 的脚本路径
    timeout: int = 30,  # 超时时间（秒）
) -> str
```

### `diff_file_content` — 文件对比

```python
@ai_tools(category="default")
async def diff_file_content(
    ctx: RunContext[ToolContext],
    file_path_a: str,  # 第一个文件路径
    file_path_b: str,  # 第二个文件路径
) -> str
```

### `list_directory` — 列出目录

```python
@ai_tools(category="default")
async def list_directory(
    ctx: RunContext[ToolContext],
    dir_path: str = "",        # 相对于 FILE_PATH 的目录路径，空字符串表示根目录
    recursive: bool = False,   # 是否递归列出子目录
) -> str
```

---

## 7.6 动态工具发现（未注册为 AI 工具）

> **注意**：以下两个函数的 `@ai_tools` 装饰器已被注释掉，**不会自动注册为 AI 工具**。它们仅作为可手动调用的辅助函数存在。

### `discover_tools` — 发现可能需要的新工具

```python
async def discover_tools(
    ctx: RunContext[ToolContext],
    task: str,               # 任务描述
    limit: int = 5,          # 最大返回工具数量
) -> str
```

### `list_available_tools` — 列出可用工具

```python
async def list_available_tools(
    ctx: RunContext[ToolContext],
    category: Optional[str] = None,  # 可选，按分类筛选
) -> str
```

---

## 7.7 Kanban 任务编排工具（`category="buildin"` / `"self"`）

多步、多代理协作的任务由 `ai_core/planning` 模块的两张持久化表
（`AIAgentTask` + `AIAgentTaskLog`）和 `AIAgentArtifact` Hub 承载，进程重启不丢。
插件无需直接操作这些表——以下工具已注册为框架保底工具，主 Agent 自主调用：

| 工具 | 用途 |
|------|------|
| `evaluate_agent_mesh_capability` | 创建任务树**前置**：让 capability_evaluator 评估现有画像是否覆盖任务，返回 covered / missing / suggested_subtasks |
| `register_kanban_task` | 注册一棵任务树（根 + N 子任务节点），事件驱动并发推进 |
| `respawn_subtask` | 复活 failed 子任务（最多 3 次后强制转 waiting_approval） |
| `fail_task_tree` | 明确终结整棵任务树 + 级联未完成子任务 |
| `respond_subtask_approval` | 转达主人对 waiting_approval 子任务的同意 / 拒绝 |
| `artifact_put` / `artifact_get` / `artifact_list` | 任务树内 Artifact Hub 增 / 取 / 列 |
| `artifact_get_recent` | 取根任务最近一份 artifact 原文，专给主人格追问溯源用 |

**关键约束**：

- 真实 ID（`task_id` / `root_task_id`）由框架代管，**绝不作为 LLM 工具参数暴露**；
  任务引用一律走自然语言句柄 + 框架解析；artifact 用显式 `res_xxx` 句柄。
- **没有定时器**：Kanban 纯事件驱动，需要"明天 6 点触发""每天复盘"这类时间
  触发，请用 [§7.1](#71-self-工具categoryself) 的 `add_once_task` / `add_interval_task` 在那个时刻把主人格
  唤醒，再由主人格视情况调 `register_kanban_task`。
- artifact 跨 `root_task_id` 严格隔离；同一任务树内才能通过 `artifact_get`
  互读。

**能力代理推进**：Kanban 调度器把每个子任务派给画像对应的**无人格能力代理**
（`run_capability_agent`）执行，结果再经主人格 `_persona_relay` 转译后通知主人。
`create_subagent` 也支持 `agent_profile` 参数（即时委派单步任务），见下方 [§7.8](#78-能力代理capability-agent)。

---

## 7.8 能力代理（Capability Agent）

能力代理是**无人格**的专职执行角色，把「执行」从「人格表达」剥离：主人格只做
识别派发 / 查进度 / 转译汇报，执行交给专职代理（不拒绝、不漂移）。现行多步任务
统一由 Kanban 任务树承载：主人格先调 `evaluate_agent_mesh_capability` 评估画像覆盖，
再调 `register_kanban_task` 创建根任务 + 子任务；每个子任务由 `agent_profile` 指定的
能力代理推进，结果经 `_persona_relay` 用人格口吻回告。

框架内置 6 个通用画像：`research_agent` / `code_agent` / `aigc_creator` /
`data_analyst` / `memory_curator` / `scheduler_assistant`。`capability_evaluator` 是内部
专用画像，只服务 `evaluate_agent_mesh_capability`，插件不要引用或覆盖它。业务画像
（如 `finance_agent`、`weather_agent`、`game_data_agent`）由插件自行注册。

### 7.8.1 插件创建并注册业务画像

插件通常在自身启动模块或插件入口导入时注册画像。注册表是进程内存数据，后写覆盖
前写：插件可注册新 `profile_id`，也可用同名 `profile_id` 覆盖内置画像；WebConsole
用户画像启动加载后也可覆盖同名内置 / 插件画像。

```python
# plugins/SayuStock/startup.py
from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,
    register_capability_agent,
)

FINANCE_PROMPT = """你是一个严谨的「量化操盘代理」。你没有任何角色人格，
只对任务结果负责，不做角色扮演、不加语气词。

【工作流】
1. 规划：先输出 <TODO_LIST>，把任务拆成 2~5 步。
2. 执行：优先调用当前工具列表中的金融专业工具：
   - 行情查询：send_stock_info / send_my_stock / search_stock
   - 估值：send_stock_PB_info（PB/PE/PS）
   - 资金流向：send_cloudmap_img（板块资金云图）
   - 市场情绪：get_vix_index（A 股 VIX）
3. 决策必须基于工具数据：选股、加减仓、止损止盈都要回答清楚
   “从哪个工具的哪段数据得到的结论”，禁止只凭 web_search 的新闻标题做决定。
4. 在 Kanban 子任务中完成执行后，用 artifact_put 把主要产出登记成 res 句柄。
5. 高风险动作（实盘下单 / 修改持仓）一律不自己执行，在交付摘要里显式列出
   “需要主人决策的动作”，让主人格转告主人定夺。

【交付格式】
① 决定 / 推荐（简洁可执行）；
② 依据：逐条列理由 + 数据来源（哪个工具 / 字段 / 数值）；
③ 风险提示。
"""


def register_finance_agent() -> None:
    register_capability_agent(CapabilityAgentProfile(
        profile_id="finance_agent",
        display_name="操盘助手",
        when_to_use="需要查行情、做仓位决策、每日复盘的金融任务",
        system_prompt=FINANCE_PROMPT,
        match_keywords=["炒股", "操盘", "股票", "金融", "行情", "选股"],
        tool_names=[
            "send_stock_info",
            "send_my_stock",
            "send_my_stock_img",
            "send_stock_PB_info",
            "search_stock",
            "get_vix_index",
            "send_cloudmap_img",
        ],
        tool_query="",
        max_iterations=25,
        max_tokens=40000,
    ))


register_finance_agent()
```

### 7.8.2 字段含义与写法约束

| 字段 | 插件应该怎么填 |
|-----|----------------|
| `profile_id` | 稳定唯一句柄，如 `finance_agent`；主人格 / Kanban 子任务会保存这个值 |
| `display_name` | 给用户和 WebConsole 看的名称 |
| `when_to_use` | 一句话说明何时派给该画像，供评估代理和人工管理理解 |
| `system_prompt` | 纯职能 Plan-and-Solve 提示词；禁止写人格口吻、好感度、角色扮演 |
| `match_keywords` | 自然语言 hint 命中词，如主人格传 `agent_profile="操盘"` 时可解析到本画像 |
| `tool_names` | 只写业务专业工具名；框架会额外附加 Artifact / state / search / web 等永远工具 |
| `tool_query` | 可选的工具向量检索查询；已有明确白名单时可留空 |
| `max_iterations` / `max_tokens` | 单次能力代理执行预算 |

`tool_names` 应只列插件提供的专业工具。框架会自动附加 `_ALWAYS_TOOLS`：
`artifact_put` / `artifact_get` / `artifact_list`、`state_*`、`search_knowledge`、
`web_search_tool` / `web_fetch_tool` 等基础能力；不要为了“保险”重复写入。

### 7.8.3 与 Kanban / `create_subagent` 的关系

- 复合多步任务：主人格按决策树先调 `evaluate_agent_mesh_capability`，覆盖后调
  `register_kanban_task`。子任务里的 `agent_profile` 必须是已注册画像，调度器运行时
  才解析画像，因此插件晚于 `init_planning` 注册也可生效。
- 即时单步委派：`create_subagent` 仍支持 `agent_profile` 参数，适合马上执行的一次性
  专项任务；复杂依赖、并行、多产物任务应交给 Kanban。
- 专业域诚实底线：如果插件未注册金融 / 医疗 / 法律等专业画像和工具，评估代理应返回
  `covered=false`，主人格不得强行创建任务树；`research_agent` 也会避免只靠通用搜索给
  高风险专业建议。

| API | 用途 |
|-----|------|
| `register_capability_agent(profile)` | 注册一个能力代理画像；同名后写覆盖前写 |
| `unregister_capability_agent(profile_id)` | 从内存注册表移除一个画像；返回是否真的删了一项 |
| `CapabilityAgentProfile` | 画像数据类 |
| `resolve_profile(hint, default)` | 自然语言 hint → `profile_id` |
| `get_profile(profile_id)` / `list_profiles()` | 查询注册表 |
| `run_capability_agent(profile_id, task, ev, bot, ...)` | 实例化并运行一个能力代理；插件通常不直接调用，Kanban 调度器会调用 |

> ⚠️ **不要直接访问 `registry._PROFILES` 内部字典**——请使用
> `register_capability_agent` / `unregister_capability_agent` / `get_profile` /
> `list_profiles` 等公开 API，避免破坏 WebConsole 三态来源和用户画像覆盖流程。

---

## 7.9 `self_model` 演化层（自我认知 4 字段）

实现：`gsuid_core/ai_core/self_cognition.py`。存储：`state_store` 表，
scope = `self:{bot_id}`，state_key = `self_model`，value 为 4 字段字典。
每轮对话由 `handle_ai` 调用 `build_self_cognition_context` 拼成"【关于我自己】"段
注入到 **用户消息侧**（不进 system_prompt，避免 prompt cache 抖动）。

### 7.9.1 四个字段语义

| 字段 | 中文含义 | 写入入口 | 注入位置（每轮取最后 N 条） |
|------|---------|---------|---------------------------|
| `commitments` | 对用户作出的承诺 | `update_self_note(note_type="commitment")` / `add_self_note(..., field="commitments")` / webconsole 整字段覆盖 | "我的承诺: …"（取后 5 条） |
| `preferences_learned` | 观察 / 被告知的偏好 | `update_self_note(note_type="preference")` / `add_self_note(..., field="preferences_learned")` / webconsole 整字段覆盖 | "我学到的偏好: …"（取后 5 条） |
| `recurring_topics` | 反复出现的话题 | **当前无主动写入**，仅 webconsole 可整字段覆盖；预留给 Memory 模块后续自动回填 | "反复出现的话题: …"（取后 5 条） |
| `self_notes` | 自我复盘 / 反思 | `update_self_note(note_type="reflection")` / `add_self_note(..., field="self_notes")` / webconsole 整字段覆盖；Kanban 任务终结后的自动复盘链路当前未接入 | "我最近的反思: …"（取后 3 条） |

### 7.9.2 写入 API

| API | 位置 | 说明 |
|-----|------|------|
| `update_self_note(content, note_type)` | `buildin_tools/self_info.py`（LLM 工具） | `note_type ∈ {"preference","commitment","reflection"}` → 自动映射到对应字段 |
| `add_self_note(bot_id, content, field)` | `ai_core/self_cognition.py`（Python API） | 插件可直接调用，`field` 必须是 4 字段之一 |
| `overwrite_self_model_field(bot_id, field, items)` | `ai_core/self_cognition.py`（Python API） | 整字段覆盖（webconsole 后台用），同样受写入限流保护 |

写入限流（保护 self_model 不被刷爆）：

- 单条 ≤ 200 字符；
- 重复内容去重（同条文本被移到列表末尾视为"最新"）；
- 每字段最多 20 条，超出丢弃最早一条；
- 非法 `field` 名返回 `False` 并日志告警。

### 7.9.3 插件使用示例

```python
# 插件检测到用户表达明确偏好时，主动写入：
from gsuid_core.ai_core.self_cognition import add_self_note

await add_self_note(
    bot_id=ev.bot_id,
    content="用户偏好被称呼为「老板」",
    field="preferences_learned",
)
```

`bot_id` 缺失时退化到 `self:default` scope（多 bot 部署时建议显式传）。
