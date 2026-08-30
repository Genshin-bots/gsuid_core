# 七、内置工具大全

所有内置工具均已注册到全局工具注册表，可直接在插件中使用或让 AI 自动调用。完整 import 块见 [§1.10 内置工具](./01-import-cheatsheet.md)。

`category` 加载机制详见 [§3 工具分类系统](./03-tool-categories.md)。

---

## 7.1 Self 工具（`category="self"`）

只有主Agent能调用，用于核心自我操作。

好感度由框架每轮结算，**没有**增量工具。绝对值覆盖见 §7.3 `set_user_favorability`（仅主人）。

已删除、不要再找：

| 旧名 | 替代 |
|------|------|
| `query_user_memory` | `search_cognition` |
| `update_user_favorability` | 框架 `settle_turn`；主人绝对值用 `set_user_favorability` |
| `read_persisted_output` | 保底 `read_handle` |
| `set_session_reply_mute` / `clear_session_reply_mute` | 框架套件 `gscore.session_mute`（主人指令），不是 agent 工具 |

### `create_subagent` — 委派子 Agent / 能力代理

```python
@ai_tools(category="common", capability_domain="长期任务编排")  # 实际 category 以注册为准
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,
    max_tokens: int = 35000,
    max_iterations: int = 15,
    agent_profile: str = "",   # node_id 或可 resolve 的自然语言
    transient: bool = False,   # True 仅 lookup；出图/落盘保持 False
) -> str
```

**路由约定（主人格）**：

| agent_profile | 用途 |
|---------------|------|
| （空） | 泛化 Plan-and-Solve 子 Agent |
| `research_agent` | 外部检索 → 事实包（来源+时点） |
| **`render_agent`** | **多项数据美观出图**（主路径；禁主人格自渲 HTML） |
| `code_agent` | 写代码 / 脚本真文件图 |
| 插件节点如 `stock_report_agent` | 业务专域（插件注册） |

`task` 须自带完整事实包或实体；出图任务写明「禁止再检索」。详见
[gscore-plugin-development §14](../../gscore-plugin-development/references/14-ai-capability-profile.md)。

### `send_message_by_ai` — 主动发送消息

```python
@ai_tools(category="self")
async def send_message_by_ai(
    ctx: RunContext[ToolContext],
    message_type: Literal["text", "image"],  # 消息类型
    text: Optional[str] = None,              # 文本内容
    image_id: Optional[str] = None,          # 图片资源ID
    user_id: Optional[str] = None,           # 目标用户ID，默认当前对话者（§E.3）
) -> str
```

> **统一输出闸门（2026-08）**：`text` 发送前过 `output_gate.tool_gate_feedback`
> （= `pre_send_gate(channel="tool")`；历史别名 `gate_warn_once`）——**尖括号**非法标签
> （含 `<br>` / `<bubble/>`）打回直至熔断；**OOC** 同轮首次警告、再命中非 never-release 放行。
> 通过后走 `send_chat_result(..., ooc_check=False)` 做呈现归一化。改造时不要绕过闸门，
> 详见 [gscore-development §7.12 / §12.22](../../gscore-development/references/07-tool-registry-and-agent.md)。

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

> **低俗谐音/钓鱼防线（2026-07-08 定案）**：定时任务**不做**词库内容闸门（初版词库已评审
> 移除：真实俚语覆盖率≈0 + 误杀严重）。防线在 system prompt 合规层——谐音"怀疑先验"+
> "绝不为低俗/钓鱼内容调用任何工具"，详见
> [gscore-development §12.22](../../gscore-development/references/12-developer-pitfalls.md)。

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
**多数据点出图已不在 buildin**：见 §7.4 `media` + 能力代理 `render_agent`。

### `search_cognition` — 回想（记忆 / 偏好 / 知识 / 落盘 / 产物 / 近窗 / 记录 / 图片 / 表情）

```python
@ai_tools(category="buildin", capability_domain="回想")
async def search_cognition(
    ctx: RunContext[ToolContext],
    query: str,
    kinds: Optional[str] = None,  # 逗号分隔；留空=全查
    limit: int = 12,
) -> str
```

