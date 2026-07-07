# 七、工具注册表与 Agent 装配

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[六、AI Session 路由与 Persona](./06-ai-session-and-persona.md) · **下一章**：[八、主动发言与任务编排](./08-heartbeat-scheduled-planning.md)

本章讲 AI 工具是怎么注册的、主 Agent 每轮怎么决定带哪些工具、子 Agent 和主 Agent 工具集的
差异，以及 2026-06 工具检索升级（Reranker 精排 + `find_tools` 渐进暴露 + `visible_when`
条件隐藏）。这是 AI 链路里最容易"越改越乱"的地方，改前务必读完。

## 7.1 工具注册表结构（`register.py`）

```python
# Dict[分类名, Dict[工具名, ToolBase]]
_TOOL_REGISTRY: Dict[str, Dict[str, ToolBase]] = {}
```

`@ai_tools` 装饰器把 async 函数包成 pydantic-ai `Tool` 存进对应 category。

```python
@ai_tools(category="default", check_func=None, visible_when=None, **check_kwargs)
async def my_tool(ctx: RunContext[ToolContext], ...) -> str: ...
```

| 参数 | 说明 |
|------|------|
| `category` | 工具分类，决定加载方式（见 §7.2） |
| `check_func` | 可选权限校验（同步/异步）。**已调用后**拦截执行并回错误文案 |
| `visible_when` | 可选谓词 `(ctx)->bool`。**是否展示**阶段决定模型能否看到该工具（见 §7.6） |
| `**check_kwargs` | 传给 check_func 的额外参数 |

> **智能参数注入**：`@ai_tools` 自动分析函数签名，把 `RunContext[ToolContext]` / `Event` /
> `Bot` 类型参数**自动注入、不暴露给 LLM**，并重写 `__signature__` 保证 PydanticAI schema 兼容。

## 7.2 工具分类（category）与加载方式

| 分类 | 加载方式 | 典型工具 |
|------|----------|----------|
| `self` | **保底**：无条件加载进主 Agent | 好感度增改、`send_message_by_ai`、`create_subagent`、`add_once_task`/`add_interval_task` |
| `buildin` | **保底**：无条件加载进主 Agent | `search_knowledge`、`web_search_tool`、`web_fetch_tool`、`query_user_memory`、`get_self_info`、`state_set`/`state_get` |
| `common` | 向量检索按需 | `search_image`、`send_meme`/`collect_meme`/`search_meme`、定时任务管理类、Kanban 管理类、`state_list`/`state_delete`/`state_append` |
| `media` | 向量检索按需 | `render_html_to_image`、`render_markdown_to_image` |
| `by_trigger` | 向量检索按需 | 插件 `to_ai` 自动注册的触发器工具 |
| `mcp` | 启动注册 + 向量检索按需 | 用户配置的 MCP 服务器工具 |
| `default` | **子 Agent 专属**（`create_subagent` 调） | 文件读写、`execute_file`、`execute_shell_command`、`get_current_date` |
| `meta` | **不被向量检索**，由 gs_agent 按门控显式注入 | `find_tools`（见 §7.5） |

> **保底池完全由 category 决定，无硬编码名单**。`get_main_agent_tools()` 把 `self`+`buildin`
> 两个分类全部无条件加载。插件想让某工具成为主 Agent 保底工具，注册时 `category="buildin"`。
>
> **安全隔离**：`self` 工具仅主 Agent（防子 Agent 直接操作用户数据）；`default` 工具（文件/
> 系统命令）仅子 Agent。改 category 等于改安全边界，谨慎。

## 7.3 主 Agent 三层工具池（`gs_agent.py::_execute_run`）

主 Agent 每轮工具列表 = **保底池 + 语境池 + 查询池**，再叠加状态驱动与会话驻留：

