# AgentNode 同构统一 + 统一审批中心 · 变更说明（2026-07-07）

> 本次重构把 **Persona 与 CapabilityAgentProfile 统一为同构的 AgentNode 定义**，
> 把**工具装配抹平为可挂载的能力族**，把**预算抹平为全局配置 + 来源会话上溯记账**，
> 并把框架内**三处各自为政的审批**收编为**一个独立审批中心模块**。
> 目标：为后续星型 / 树状 / 双人等多人格编排铺平结构——任何节点都可被指派为
> 与人交互的入口（省去 roleplay 与人格转译开销），任何节点也都可被派活执行任务。

---

## 一、AgentNode 统一节点层（新增 `gsuid_core/ai_core/agent_node/`）

### 1.1 模型（`agent_node/models.py`）

一个类、全字段有默认值，Persona 与能力代理只是取值不同的两行：

| 段 | 字段 | 说明 |
|---|---|---|
| identity | `node_id` / `display_name` / `prompt` / `prompt_style` | `prompt` 即原 persona.md 正文或原 `system_prompt`；`prompt_style="roleplay"` 时 session 装配走人格链路，`"plain"` 时用 lite 约束集（`compose_plain_session_prompt`，为画布/各种前端专门类专属入口预留） |
| routing | `when_to_use` / `match_keywords` | 委派 / 编排层消费（`resolve_node` 关键词路由） |
| tools | `tool_packs` / `tool_names` / `tool_query` | 能力族 + 显式白名单 + 检索补充词（见 §二） |
| task | `boundary_override` | task-mode 交付边界覆写（空=框架默认 `DELIVERY_BOUNDARY`） |
| interaction | `ai_mode` / `scope` / `target_groups` / `inspect_interval` / `keywords` | 入口行为；persona 投影节点来自 config.json（只读视图），能力节点保持默认（不作入口） |
| meta | `source` / `version` | `builtin / plugin / user / persona` 四态来源 |

**刻意不在 schema 里的两样东西**：

- **运行模式**（session / task）——由实例化方决定：`ai_router` 创建的是 session-mode
  （多轮 + 记忆 + 巡检），`run_capability_agent` 创建的是 task-mode（一次循环 +
  交付边界叠加）。同一节点两种模式都可实例化。
- **预算**（`max_iterations` / `max_tokens`）——统一走全局配置任务档（见 §三）。

### 1.2 注册表与投影（`agent_node/registry.py` + `persona_proj.py`）

- 统一注册表：`register_agent_node` / `get_node` / `list_nodes` / `resolve_node`
  （原 `resolve_profile` 语义，保注册序，`plugin_developer_agent` 的"插件"兜底
  关键词依赖它排最后）。
- **Persona 磁盘布局零迁移**：`persona/<name>/{persona.md, config.json, 资源}` 与
  `PersonaConfigManager` 写路径全部不动。persona 目录经 `persona_proj` 投影为
  `source="persona"` 的只读节点，按 persona.md / config.json 的 **mtime 自动刷新**
  （与 ai_router 热重载同信号源）。`get_node()` 未命中注册表时自动回落投影。

### 1.3 CapabilityAgentProfile 的去向（破坏性变更 + 兼容层）

- 框架内 6 个内置画像（`capability_agents/profiles.py`）、`capability_evaluator`
  （`evaluator.py`）全部改为 `AgentNode` 定义并注册进统一注册表。
- webconsole 用户自建画像（`persistence.py`）改存 AgentNode v2 形状；**磁盘上的
  v1 旧画像 JSON（`profile_id` / `system_prompt` / `max_*`）读取时自动迁移**，
  下次保存即落新格式——部署者无需任何手动操作。
- **插件兼容层**（`capability_agents/registry.py`）：`CapabilityAgentProfile`
  旧 dataclass + `register_capability_agent` 保留一个大版本——接受旧对象自动转
  AgentNode（`max_iterations` / `max_tokens` 被忽略并打 warning）。仓库内
  SayuStock / RH_ComfyUI 等存量插件**无需改动即可继续工作**。
- 消费方全部切到 agent_node API：`runner` / `evaluator` / `subagent`（transient
  与 Kanban 两条委派路径）/ `kanban_tools`（agent_profile 校验）/
  `gs_agent._matched_delegation_only_profile` / `webconsole/kanban_api`（
  kanban-candidates）/ `webconsole/capability_agents_api`。

### 1.4 交付边界拆层（原 `_DELIVERY_BOUNDARY`）