主人格唯一「回想」动词。不查实时外网。命中公共枢纽时回执带**路径卡**（挂件目录 + 本环境事实）；
问到某一栏且能唯一选定时同一次返回该篇全文（≤6000 字，超出用 `read_handle`）。
只问正式名、或同一 slot 命中两篇，都只给路径卡不附全文。
插件/手动文只读。全文句柄：`kb_plugin:{id}` / `kb_kbdoc:{doc_id}`。
`to_` 落盘只出现在路径卡，不在工具回执里展开正文（属主 ACL 仍走 `read_handle`）。
门面 `cognition.facade.search_cognition` 仍返回 `List[CognitiveHit]`，展开在工具层。
展开失败 fail-open（联邦命中列表仍返回）。

> **不可信内容包裹**：命中列表套 `wrap_untrusted("memory_recall", …)`；选定全文另套
> `knowledge_article`；路径卡里的本群事实同样套 `memory_recall`。同一 query 本轮重入会短路并复述上次摘要；第一次无命中时不得写「含路径卡」。

### `read_handle` — 统一读句柄（全文 / 委派状态）

```python
@ai_tools(category="buildin", capability_domain="产物")
async def read_handle(
    ctx: RunContext[ToolContext],
    handle_id: str,
    offset: int = 0,
    limit: int = 8000,
) -> str
```

实现在 `planning/tool_output_tools.py`，启动时随 planning 注册，**不是**
`read_persisted_output` 别名。`to_` / `sa_` / `res_` / `img_` / `dlg_` /
`kb_plugin` / `kb_kbdoc` 都走这里；落盘属主 ACL 在句柄解析层。
列举 / grep 落盘、`search_image`、`search_meme`、`artifact_get` 对主人格
`visible_when=visible_to_capability_only`；主人格深读只留 `read_handle`。

### `attach_article` — 往公共枢纽挂新文

```python
@ai_tools(category="buildin", capability_domain="回想")
async def attach_article(
    ctx: RunContext[ToolContext],
    node_query: str,  # 已有正式名则复用；没有且过门则新建

    title: str,
    content: str,
    slot: str = "补充",
) -> str
```

只允许写公共枢纽。插件/手动篇 `writable=false`，硬拒。
`node_query` **先查已有**枢纽（别名正式名 / title）；零命中且过公共名词门
（可索引、无歧义、无句读、长度上限）才新建 `world:agent:…`。
同标题且 `source=agent` + `kbdoc:` 的可写篇才覆盖；网页落盘（`to_`）即使 `writable=true`
也必须换标题新建。SQL 分片会打标签 `hub:{正式名}`，以便 `rebuild_cognition_mount`
后扫 agent 源时回挂——**启动扫描**仍不会从 agent 文新建枢纽。
账号进度、谁持有、谁的状态是环境事实，禁止写成公共百科。
`slot` 只表示信息结构：概要 / 细则 / 资料 / 补充。

### `web_search_tool` — Web 搜索

```python
@ai_tools(category="buildin", timeout=100.0)
async def web_search_tool(
    ctx: RunContext[ToolContext],
    query: str,                       # 搜索关键词
    limit: Optional[int] = None,      # 最大结果数；None 时用 web_search_default_limit
) -> str
```

> **注意**：统一走 `web_search()`，默认主用 **AnySearch**（Key 可选，匿名有额度），另支持 Firecrawl（官方 SDK，Key 可选）/ Tavily / Jina（`s.jina.ai`，需 Key）/ Exa / MCP。
> 调度字段：`websearch_provider` / `websearch_lb_strategy`（默认 `error_switch`）/ `websearch_fallback_order`。
> **异常或空结果**会在多源策略下切换下一源。工具外层 `timeout=100` 覆盖串行 failover。
> MCP 时配置 `mcp_tools_config.websearch_mcp_tool_id`。详见 [§11.3](./11-mcp-image-search-and-meme.md)。
>
> **时效标记（2026-08）**：返回体带 `[source=web|staleness_risk=high]`。loop 账本据此判断
> 「本轮只有滞后 web」→ 注入 `WEB_ONLY_STALENESS_CAVEAT`（禁止把网页数字当「此刻」实时读数）。
> 结构化工具应返回 `[as_of=…]` 挡住误报。
>
> **落盘回想（2026-08-16）**：返回体带 `query:` 行。FileOS 短标题用 query（不是
> `<search_results>`）；整页结果用规则摘要进落盘 `summary`、`tool_output` 节点和枢纽挂件。
> 下次 `search_cognition` 用原 query 可命中 FileOS / 路径卡。SERP 全文不晋升百科。
> query 过公共名词门才弱挂 `world:web:…`；长句/句读只留落盘摘要，不建枢纽。