| 层 | 机制 | 作用 |
|---|---|---|
| L1 保底池 | `get_main_agent_tools()`：`self`+`buildin` 无条件加载 | 框架基础能力常驻 |
| L2 状态驱动 | `get_state_driven_family_tools()`：按用户持久实体补能力族 | 跨轮追问定时任务/Kanban/record |
| L3 会话驻留 | `_recent_tool_families`（sticky 3 轮） | 刚用过的族继续常驻数轮 |
| 语境池 | `get_tools_by_context_tags()` | 群画像标签匹配工具（最多 8 个） |
| L4 族展开 | `expand_tools_to_families()` | 召回任一工具即带出整族（"能建就能改/删"） |
| L5 上文增强检索 | `_recent_user_texts` 拼进检索 query | "改成后天吧"借上文召回 |

保底池全保留；语境 + 查询池合并去重后限制附加数量上限（`MAX_EXTRA_TOOLS`）。

```python
def get_main_agent_tools() -> ToolList:
    """仅 self + buildin 分类，无条件加载。by_trigger 等不再无条件全载，避免插件膨胀
    导致 100+ 工具列表浪费 Token 并降低 LLM 选工具准确率。"""
async def search_tools(query, limit=4, category="all", non_category="", rerank=True) -> ToolList: ...
def get_all_tools() -> Dict[str, ToolBase]: ...          # 平铺所有工具
def get_registered_tools() -> Dict[str, Dict[str, ToolBase]]: ...  # 按分类
```

## 7.4 主 / 子 Agent 工具集差异

- **主 Agent**：保底（`self`+`buildin`）+ 语境 + 查询池（`search_tools(non_category=["self","buildin"])`
  检索 `by_trigger`/`common`/`media`/`mcp`）。**不调 `default`**。
- **子 Agent**（`create_subagent`）：`search_tools(non_category="self")`，加载 `buildin`/`common`/
  `default`。**不调 `self`**。有 `max_iterations=3` 硬限制（防"思考→执行→报错→思考"死循环）。

## 7.5 渐进式工具暴露：`find_tools` + `RetrievableToolset`（2026-06-19）

**要解决的问题**：每轮按本轮用户消息静态检索装配工具，**跨轮澄清会漏召回**——
"帮我查天气"→AI 反问"哪个城市"→用户"广州"，这轮只拿"广州"去检索，召不回天气工具。

**做法**：利用 pydantic-ai 1.x 原生支持的**运行时动态工具集**（`AbstractToolset.get_tools(ctx)`
每个 step 重新调用），让模型"调一次检索工具→下一步那些工具就真的可调用"。

三个组成部分：

1. **`ToolContext.dynamic_tool_names: Set[str]`**（`models.py`）——单轮共享集合（`ToolContext`
   每轮新建，作用域天然是"单次 run"，轮末自然丢弃）。
2. **`find_tools` meta-tool**（`buildin_tools/dynamic_tool_discovery.py`，`category="meta"`）——
   模型发现缺工具时调用，内部 `search_tools_by_domain` 检索并把命中工具名写进
   `ctx.deps.dynamic_tool_names`，返回简短清单。**不声明 `capability_domain`**（否则被 L3
   sticky 带进随后闲聊轮，破坏"闲聊轮零开销"）。
3. **`RetrievableToolset(AbstractToolset)`**（`dynamic_toolset.py`）——`get_tools(ctx)` 每个 step
   读 `dynamic_tool_names`，逐名 `find_tool_base` + `prepare_tool_def` 解析成可调用工具；用
   `exclude_names`（本轮静态已装配工具名）去重避免跨 toolset 重名冲突。

**装配门控**（`gs_agent.py`）：

```python
ENABLE_PROGRESSIVE_TOOLS = True
# 意图门：仅在 intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS（即非「闲聊」）注入 find_tools
#         + 挂 RetrievableToolset；闲聊轮完全跳过（零开销）
```

`meta` 已加入 `NON_SEARCHABLE_TOOL_CATEGORIES`，`find_tools` **永不被向量检索召回**，只由
gs_agent 按门控显式注入。

一次 run 的时间线：

```
step1  工具列表 = 静态召回(L1–L5) + find_tools     模型缺"查天气"工具
       ToolCall: find_tools(need="查询某城市实时天气")
       工具体: search_tools_by_domain → dynamic_tool_names |= {get_weather,...}
step2  RetrievableToolset 读集合 → get_weather 本步"出现"并可调用
       ToolCall: get_weather(city="广州")   ← 同一轮内完成，无需用户再补话
```