- 交付边界从 6 个画像 prompt 尾部的手工拼接，改为 task-mode 实例化时由
  `compose_task_prompt(node)` **统一叠加**（`agent_node/models.py::DELIVERY_BOUNDARY`）。
- `plugin_developer_agent` 用 `boundary_override` 保留其裁剪版（放宽"唯一可写目录"
  以允许审批后的 copy_to_plugin_dir）。
- 收益：任何节点（包括插件注册的、prompt 里没写边界的）被派活时自动获得交付边界，
  不依赖作者手抄；将来 converse-mode 只需第三种叠加层。

---

## 二、工具装配抹平为能力族（tool packs）

`agent_node/tool_packs.py`，节点经 `tool_packs` 同构挂载，三类族：

| pack | 内容 | 对应旧机制 |
|---|---|---|
| `dynamic` | 运行时五层自动装配（保底池+状态池+驻留池+语境池+向量检索+find_tools 渐进暴露），gs_agent 逐轮执行 | 原主人格独占的自动装配 |
| `task_basics` | artifact_* / state_* / record_* / search_knowledge / web_* | 原 `runner._ALWAYS_TOOLS` 强注 |
| `<capability_domain>` | 按 `@ai_tools(capability_domain=...)` 整族解析 | 原 L4 族展开的数据 |

- **persona 默认 `tool_packs=["dynamic"]`**（行为与历史逐字节一致）；persona
  `config.json` 新增 `tool_packs` / `tool_names` 两个配置项（GSC 模板 append-only，
  启动自动补默认值）——**persona 从此也能挂显式工具白名单**（并入保底池，
  `gs_agent` 装配层按 `persona_name` 查节点合并）。
- **能力节点默认 `tool_packs=["task_basics"]`**；想要五层自动装配的节点加
  `"dynamic"` 即可（`runner` 传 `create_agent(dynamic_tools=True)`，gs_agent
  装配并与显式工具合并）。
- `gs_agent` 新增 `dynamic_tools: Optional[bool]`：`True`=每轮装配并合并显式
  tools；`False`=永不装配；`None`=沿用旧门（`create_by ∈ _AGENTIC_CREATE_BY`
  且未传 tools）——所有存量调用方行为不变。
- 插件可 `register_tool_pack(name, tool_names)` 注册自己的静态族。

---

## 三、预算抹平：全局任务档 + 来源会话上溯

- 节点**不再携带**预算字段。`ai_config` 新增两项全局配置：
  - `task_max_iterations`（默认 30，选项 12/20/30/40/60）
  - `task_max_tokens`（默认 50000，选项 20000/35000/50000/60000）
  所有 task-mode 节点统一用它们（原画像上散落的 12/18/20/30/40 各种数字废弃；
  插件传入的旧值被忽略）。重型任务（插件开发等）不够用时**全局调大**即可。
- **消耗上溯**复用既有预算体系（`ai_core/budget/`，无重复建设）：
  - 交互链路：ev 直接派生 scope（group_id / user_id / bot_id）；
  - 嵌套子代理：contextvar 自动继承父 run 的 scope；
  - Kanban / 定时任务异步续跑：任务表存有来源会话字段，执行时重建 Event →
    scope 归属回到发起人。
  即"无论哪个 Agent 接到活，Token 都记到来源会话的账本、受同一套预算规则约束"。

---

## 四、统一审批中心（新增 `gsuid_core/ai_core/approval/`）

### 4.1 结构：一张表 + 两个动词 + category 领域回调

- **表** `AIApprovalRequest`（`approval/models.py`，随 AI 建表自动创建）：
  `request_id` / `short_id` / `interaction`(approval|question) /
  `audience`(user|master) / `category` / `ref_key` / 来源会话与操作者 /
  `title` / `payload_json`（提交时冻结的快照）/ `status`(pending / approved /
  rejected / expired / auto_approved) / 裁决人与途径 / 时间戳。
- **中心** `approval/center.py`：`submit()` / `resolve()`（+`resolve_row` /
  `locate` / `list_pending_for_resolver` / `expire_stale` / `prime_pending`）。
  裁决权校验收敛一处：master 级只认主人（webconsole 登录态等同主人）；user 级认
  发起者本人或主人代裁。**「完全访问」豁免只作用于 user 级**（`set_full_access`，
  豁免照常落 `auto_approved` 记录，审计链完整），master 级永不可豁免。
- **category 领域回调**（"批准之后干什么"）：