### `web_fetch_tool` — 网页抓取

```python
@ai_tools(category="buildin", visible_when=context_has_url, timeout=100.0)
async def web_fetch_tool(
    ctx: RunContext[ToolContext],
    url: str,            # 要抓取的网页 URL
) -> str
```

> 默认 **Jina Reader**（`r.jina.ai`，Key 可选）+ 备用 **local** 本机直连；
> 字段 `webfetch_provider` / `webfetch_lb_strategy` / `webfetch_fallback_order` 与搜索同构。
> 空正文或抛错会触发换源；外层 `timeout=100`。详见 [§11.3b](./11-mcp-image-search-and-meme.md)。
> 成功返回同样前缀 `[source=web|staleness_risk=high]\n…`（时效契约同源）。

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

主人格隐藏（`visible_to_capability_only`）；回想走 `search_cognition(kinds=image)`。

```python
@ai_tools(category="common", visible_when=visible_to_capability_only)
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
@ai_tools(category="common", capability_domain="用户档案", check_func=_set_favor_master_only)
async def set_user_favorability(
    ctx: RunContext[ToolContext],
    value: int,                     # 好感度绝对值（按 favor_floor/ceil 钳制）
    user_id: Optional[str] = None,
) -> str
```

> **仅主人可用**（2026-07-08）：绝对值设定是管理动作，`check_func` 校验发起者为 master，
> 普通用户调用返回拒绝文案。

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

主人格隐藏（`visible_to_capability_only`）；回想走 `search_cognition(kinds=meme)`。