> `find_tools` 是**安全网而非替代**：静态召回（L1–L5）仍处理常见场景，模型只在"静态召回没
> 给到所需工具"时才调它。它依赖模型主动意识到"我缺工具"，弱模型可能不触发——所以**不能纯靠
> 它**，保底/状态驱动仍在。总开关 `ENABLE_PROGRESSIVE_TOOLS=False` 即完全回退。

## 7.6 条件隐藏：`visible_when`（2026-06-19）

`@ai_tools` 的 `visible_when` 谓词被包成 pydantic-ai 的 `prepare` 函数挂到 `Tool(prepare=...)`。
`prepare` 每个 step 对每个工具求值，返回 `None` 即本步对模型隐藏（schema 都不下发）。判定
抛异常时**默认可见**。

```python
# 通用谓词集中在 buildin_tools/visibility.py，管理员专属工具共用 visible_to_admin
# （execute_shell_command / install_skill 等，2026-07 抽出，勿再各自复制实现）：
# - 后台/能力代理（无 ev）→ True 不隐藏，交 check_func 执行期兜底（避免误伤能力代理）
# - 交互式用户 → 仅管理员（ev.user_pm == 0）可见
```

| 机制 | 阶段 | 作用 |
|------|------|------|
| `check_func` | "已调用"后 | 拦截执行并回错误文案 |
| `visible_when` | "是否展示" | 决定模型能否看到该工具 |

二者纵深互补。

> `visible_to_admin` 只判 `user_pm == 0`。`command_exec/tools.py` 的
> `_cmd_visible_to_master` / `_has_pending_for_master` **刻意不共用**：它们还叠加了命令执行器
> `enable` 开关与 `operator_user_ids` 操作员白名单（后者无 ev 时偏隐藏），语义不同不要合并。

**保底池条件隐藏（2026-06-20）**：`visible_when` 已从附加池下沉到**保底池**里的窄场景常驻
工具——`buildin_tools/visibility.py` 给 `read_image`（`context_has_image`）与 `web_fetch_tool`
（`context_has_url`）各挂一个谓词：工具仍属 `buildin` 保底、需要时立即出现，但图片/URL 无关
轮不下发其 schema，把保底实下发从 14 压到 ~12。两谓词都**连 `ctx.messages` 一起扫**（不只看
`ev` 文本）——`web_fetch` 的 URL 常来自 `web_search_tool` 的**工具结果**，只看 `ev` 会在
"search→fetch"时误隐藏（且 `visible_when` 同样作用于 `find_tools` 动态暴露，隐藏后现拉也救不回）。
谓词一律**偏可见**。详见 [`docs/AI_CORE_TOOL_RETRIEVAL_UPGRADE_20260619.md`](../../../AI_CORE_TOOL_RETRIEVAL_UPGRADE_20260619.md) §11。

> ⚠️ **约束**：`prepare` 每 step 对每个工具求值，`visible_when` 谓词**必须廉价、内存判定**，
> 切忌每步查库/发网络。贵的前置条件走 L2 状态驱动（加载时判一次）。

## 7.7 两段式 domain 检索（`search_tools_by_domain`）

```python
search_tools_by_domain(query, domain_limit=3, per_domain_limit=6, recall=12)
```

先语义召回（含 Reranker 精排）得到种子，再**聚合到 `capability_domain`**，取语义最靠前的至多
`domain_limit` 个能力族**整族**纳入（未声明 domain 的种子按"单工具族"各占一名额）。以**能力族**
为最小装配单位，保证"能建就能改/删"、避免半个族被截断；用 domain 数量（非工具总数）控规模。
`find_tools` 用它作检索后端。

> `search_tools(rerank=True)` 先粗召回 `_RERANK_RECALL_LIMIT=20` 个，过滤后用交叉编码 Reranker
> 按「工具名+描述」精排再裁到 `limit`。Reranker 未启用/异常时降级回"按向量分数取前 limit"，
> 行为与不接 Reranker 完全一致。主装配路径已降 limit（8→4、`MAX_EXTRA_TOOLS` 16→8）。