| category | 注册处 | on_resolve 动作 |
|---|---|---|
| `command_exec` | `command_exec/approval.py` | 复核 policy → 执行入库 argv 快照 → 审计 |
| `kanban_subtask` | `planning/startup.py` | `approve_subtask`（回 pending / 标 failed）+ `kick_root`；**插件安装审批就是这条**（它本来就是 Kanban 子任务审批），plugin_developer 的多步安装状态机 / 断点续作提示原样保留 |
| `tool_call` | 内置 | 批准 → 发放 10 分钟一次性放行 grant，Agent 重新调用即执行 |
| `agent_request` | 内置 | Agent 主动请求（request_user/master_approval）只落结论 |

### 4.2 三个入口 + 一个工具

1. **对话入口（一个工具）**：`respond_approval(approved, request_ref, note)`
   （`buildin_tools/approval_tools.py`）——全框架**唯一**审批转达工具，替代原
   `respond_command_approval` + `respond_subtask_approval` 两个（均已删除）。
   保留并统一了「主人亲口表态」证据闸门（当前用户消息必须有明确同意/拒绝表达，
   防代理替人拍板）。配套 `list_pending_approvals` 列出可裁决项。两者
   `category="buildin"` + `visible_when=有待审批`（无待审批时对模型隐藏）。
2. **webconsole 通用入口**：`GET /api/ai/approvals/list`、
   `POST /api/ai/approvals/{request_id}/resolve`（`webconsole/approvals_api.py`）。
3. **Kanban 看板兼容入口**：`POST /api/ai/kanban/subtasks/{id}/approve` 保留，
   内部转 `resolve_row`（并修复了旧端点不认叶子根任务的问题）；升级前遗留的
   无票据 waiting_approval 走直连兜底。

`AIAgentTask.status="waiting_approval"` 从此是**派生视图**（看板 Blocked 列渲染
用），账本 / 裁决 / 过期 / 待审批可见性全在审批中心。

### 4.3 三种审批 =（interaction × audience）

- `question × user`：`ask_user(question, options, timeout_seconds, default_choice)`
  —— 澄清提问（选项按钮 + 超时默认），走 `bot.receive_resp`，无权限语义；
- `approval × user`：`request_user_approval(summary)` —— 花用户自己的资源 / 积分，
  可被完全访问豁免；
- `approval × master`：`request_master_approval(summary)` —— 敏感权限，永不可豁免。

三个工具 `capability_domain="审批交互"`（审批能力族），任何节点可经
`tool_packs=["审批交互"]` 或 `tool_names` 挂载。

### 4.4 工具策略门（不依赖 LLM 自觉的强制拦截）

`@ai_tools(approval="user"|"master")` 新参数：声明后每次调用在 check_func 之后
强制过 `tool_call_gate`——完全访问（user 级）放行并留审计记录；有未消费 grant
放行；否则自动提交审批并把「已提交 #xx，获批后重新调用」回给模型。花积分 /
敏感副作用的工具（如 RH_ComfyUI 生成类）声明一个参数即接入，幻觉绕不过。

### 4.5 收编后的删除项

- `command_exec/approval.py` 原 289 行独立实现 → 领域适配层（submit + on_resolve）；
  `AICommandApproval` 表模型删除（旧表在既有部署中留作孤表，pending TTL 仅 30
  分钟，升级损失可忽略）。
- `kanban_tools.respond_subtask_approval` / `command_exec.respond_command_approval`
  / `list_pending_commands` 三个工具删除（由统一工具替代）；
  `tool_state_signals` / persona prompts / 各处文档引用同步更新。
- plugin_developer 的 `req-*` 账本重放（`_install_state`）**保留**——它是安装
  阶段机（领域逻辑），其消费的 TaskLog "主人批准" 事件仍由 `approve_subtask`
  写入（现在由审批中心的 kanban 回调触发），链路不变。

---

## 五、逐文件变更清单

**新增**
- `ai_core/agent_node/{__init__,models,registry,tool_packs,persona_proj}.py`
- `ai_core/approval/{__init__,models,center}.py`
- `ai_core/buildin_tools/approval_tools.py`
- `webconsole/approvals_api.py`

**重写 / 大改**
- `ai_core/capability_agents/registry.py`（→ 插件兼容层）
- `ai_core/capability_agents/profiles.py`（6 内置节点 AgentNode 化 + 边界拆出）
- `ai_core/capability_agents/runner.py`（节点化 + packs + 全局任务档预算 + 边界叠加）
- `ai_core/capability_agents/persistence.py`（v2 落盘 + v1 自动迁移）
- `ai_core/capability_agents/{__init__,evaluator}.py`
- `ai_core/command_exec/{approval,tools,models,startup}.py`
- `webconsole/capability_agents_api.py`（AgentNode 字段，破坏性）