```python
@ai_tools(category="common", visible_when=visible_to_capability_only)
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

## 7.4 Media 工具（`category="media"`）—— 资料出图（归 `render_agent`）

同属 `capability_domain="资料出图"`。主路径：

```text
create_subagent(agent_profile="render_agent", task=事实包)
```

交互主人格经 **exclusive 剥离**后不应直调 `render_*`。

### `render_html_to_image` — 自由 HTML 出图（主渲染工具）

```python
@ai_tools(category="media", capability_domain="资料出图")
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 800,
) -> str | bytes
```

- 默认自由 HTML；原生 `<table>` → `table_rewrite` flex 网格；自动嵌 `https`/`icon:`/`img_`/`res_`。
- 约束见工具 docstring、[`TAKUMI_HTML_GUIDE.md`](../../../../docs/TAKUMI_HTML_GUIDE.md)。

### `render_chart_spec` — 声明式图表 → 内联 SVG（2026-08，2026-08-16 编码补强）

```python
@ai_tools(category="media", capability_domain="资料出图")
async def render_chart_spec(
    ctx: RunContext[ToolContext],
    type: str,                          # line / bar / hbar / pie
    data: List[Dict[str, Any]] | None = None,   # 单系列 [{label, value}, ...]
    series: List[Dict[str, Any]] | None = None, # 多系列 [{name, data, color?}, ...]
    width: int = 640,
    height: int = 360,
    color: str = "",
    dark: bool = True,
    signed: bool = False,               # True：柱色按正负，系列靠分组+图例
    title: str = "",
    legend: bool = True,
) -> str
```

渲染引擎**无 JS**（pytakumi），echarts/canvas 不可用。本工具返回可直接嵌进 HTML 的
`<svg>…</svg>` 片段，再交给 `render_html_to_image`。`render_agent` 白名单已含本工具；
≥3 数值点应优先图表而非纯文字表。

编码硬规则（防信息误解）：

- 多实体 × 多指标：**必须** `series`（每实体一个 `name`）+ 图例。禁止把身份拍扁进单柱 label。
- `signed` 只表达正负；系列身份不用升/降色。有负值走零轴。
- 缺测点断线，禁止补 0。一个字段一个值，禁止把分歧来源画成未标注的两根柱。
- 类目标签保留约 18 字。身份色避开升/降红绿。

### `render_card` / `render_markdown_to_image`

固定 JSON 卡片 / Markdown 出图；同属 media，通常由 `render_agent` 选用。

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
| `respond_approval` | **统一审批转达工具**（`buildin_tools/approval_tools.py`）：转达用户/主人对任何待审批请求（Kanban 子任务 / 命令执行 / 工具授权等）的同意 / 拒绝，见 [§7.10](#710-审批与授权统一审批中心) |
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
`create_subagent` 也支持 `agent_profile` 参数（即时委派单步任务），见下方 [§7.8](#78-能力代理agentnode-task-mode-节点)。

---

## 7.8 能力代理（AgentNode task-mode 节点）

能力代理是**无人格**的专职执行节点，把「执行」从「人格表达」剥离：主人格只做
识别派发 / 查进度 / 转译汇报，执行交给专职节点（不拒绝、不漂移）。框架内
Persona 与能力代理**同构为一个 `AgentNode` 定义**（`gsuid_core.ai_core.agent_node`）：
同一张 schema、同一个统一注册表；persona 目录被投影为 `source="persona"` 的只读
节点，能力节点是 `builtin / plugin / user` 三态。运行模式不是节点属性——同一节点
可被 session-mode（作交互入口）或 task-mode（被 Kanban 派活）实例化。

多步任务统一由 Kanban 任务树承载：主人格先调 `evaluate_agent_mesh_capability`
评估节点覆盖，再调 `register_kanban_task` 创建根 + 子任务；每个子任务由
`agent_profile`（即 `node_id`）指定的节点推进，结果经 `_persona_relay` 用人格
口吻回告。

框架内置 **7** 个通用节点：`research_agent` / **`render_agent`** / `code_agent` /
`internal_reporter` / `memory_curator` / `scheduler_assistant` / `plugin_developer_agent`。
`capability_evaluator` 仅服务评估，插件勿覆盖。业务节点（如 `stock_agent` /
`stock_report_agent`）由插件注册。

**`render_agent`**：`tool_packs=[]` + 渲染白名单（含 **`render_chart_spec`**）；
runner **禁止** task 向量回填（防 web）。主人格多项数据出图委派它，勿自写 HTML。

### 7.8.1 插件创建并注册业务节点

插件通常在自身启动模块或插件入口导入时注册。注册表是进程内存数据，后写覆盖
前写：插件可注册新 `node_id`，也可用同名覆盖内置节点；WebConsole 用户节点启动
加载后也可覆盖同名内置 / 插件节点。

```python
# plugins/SayuStock/startup.py
from gsuid_core.ai_core.agent_node import (
    TASK_BASICS_PACK,
    AgentNode,
    register_agent_node,
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
   "从哪个工具的哪段数据得到的结论"，禁止只凭 web_search 的新闻标题做决定。
4. 在 Kanban 子任务中完成执行后，用 artifact_put 把主要产出登记成 res 句柄。
5. 高风险动作（实盘下单 / 修改持仓）一律不自己执行，在交付摘要里显式列出
   "需要主人决策的动作"，让主人格转告主人定夺。

【交付格式】
① 决定 / 推荐（简洁可执行）；
② 依据：逐条列理由 + 数据来源（哪个工具 / 字段 / 数值）；
③ 风险提示。
"""


def register_finance_agent() -> None:
    register_agent_node(AgentNode(
        node_id="finance_agent",
        display_name="操盘助手",
        when_to_use="需要查行情、做仓位决策、每日复盘的金融任务",
        prompt=FINANCE_PROMPT,
        match_keywords=["炒股", "操盘", "股票", "金融", "行情", "选股"],
        tool_packs=[TASK_BASICS_PACK],
        tool_names=[
            "send_stock_info",
            "send_my_stock",
            "send_my_stock_img",
            "send_stock_PB_info",
            "search_stock",
            "get_vix_index",
            "send_cloudmap_img",
        ],
    ))


register_finance_agent()
```

### 7.8.2 字段含义与写法约束

| 字段 | 插件应该怎么填 |
|-----|----------------|
| `node_id` | 稳定唯一句柄，如 `finance_agent`；主人格 / Kanban 子任务会保存这个值 |
| `display_name` | 给用户和 WebConsole 看的名称 |
| `prompt` | 纯职能 Plan-and-Solve 提示词；禁止写人格口吻、好感度、角色扮演。**不要**手写"交付边界"段——task-mode 实例化时框架自动叠加（`compose_task_prompt`），特殊边界用 `boundary_override` 覆写 |
| `prompt_style` | 能力节点保持默认 `"plain"`；`"roleplay"` 是 persona 投影节点专用 |
| `when_to_use` | 一句话说明何时派给该节点，供评估代理和人工管理理解 |
| `match_keywords` | 自然语言 hint 命中词，如主人格传 `agent_profile="操盘"` 时可解析到本节点 |
| `tool_packs` | 多数挂 `task_basics`；纯渲染可 `[]`（避免 web） |
| `tool_names` | 业务工具；日期工具注册名多为 `_get_current_date` |
| `tool_query` | 可选；runner 默认再按 task 回填（`render_agent` 已跳过回填） |
| `boundary_override` | 空=默认只向主人格交付；渲染节点可放宽「允许 render 工具发图」 |

> **预算不在节点上**：单次执行的 `max_iterations` / `max_tokens` 统一走 AI 配置的
> `task_max_iterations` / `task_max_tokens`（全局任务档）。Token 消耗经预算 scope
> 自动上溯到来源会话记账，受统一预算规则约束。
>
> **旧 API 兼容**：`register_capability_agent(CapabilityAgentProfile(...))`
> （`profile_id` / `system_prompt` / `max_*` 旧字段名）仍可用——自动转换为
> AgentNode 注册并打废弃 warning，`max_*` 被忽略；**将在下个大版本移除**，
> 新代码请直接用上例的 `register_agent_node(AgentNode(...))`。

### 7.8.3 与 Kanban / `create_subagent` 的关系

- 复合多步任务：主人格按决策树先调 `evaluate_agent_mesh_capability`，覆盖后调
  `register_kanban_task`。子任务里的 `agent_profile` 必须是已注册节点，调度器运行时
  才解析节点，因此插件晚于 `init_planning` 注册也可生效。
- 即时单步委派：`create_subagent` 仍支持 `agent_profile` 参数，适合马上执行的一次性
  专项任务；复杂依赖、并行、多产物任务应交给 Kanban。
- 专业域诚实底线：如果插件未注册金融 / 医疗 / 法律等专业节点和工具，评估代理应返回
  `covered=false`，主人格不得强行创建任务树；`research_agent` 也会避免只靠通用搜索给
  高风险专业建议。

| API（`gsuid_core.ai_core.agent_node`） | 用途 |
|-----|------|
| `register_agent_node(node)` | 注册一个节点；同 `node_id` 后写覆盖前写 |
| `unregister_agent_node(node_id)` | 从注册表移除一个节点；返回是否真的删了一项 |
| `AgentNode` | 统一节点数据类 |
| `resolve_node(hint, default)` | 自然语言 hint → `node_id`（原 `resolve_profile` 语义） |
| `get_node(node_id)` / `list_nodes(include_persona=False)` | 查询注册表（含 persona 投影回落） |
| `register_tool_pack(name, tool_names)` | 注册一个可被 `tool_packs` 挂载的静态工具能力族 |
| `run_capability_agent(node_id, task, ev, bot, ...)` | task-mode 实例化并运行一个节点（`gsuid_core.ai_core.capability_agents`）；插件通常不直接调用，Kanban 调度器会调用 |

> ⚠️ **不要直接访问注册表内部字典**——请使用上表公开 API，避免破坏 WebConsole
> 来源标记和用户节点覆盖流程。

---

## 7.9 `self_model` 演化层（自我认知 4 字段）

实现：`gsuid_core/ai_core/self_cognition.py`。存储：`state_store` 表，
scope = `self:{bot_id}`，state_key = `self_model`，value 为 4 字段字典。

**注入位置（2026-07 O-3 起，两段分离，勿按旧描述改代码）**：
- self_model 自述块（4 字段，慢变、bot/scope 级）：`build_self_cognition_context(bot_id,
  scope_key, include_relationship=False)` → `ai_router` 建 session 时固化进 **system_prompt
  稳定前缀**（跨轮命中 provider 缓存，活跃会话按 TTL 原地刷新，见 `gscore-development` §6.7.1）。
- 当前对话者关系行（per-user）：`build_relationship_context(user_id, favorability)` →
  `handle_ai` **每轮注入用户消息侧**（群共享 session，关系随对话者变、不能冻进共享前缀）。

签名：`build_self_cognition_context` 的 `user_id` 现为可选（`include_relationship=False`
时不需要），`include_relationship` 默认 True 保持旧调用兼容——旧插件若直接调它取"含关系
的整段"仍可用，但生产链路已改走上面两段分离的方式。

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


---

## 7.10 审批与授权（统一审批中心）

框架内所有审批（命令执行 / Kanban 子任务与插件安装 / 工具授权 / Agent 主动请求）
统一由 `gsuid_core.ai_core.approval` 承载：一张 `AIApprovalRequest` 表 +
`submit` / `resolve` 两个动词 + category 领域回调。三个裁决入口——对话工具
`respond_approval`、webconsole `/api/ai/approvals/*`、Kanban 看板兼容端点——
全部落到同一模块。

三种审批 =（interaction × audience）三个合法组合：

| 组合 | LLM 工具 | 语义 |
|------|---------|------|
| question × user | `ask_user(question, options, timeout_seconds, default_choice)` | 澄清提问（选项按钮 + 超时默认），无权限语义 |
| approval × user | `request_user_approval(summary)` | 花**当前用户**自己的资源 / 积分前请求授权；可被「完全访问」豁免（照常留审计记录） |
| approval × master | `request_master_approval(summary)` | 敏感权限请求**主人**；永不可豁免 |

三个工具 `capability_domain="审批交互"`——任何节点可经
`tool_packs=["审批交互"]` 或 `tool_names` 挂载。转达 / 列表工具
`respond_approval` / `list_pending_approvals` 为 buildin 保底（仅有待审批时对
模型可见），含"用户/主人亲口表态"证据闸门，Agent 无法替人拍板。

### 7.10.1 工具策略门（推荐的接入方式）

会产生消费 / 敏感副作用的插件工具，**声明一个参数即接入强制审批**：

```python
@ai_tools(category="common", approval="user")   # 或 approval="master"
async def generate_video(ctx: RunContext[ToolContext], prompt: str) -> str:
    """按提示词生成视频（消耗积分，需用户授权）"""
    ...
```

调用时无有效放行 → 框架自动提交审批并拦截（返回"已提交 #xx，请转告并等回复"）；
用户/主人经 `respond_approval` 批准后发放一次性放行 grant（10 分钟有效），Agent
重新调用即执行。策略门在 `check_func` 之后执行，且不依赖 LLM 自觉。

### 7.10.2 Python API（插件侧）

| API（`gsuid_core.ai_core.approval`） | 用途 |
|-----|------|
| `set_full_access(user_id, enabled)` / `is_full_access(user_id)` | 维护「完全访问」豁免（如宿主前端的授权配置开关）；只作用于 user 级 |
| `submit(category, title, ev=..., audience=..., ref_key=..., payload=...)` | 提交一条审批 / 交互请求（返回落库行，含 `short_id`） |
| `resolve(request_ref, approved, resolver_user_id, note, via)` | 裁决（含定位 + 裁决权校验 + 领域回调） |
| `register_approval_category(name, on_resolve, ttl_seconds)` | 注册自定义审批领域：`on_resolve(row, approved, note) -> str` 承担"批准之后干什么" |
| `has_pending(user_id)` | 内存快判是否可能有待审批（做 `visible_when` 谓词用） |

> 内置 category：`command_exec`（执行 argv 快照）/ `kanban_subtask`（子任务回
> pending + kick，插件安装审批同属此类）/ `tool_call`（策略门 grant）/
> `agent_request`（Agent 主动请求）。插件自定义领域请避开这些名字。