## 7.8 触发器桥接工具（`by_trigger`）

插件 `@sv.on_command(..., to_ai="...")` 声明非空 `to_ai`（作为 AI 工具 docstring）→ 插件加载时
`_register_trigger_as_ai_tool()` 把触发器包成 AI 工具，注册到 `_TOOL_REGISTRY["by_trigger"]`，
向量检索按需加载。

AI 调用时：

1. **权限检查**（与用户直接触发一致）：`plugins.enabled`/`sv.enabled`、`user_pm <= sv.pm`。
   不足则返回错误文本给 AI（配置改后实时生效）。
2. `MockBot` 代理 `bot.send`：图片→`RM.register()` 返回资源 ID；纯文本→收集为工具返回值；
   `receive_resp` 返回 `None`（AI 不支持交互式等待）。
3. 触发器内可 `ai_return()` 向 AI 返回纯文本中间结果。
4. AI 据工具返回（文本摘要 + 资源 ID）决定是否 `send_message_by_ai(image_id=...)` 把图发给用户。

> 详细 API（给插件作者）见 `gscore-ai-core-api` 的触发器桥接章。本章只描述框架侧机制。

## 7.9 MCP 工具集成（`ai_core/mcp/`）

启动时（`mcp/startup.py`）读 `data/ai_core/mcp_configs/*.json` 中 `enabled=true` 的配置 → 连
MCP 服务器（fastmcp，stdio）→ `list_tools()` → 为每个工具动态建包装函数（解析 input_schema
生成签名、注入 `RunContext[ToolContext]`）→ 注册到 `_TOOL_REGISTRY["mcp"]`（仅当
`register_as_ai_tools=true`）。工具命名 `mcp_{server}_{tool}` 避免冲突。

- **MCP 工具 ID 格式**：`{mcp_id} - {tool_name}`（如 `minimax - web_search`），用
  `parse_mcp_tool_id` / `format_mcp_tool_id` 解析组装。
- **通用调用**：`mcp_tool_caller.call_mcp_tool(mcp_tool_id, arguments)` 不需注册为 AI 工具即可
  调；Web Search / Image Understand 走它。
- **热重载**：`POST /api/ai/mcp/reload` 清掉已注册 MCP 工具、重读配置、重连重注册，无需重启。
- **MCP Server**（`mcp/server.py`）：反向把本框架 `to_ai` 触发器对外暴露为 MCP 服务。

## 7.10 运行时 Skill 系统与统一安装链路（`ai_core/skills/`，2026-07）

Markdown Skill（`SKILL.md`，agentskills.io 约定）与 `@ai_tools` 工具是两套体系（见
`skills/__init__.py` 模块 docstring）。`resource.py` 持 `skills_toolset` 单例与 `skills` 字典；
`operations.py` 是全部管理操作（增删改 + `_rebuild_skills` 热重载）的唯一入口。

**统一安装函数 `operations.install_skill(source_url, skill_name, update)`**——git 克隆 / 下载
压缩包 / 写单文件的三条旧路径已收敛于此，返回 `SkillInstallResult`（TypedDict）：

1. **来源自适应**：`.git`/`git@` 走浅克隆；其余 URL 先 HTTP 下载按**内容魔数**识别
   zip / tar.gz / 单个 SKILL.md（frontmatter 文本）；拿到 HTML 网页再回退试 git clone
   （覆盖 `github.com/x/y` 主页型地址），并识破 dumb-http「克隆出空仓库却 exit 0」的假成功。
2. **先落临时目录再进 data**：找出全部含 `SKILL.md` 的技能根（支持嵌套、一包多技能），
   找不到 `SKILL.md` 直接拒装——这是防「Agent 照第三方 setup 文档把技能装到
   `~/.workbuddy` 等错误路径」的关键闸门。