**局部修改**
- `ai_core/gs_agent.py`：`dynamic_tools` 开关、装配分支合并显式工具 + persona
  节点白名单、`_matched_delegation_only_profile` 走统一注册表
- `ai_core/register.py`：`@ai_tools(approval=...)` 策略门
- `ai_core/persona/config.py`：`DEFAULT_PERSONA_CONFIG` 追加 `tool_packs` /
  `tool_names`
- `ai_core/configs/ai_config.py`：`task_max_iterations` / `task_max_tokens`
- `ai_core/planning/kanban.py`：`request_subtask_approval` 提交中心票据（幂等）
- `ai_core/planning/{startup,kanban_tools}.py`、`ai_core/tool_state_signals.py`、
  `ai_core/startup.py`（+审批中心 init 步）、`ai_core/buildin_tools/__init__.py`
- `webconsole/{kanban_api,setup_frontend}.py`、`utils/database/startup.py`
  （+`approval.models` 建表）

---

## 六、插件作者需要注意的点

1. **注册 API**：`register_capability_agent(CapabilityAgentProfile(...))` 本版本
   仍可用（自动转换 + DeprecationWarning），**下个大版本移除**。请迁移到：
   ```python
   from gsuid_core.ai_core.agent_node import AgentNode, register_agent_node

   register_agent_node(AgentNode(
       node_id="stock_agent",
       display_name="股票研究分析代理",
       prompt=STOCK_AGENT_PROMPT,          # 原 system_prompt；不要再手拼交付边界
       when_to_use="...",
       match_keywords=[...],
       tool_packs=["task_basics"],          # 原 _ALWAYS_TOOLS 改为显式声明
       tool_names=[...],
   ))
   ```
2. **预算字段失效**：旧 `max_iterations` / `max_tokens` 被忽略。重型节点需要更多
   轮数时，请提示部署者调 AI 配置的 `task_max_iterations` / `task_max_tokens`。
3. **交付边界不要写进 prompt**：task-mode 会自动叠加；特殊边界用
   `boundary_override`。
4. **审批接入**：
   - 花用户资源的工具加 `@ai_tools(approval="user")`，敏感操作加 `approval="master"`；
   - 「完全访问」开关：`from gsuid_core.ai_core.approval import set_full_access`；
   - 自定义审批领域：`register_approval_category(name, on_resolve, ttl_seconds)`。
5. **工具族**：`register_tool_pack("我的族", [...])` 可注册可挂载静态族；
   `capability_domain` 声明本身就是一个族名。
6. **已删除的工具名**：`respond_command_approval` / `respond_subtask_approval` /
   `list_pending_commands`——插件 prompt / 文档里若提及请改为
   `respond_approval` / `list_pending_approvals`。
7. **webconsole API 字段（破坏性）**：`/api/ai/capability-agents/*` 的
   `profile_id`→`node_id`、`system_prompt`→`prompt`，删 `max_*`，增
   `tool_packs` / `prompt_style` / `boundary_override`；
   `/api/ai/kanban/capability-agents/kanban-candidates` 同步改名。

## 七、部署者升级影响（是否无痛）

**是——除 webconsole 前端一处，其余全自动，无需任何手动操作：**

| 项 | 升级行为 |
|---|---|
| Persona 目录 / persona.md / 资源 | **完全不动** |
| Persona config.json | GSC 模板 append-only：首次读取自动补 `tool_packs=["dynamic"]`、`tool_names=[]`，行为与升级前逐字节一致 |
| webconsole 用户自建画像 JSON | 读取时自动 v1→v2 迁移，下次保存落新格式 |
| 数据库 | 仅新增 `aiapprovalrequest` 表（create_all 自动建）；无列迁移、无数据搬迁。旧 `aicommandapproval` 表留作孤表不再读写 |
| 存量插件（SayuStock / RH_ComfyUI 等） | 经兼容层继续工作，仅启动日志多一条废弃 warning；其画像预算值被全局任务档取代 |
| 升级瞬间在途状态 | 命令审批 pending（TTL 30 分钟）作废需重发；升级前已挂起的 Kanban waiting_approval 无中心票据，可经看板兼容端点直连裁决（已兜底） |
| AI 配置 | 新增 `task_max_iterations` / `task_max_tokens` 两项，默认值即可用 |
| **需要人工跟进的唯一一处** | webconsole **前端**若有能力代理编辑页 / 看板审批页，需按 §六.7 的字段名更新（后端已带兼容端点，Kanban 审批按钮不受影响） |