3. **目录名 = frontmatter `name`**：`pydantic_ai_skills` 以 frontmatter `name` 为 skills 字典键，
   安装目录名必须跟随它，否则 `delete_skill` 的 `SKILLS_PATH/<name>` 定位会失效。
   无 name 时依次退到入参 / 目录名 / URL 推导（含 `?slug=`），技能名一律过路径穿越校验。
4. **安装/更新一体**：同名已存在默认报错，`update=True` 先整体解析命名与冲突再整批拷贝
   （不留半成品），拷贝剔除 `.git`，结束调 `_rebuild_skills()` 即时生效并校验确实被加载。

**两个消费方**：webconsole `POST /api/ai/skills/clone`（`asyncio.to_thread` 包装，见
`webconsole/docs/15-ai-skills.md` §15.4）；AI 工具 `buildin_tools/skill_installer.py::install_skill`
（`category="common"` 向量检索按需召回，`check_pm` + `visible_to_admin` 双层限主人）。

> ⚠️ `_rebuild_skills` 必须**就地** `skills.clear()+update()` ——`skills` dict 被 webconsole 与
> toolset 按引用共享，用 `skills_toolset.reload()` 重绑引用会让旧引用者读到过期数据。


## 7.11 AgentNode 统一与工具能力族（2026-07-07）

Persona 与能力代理画像统一为 **AgentNode**（`ai_core/agent_node/`）：统一注册表
（`register_agent_node` / `get_node` / `resolve_node`）+ persona 目录只读投影
（mtime 自动刷新）。工具装配抹平为**能力族（tool packs）**（`agent_node/tool_packs.py`）：

- `dynamic`：五层自动装配（本章 7.x 描述的整套），persona 默认；能力节点声明后
  `runner` 传 `create_agent(dynamic_tools=True)`，gs_agent 装配并与显式工具合并；
- `task_basics`：原 `runner._ALWAYS_TOOLS`（artifact/state/record/search/web 族）；
- 任意 `capability_domain` 名可整族挂载；插件可 `register_tool_pack` 注册静态族。

`gs_agent.dynamic_tools`（None=旧门 / True=装配并合并 / False=永不）；persona 节点
`tool_names`（config.json 新键）在装配分支被并入保底池。预算不在节点上——统一读
`ai_config` 的 `task_max_iterations` / `task_max_tokens`。task-mode 系统提示词 =
节点身份核 + 交付边界叠加（`compose_task_prompt`，节点可 `boundary_override`）。

另：`@ai_tools(approval="user"|"master")` 是统一审批中心的**工具策略门**——声明后
每次调用强制过 `approval.tool_call_gate`（完全访问豁免仅 user 级 / 一次性放行
grant / 自动提交审批），不依赖 LLM 自觉。详见
[`docs/AGENT_NODE_UNIFICATION_20260707.md`](../../../AGENT_NODE_UNIFICATION_20260707.md)。

## 7.12 主 Agent 输出链路：出戏防火墙重说闭环（2026-07-08）

`_execute_run_once` 的流式循环对每个 TextPart 发送前做 `output_firewall.check_ooc`
预检——命中**不发送**、记入 `_ooc_blocked`；iter 结束后 `_ooc_rewrite_and_send` 用轻量
无工具 Agent（复用"强制总结"模式）带 `build_rewrite_warning` 重写一次，产物经
`send_chat_result(..., ooc_check=False)` **无检放行**，history 中被拦原文同步替换为重写版。
`send_chat_result` 自身保留"命中即兜底替换"，只兜 proactive / 兜底总结等无重说通道的
调用方。语义与不变量（为什么是"提醒一次→重说→放行"而非"命中即封禁"、`ooc_check=False`
的使用边界、词库加词的碰撞检查）统一见 [§12.22](./12-developer-pitfalls.md)。

> 原"工具前摇台词"机制（`_FRAMEWORK_PRE_TOOL_EXPRESSIONS` + persona `pre_tool_expressions`）
> 已整体移除：耗时工具前的告知由 prompt 条款驱动 Agent 自行组织语言，框架不再替 AI 说
> 固定话（历史上两次硬编码人格台词事故的根治，见 §12.22）。
