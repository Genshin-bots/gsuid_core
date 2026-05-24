# 拟人化 Agent · 能力代理与 Kanban 任务树架构

> 本文是框架 AI 代理层的完整参考文档，以代码库现行实现为准。
> 关联 WebAPI 文档见文末 §22。

---

## 1. 总览

### 1.1 设计目标

| 目标 | 说明 |
|------|------|
| 历史安全 | 送入模型 API 的历史永远保持工具调用与工具结果配对自洽 |
| 执行 / 表达分离 | 严肃执行交给无人格能力代理；主人格只负责识别、派发、查询、转译 |
| 任务可持久化 | 多步任务状态、依赖、产出全部落库，跨重启崩溃恢复不依赖进程内会话 |
| 能力可扩展 | 框架内置 5 个通用画像，业务画像由插件注册；WebConsole 支持用户画像管理 |
| 人格稳定 | 系统提示词决策树、动态口吻锚点、self_model 注入都走用户消息侧，不污染 persona 定义和 prompt cache |
| 跨平台可执行 | Windows SelectorEventLoop 下通过线程包装同步 subprocess |

### 1.2 架构：hub-and-spoke（星型）

- **主人格是 hub**：识别任务、选择画像、调 `evaluate_agent_mesh_capability` / `register_kanban_task` / `create_subagent`、查询进度、回答追问、把结果转译为角色口吻。
- **能力代理是 spoke**：无人格、纯职能、Plan-and-Solve，只对任务结果负责。
- **Kanban 任务树**是框架唯一的多步任务承载体：两张持久化表 + Artifact Hub，跨重启崩溃恢复不依赖进程内会话。
- **多代理协作**只通过 Kanban 任务树（依赖边 + Artifact Hub）实现，不引入点对点消息总线。
- **一棵树包完整生命周期**：任意"持久化状态 + 周期更新 + 最终汇总"形态任务（虚拟盘 / 健康打卡 / 学习计划 / 销售追踪 / 项目追踪…）**用一棵树**表达——init 子任务（一次性）+ 周期子任务（带子任务级 `recurring_trigger`）+ final 子任务（带 `not_before`）。整棵树在所有 armed 周期子任务过期前都保持 running。这是 2026-05-25 架构升级的核心：旧版需要拆"三棵独立树"由主人格串联调度的折中已被淘汰。
- **调度模型以事件驱动为核心**：`register_kanban_task` / `respawn_subtask` / `resume` 等写操作触发 `kick_root`，把所有依赖满足的子任务并发派出；某个子任务完成 / 失败时框架自动再 kick，把下游解锁的子任务接力派出。子任务级周期触发由 APScheduler 桥（`recurring._fire_subtask_template`）到点克隆执行实例后送入事件驱动流；子任务级 `not_before` 由 `recurring._fire_not_before` 到点 kick；根任务级周期模板（兼容路径）按 `recurring._fire_template` 克隆整棵实例树后再执行（详见 §5.4 / §16）。

---

## 2. 数据模型

### 2.1 `AIAgentTask`——任务节点表

`gsuid_core/ai_core/planning/models.py` 的 `AIAgentTask` 同时承载根任务和子任务，靠 `node_kind` 字段区分。

| 字段 | 类型 | 含义 |
|------|------|------|
| `id` | str (UUID) | 主键 |
| `ordinal` | int | 按 owner 递增的用户可见短序号 |
| `goal` / `display_name` | str | 任务目标 / 展示名 |
| `status` | str | `pending` / `running` / `paused` / `waiting_approval` / `completed` / `failed` / `cancelled` |
| `scope_key` / `owner_user_id` / `persona_name` | str | 归属与作用域 |
| `agent_profile` | str | 子任务由哪类能力代理推进（根任务为空） |
| `bot_id` / `bot_self_id` / `group_id` / `user_type` / `WS_BOT_ID` / `session_id` | 多 | 重建 Event 所需上下文 |
| `broadcast_targets` | JSON list | 授权播报白名单 |
| `review_notes` | text | 复盘累积区 |
| `interval_seconds` | int | v1 长任务遗留 / 根任务兜底心跳间隔字段；当前 Kanban 执行不使用它做子任务级定时等待，默认 0 |
| `created_at` / `updated_at` / `last_heartbeat_at` | datetime | 时间戳；`last_heartbeat_at` 是僵尸恢复依据 |
| `parent_task_id` / `root_task_id` / `node_kind` | str | 任务树结构（`root` / `subtask`） |
| `dependency_task_ids` | JSON list | 子任务依赖的兄弟子任务 id |
| `failure_reason` | text | 最近失败原因 |
| `respawn_count` | int | 已被重派次数（≥ `DEFAULT_RESPAWN_LIMIT=3` 时强制 `waiting_approval`） |
| `params_override` | JSON dict | 重派 / 审批后修正的参数 |
| `input_artifact_ids` / `output_artifact_id` | JSON list / str | 上游产出引用 / 本节点最终产出 |
| `failure_policy` | str | `notify_persona`（默认）/ `auto_abort` |
| `workspace_policy` | str | `artifact_only`（默认）/ `unrestricted` |
| `recurring_trigger` | str | `"interval:<seconds>"` 或 `"cron:<m> <h> <dom> <mon> <dow>"`；空=一次性。**既支持根任务级**（整棵树克隆，保留用于"任意时刻独立跑一棵新树"的兼容用法）**也支持子任务级**（一棵树内 init/recurring/final 多阶段共存，**新版推荐**） |
| `recurring_until` | datetime | 周期失效时间；空=永远生效 |
| `template_root_id` | str | 根任务级克隆实例的反向指针，指向模板根任务；模板本身为 None |
| `template_subtask_id` | str | **子任务级**克隆实例的反向指针——指向模板子任务；模板子任务自身为 None，每次 fire 框架在同一棵树下新建一个执行实例子任务，本字段反向指向模板，便于 webconsole 把"近 N 次开火实例"挂到模板下展示 |
| `recurring_status` | str | `"armed"` / `"disarmed"` / `""`（非周期）。模板子任务依赖满足时由 executor 自动 arm 挂 APScheduler；arm 失败 / `recurring_until` 到期 / 整树 cancel 时 disarm |
| `fire_count` | int | 模板被开火次数（根级模板 + 子任务级模板共用） |
| `not_before` | datetime | 子任务级"最早可派出时间"；`get_ready_child_tasks` 把 `not_before > now` 的子任务过滤掉。注册时框架自动挂 APScheduler 单次 date job 到点 `kick_root`；周期模板克隆实例时**不复制**该字段 |

### 2.2 `AIAgentTaskLog`——任务事件日志

字段：`id` / `task_id` / `step_id` / `timestamp` / `event_type` / `content`。

`event_type` 取值：`plan_created` / `step_started` / `step_done` / `step_failed` / `decision` / `broadcast` / `review` / `approval` / `workspace_violation`。

崩溃恢复幂等性、webconsole 审计窗口、`workspace_violation` 自动升级 fail（达 `MAX_WORKSPACE_VIOLATIONS = 3` 次）均依赖本表。

### 2.3 `AIAgentArtifact`——任务节点产出登记

- `payload_inline` ≤ 4KB（`INLINE_PAYLOAD_LIMIT`）直接 inline，超过走 `payload_path` 落盘到 **任务的 workspace 内**（`data/ai_core/artifacts/{root_task_id}/{task_id}/workspace/...`）。所有"代理跑出的中间代码 + 真实产物 + 落盘大文本 artifact"都共住同一 `workspace/` 目录——webconsole 一个文件列表就能看到全貌，主人格 `artifact_get` / `send_message_by_ai` 也只需要看一个目录。
- 访问边界：默认禁止跨 `root_task_id` 读取（`artifact_get` 严格校验 plan_ctx 匹配），同一任务树内才能互读。
- `artifact_kind` 取值：`output` / `workspace_file` / `log` / `report` / `patch`。
- TTL 默认 30 天（`DEFAULT_TTL_DAYS`）；过期清理由 `planning/startup` 注册的每日 04:00 APScheduler job 触发，调 `AIAgentArtifact.delete_expired()` 删除 `expires_at < now` 的行 + 落盘文件 + 空 workspace 壳。多个 artifact 行可能共享同一 `payload_path`（如 `workspace_file` 自动登记 + `output` 显式登记同一文件），删除按路径去重；`expires_at IS NULL` 视为"永久保留"，不删。

**三种登记入口**（`workspace.put_artifact` / `artifact_put` LLM 工具）：

| 模式 | 用法 | 写入字段 | mime 处理 |
|------|------|---------|----------|
| **登记真实文件**（PNG / PDF / CSV / 二进制） | `artifact_put(file_path="workspace 内文件名", summary=...)` | **直接登记 workspace 内原文件路径**（不再拷贝副本）；越界文件被拒绝 | 按后缀自动推断（`_EXT_TO_MIME` 速查表，覆盖图片 / 文档 / 数据；未命中退回 `application/octet-stream`） |
| **登记 inline 文本**（≤ 4KB） | `artifact_put(payload="结论文本", summary=...)` | `payload_inline` 字段 | 默认 `text/plain` |
| **登记落盘大文本**（> 4KB） | `artifact_put(payload="...大段 JSON/HTML/Markdown...", mime="application/json"/…)` | 自动落盘到 `workspace/_artifact_<id>.<ext>` | mime 决定扩展名（`json` / `html` / `md` / 其它 `txt`） |

**为什么"登记真实文件"模式不再拷贝**：旧实现把 `artifact_put(file_path=...)` 登记的文件复制到 `{artifact_id}/payload.<ext>` 子目录，导致同一份产物同时存在于 `workspace/` 与 `{artifact_id}/`——webconsole 只能看到 `{artifact_id}/payload.<ext>`（一行 artifact 卡片），看不到 `workspace/` 里的中间代码（实测会话 `e05e495b` 主人投诉点：他想直接在网页控制台检查代理生成的 `weather_legend.py` 源码却找不到）。新实现把中间代码、生成的产物文件、落盘 artifact 全部统一在同一个 `workspace/` 目录，webconsole 一个文件列表就能看到任务全貌，主人格用 artifact 句柄拿到的也是 workspace 内的原文件，删除时按路径去重一次性删干净。

**"登记真实文件"模式仍是必须的**（实测会话 b8cf57ca 教训）：code_agent 用 PIL 生成了真实的 `love_heart.png`，但若没有显式 `artifact_put(file_path=...)`，仅靠命令前后扫描自动登记的 `workspace_file` artifact 只有 `mime=application/octet-stream`——主人格 `send_message_by_ai(image_id=res_xxx)` 时无法识别为图片。`file_path` 模式让代理显式声明"这是一份对外交付的产物 + 它的 mime"，主人格才能透明发出。代理仍要写 `payload='{"file": "x.png", "size": 17842}'` 这种 JSON 元数据冒充文件会被 inline 文本路径拒绝——明确的红线见 §21 #15。

### 2.4 旧库迁移

`gsuid_core/utils/database/startup.py` 的 `exec_list` 含若干幂等 `ALTER TABLE aiagenttask ADD COLUMN ...`，覆盖所有新增字段（含周期触发字段）。另有一条 `UPDATE` 把更早期的旧任务退化为只有根节点的退化树（`root_task_id = id WHERE root_task_id IS NULL`）。

---

## 3. 能力代理（Capability Agents）

### 3.1 模块结构

`gsuid_core/ai_core/capability_agents/`：

| 文件 | 内容 |
|------|------|
| `__init__.py` | 模块导出（注册表、画像持久化 API、`unregister_capability_agent` 等公开入口） |
| `registry.py` | `CapabilityAgentProfile` 数据类、进程内注册表、`resolve_profile` / `get_profile` / `unregister_capability_agent` |
| `profiles.py` | 5 个内置画像（`research_agent` / `code_agent` / `internal_reporter` / `memory_curator` / `scheduler_assistant`）；注册函数 `register_builtin_profiles()` |
| `runner.py` | `run_capability_agent()`：按画像装配工具集，跑无人格 Plan-Solve Agent；导出 `CAPABILITY_AGENT_ERROR_PREFIX` |
| `evaluator.py` | 内部 `capability_evaluator` 画像 + `evaluate_capability()` + 15 分钟内存评估缓存 |
| `persistence.py` | 用户自定义画像 JSON 持久化、source 三态判定、启动时加载用户画像 |

### 3.2 画像 Profile 契约

`CapabilityAgentProfile` 表示一种无人格的专职执行角色，核心字段：

| 字段 | 含义 |
|------|------|
| `profile_id` | 唯一 id，例如 `research_agent` / `code_agent` |
| `display_name` | 展示名 |
| `when_to_use` | 何时使用该画像 |
| `system_prompt` | 纯职能 Plan-and-Solve 提示词，不带角色人格 |
| `match_keywords` | 自然语言 hint 解析关键词 |
| `tool_names` | 显式工具白名单 |
| `tool_query` | 运行时工具向量检索的查询文本 |
| `max_iterations` / `max_tokens` | 能力代理执行预算 |

注册表是进程内存数据，启动时由 `register_builtin_profiles()` 和插件 / 用户画像重建。同名 `profile_id` 后写覆盖前写。

### 3.3 内置画像（5 个通用）

| profile_id | display_name | 定位 | 关键工具（核心） |
|---|---|---|---|
| `research_agent` | 调研助手 | 外部资料收集 / 综合分析 / 有据可查结论 | （空白名单，运行时按 task 文本向量检索装配 + `_ALWAYS_TOOLS`） |
| `code_agent` | 代码助手 | 沙盒内写代码 / 跑脚本 / **用 PIL / matplotlib / 任意第三方库生成真实文件产物** | `list_directory` / `read_file_content` / `write_file_content` / `diff_file_content` / `execute_file` / `execute_shell_command` / `get_current_date` / `state_*` / `record_put` / `record_get` / `record_list` |
| `internal_reporter` | 内部数据报告员 | 只查框架内部库后渲染 Markdown 报告；不查 web，不跑代码 | `query_user_memory` / `query_user_favorability` / `record_get` / `record_list` / `record_summary` / `query_scheduled_task` / `list_scheduled_tasks` / `render_markdown_to_image` / `get_current_date` |
| `memory_curator` | 记忆管家 | 用户偏好 / 承诺 / 反思的轻量记忆维护 | `update_self_note` / `query_user_memory` / `get_current_date` |
| `scheduler_assistant` | 日程助手 | 自然语言时间解析 + AIScheduledTask 增删改查 | `add_once_task` / `add_interval_task` / `list_scheduled_tasks` / `query_scheduled_task` / `modify_scheduled_task` / `cancel_scheduled_task` / `pause_scheduled_task` / `resume_scheduled_task` / `get_current_date` |

每个画像还附带"永远工具" `_ALWAYS_TOOLS`（即便画像忘写也不会丢）：`artifact_put` / `artifact_get` / `artifact_list` + `state_*` + `search_knowledge` + `web_search_tool` / `web_fetch_tool`。

**所有内置画像均已移除 `send_message_by_ai`**：能力代理只对主人格交付结果，绝不直接和主人对话。下行播报统一由 `kanban_executor._persona_relay` 用主人格口吻转译后送达。

**`code_agent` 不持有 HTML / Markdown 渲染工具**（`render_html_to_image` / `render_markdown_to_image` 在 `internal_reporter` 白名单里，也在主人格保底池里）。原因：实测会话 b8cf57ca 里 code_agent 跑完 PIL 脚本拿到真实 `love_heart.png` 之后，又自作主张调 `render_html_to_image` 弄了一张"HTML 模板预览图"作为额外 artifact，转译 agent 不知道哪张才是用户要的，最后发了 HTML 预览图而不是 PIL 真实产物。code_agent 现在只负责"写代码 / 跑代码 / 把真实文件落到 workspace 并 `artifact_put(file_path=...)` 登记"，要不要再叠"渲染层"由主人格决定。

业务数据画像（`stock_agent` / `weather_agent` 等）由插件自行注册，不内置进框架（见 §17）。

### 3.4 内部 `capability_evaluator` 画像

`when_to_use="framework_internal_only"`、`match_keywords=[]`——`resolve_profile` 不会把它选上。由 `evaluator.py` 的 `register_capability_evaluator()` 注册，专门服务 `evaluate_agent_mesh_capability` 工具。

`evaluate_capability(user_goal, owner_user_id, persona_name)` 是一次性无记忆会话，输出必须是合法 JSON，否则一律返回 `covered=false` + `risk_notes`。

**输出解析容错**（`_parse_evaluator_output` + `_extract_first_json_object`）：

`_extract_first_json_object(text)` 是统一的"从任意文本里捞 JSON 对象"兜底：
1. **优先匹配 markdown 代码围栏** `` ```json {...} ``` `` 或 `` ``` {...} ``` `` —— 这是 LLM 最常用的包装方式。
2. **退而求其次**：找文本里第一个 `{`，从该位置截到末尾交给 `json.JSONDecoder.raw_decode` 自己判断对象边界 —— 任何前置文本（`**标题**`、`以下是评估结果：`等）都被跳过。

然后 `_parse_evaluator_output` 用 `json.loads` → 失败时退到 `raw_decode` 取第一个完整对象，覆盖"多 JSON 拼接"与"后置闲话"两类常见格式漂移。

**覆盖的真实失败模式**：
- 模型偶尔在 ``` 围栏内输出 → 围栏匹配优先；
- 模型偶尔在 JSON 前加 markdown 标题（实测会话 e05e495b：`**Capability Evaluation — Virtual Stock Trading Simulation**\n\n` 然后才是 ```json 围栏）→ 围栏匹配跳过前导文字命中；旧实现只 strip 首位 ``` 看不到结构，直接误判为坏输出导致主人格走旁路自己干；
- 模型偶尔把同一份 JSON 输出两次（实测会话 17ed4f85，字段顺序换 / 内容略变） → `raw_decode` 取第一份；
- 模型偶尔加后置闲话（`{...} \n 希望对你有帮助`） → `raw_decode` 截到 `}` 即停。

**自动重试**：`evaluate_capability` 第一次解析失败时**自动重试一次**（`_EVAL_MAX_ATTEMPTS = 2`），重试时在 system / user 两侧追加更严格的"裸 JSON"口令（首字符 `{`、末字符 `}`、前后无任何字符）。只有重试也失败才返回 `covered=false`。**严禁**主人格在评估失败时走旁路自己拼任务——见 §7.3 / §21 #22。

evaluator prompt 顶部还硬约束「**严格只输出一个 JSON 对象**，结束 `}` 后立即停止，绝不允许追加第二份」，与解析容错 + 重试形成三层保险。

**评估缓存与匹配策略**：评估结果按 owner 分组存入 `_RECENT_EVALUATIONS_BY_OWNER`，每个 owner 最多保留最近 `_RECENT_EVALUATIONS_PER_OWNER=4` 份未过期（TTL 15 分钟）结果。`register_kanban_task` 查 `get_recent_evaluation(owner, goal)` 时按"先精确后模糊"两段匹配：

1. **精确**：`evaluate user_goal[:200].strip() == register goal[:200].strip()` 直接命中。
2. **模糊**：把两段文本都过 `_tokenize_for_overlap`（中文双字 bigram + 英文/数字小写 + stopword 过滤），算**重叠系数** `|A ∩ B| / min(|A|, |B|)`；≥ `_FUZZY_MIN_OVERLAP = 0.45` 时命中。选 overlap coefficient 而非 Jaccard 是因为：register goal 通常是 evaluate user_goal 的"精炼版"或反过来（长短差异大），Jaccard 会被长度差异严重惩罚（同一组任务也只 0.15–0.25），overlap coefficient 不受长度影响。
3. 两段都没命中 → 返回 None，`register_kanban_task` 拒绝并把最近 3 次评估的标题列出来作为主人格回头线索，**所有拒绝文案统一用 `_FUZZY_MIN_OVERLAP` 常量而非硬编码数字**——避免文档与实现漂移。

> 此前实现按 `(owner, goal前200字)` 字符串完全相等做缓存键，导致 evaluate 与 register 之间只要 goal 句首词序略改就 100% 失配。当前的模糊匹配设计专门解决这个落地缺口。

evaluator 对**「持久化状态 + 周期更新 + 最终汇总」类**任务（虚拟盘 / 健康打卡 / 学习计划 / 销售追踪 / 项目追踪 …——任何"建一份状态 → 周期更新 → 最后总结"形态）一律判 `covered=true`，对**真实外部副作用**任务（实盘下单 / 真实转账 / 修改医疗病历 / 法律文书提交 …）在框架未挂载专业外接工具时判 `covered=false`。周期任务 + ≥ 2 子任务时，`risk_notes` 顶部首条必须给出推荐的 `recurring_trigger` 字符串，cron 表达式**从用户描述的时段反推**（"工作日上午" / "每天早晚" / "每周一" / 行业特定时段如证券开盘 / 医院门诊 / 餐饮翻台等），不要套模板。

### 3.5 `run_capability_agent()` 行为

`runner.py` 使用 `create_by="CapabilityAgent"` 创建无人格 Agent，显式传入画像解析出的工具集，绕过主人格 `_execute_run` 的自动工具装配。

关键行为：
- `return_mode="return"`：能力代理文本不会直接发给用户。
- session id 带画像和任务 suffix，便于调试和日志隔离。
- 失败时返回 `f"{CAPABILITY_AGENT_ERROR_PREFIX}: {e}"`；调用方（`kanban_executor`）用 `raw_result.startswith(CAPABILITY_AGENT_ERROR_PREFIX)` 识别。

**调用路径已统一为 Kanban**：`run_capability_agent` 只被 `kanban_executor._run_one_task_node` 一条路径调用。主人格 `create_subagent(agent_profile=...)` 不再直接调本函数走 ad-hoc workspace——而是自动转为创建 Kanban **叶子根任务**并由调度器派活（详见 §3.5.1）。ad-hoc workspace 的 contextmanager `_ensure_adhoc_workspace` 仍保留作"最后一道兜底"——极少情况下（如启动早期 planning 未就绪时）插件代码 / 单元测试直接调 `run_capability_agent` 也不会污染项目根：

| 入口判定 | 行为 |
|---------|------|
| 已绑 `PlanRunContext.artifact_workspace`（Kanban 派活路径） | 透传现有 ctx，零开销 |
| 无 `PlanRunContext`（兜底路径，正常运行时不应发生） | 建临时 `PlanRunContext`：`root_task_id = "adhoc_<sess_hash>"`、`task_id = "adhoc_<profile>_<ts>_<rand>"`、`workspace = data/ai_core/artifacts/adhoc_<sess>/<task>/workspace/`，并 `bind_plan_context`；退出时 reset |

ad-hoc artifact 仍受 §4.3 的 30 天 TTL 清理。但产物归属 / 看板可见 / `artifact_get_recent` 追溯**只有 Kanban 路径才保证**——见 §3.5.1。

### 3.5.1 `create_subagent(agent_profile=...)` 自动转 Kanban 叶子根任务

主人格调 `buildin_tools.subagent.create_subagent(task=..., agent_profile=...)` 时，**框架内部自动**走"叶子根"快路径：

1. 用 `kanban.create_kanban_tree(root_agent_profile=pid, subtasks=[])` 创建一棵**只有根任务**的 Kanban 树（goal 直接用任务原文）。**根任务自身带 `agent_profile`**——它就是执行节点本身，**不再**附挂一个冗余的"1 子任务"包装层。
2. `kick_root(root.id)` 立刻派活——`execute_ready_tasks` 检测到 `is_leaf_root(root, children=0)` 后直接把根任务作为单一可执行节点送进 `_run_one_task_node(root, root)`：绑 workspace（`artifacts/<root.id>/<root.id>/workspace/`）、执行画像、产物登记 artifact、`_persona_relay` 用主人格口吻播报。
3. 主人格侧的 `create_subagent` 调用同步轮询**根任务**终态（`_KANBAN_INLINE_WAIT_TIMEOUT_SEC = 180s`、`_KANBAN_INLINE_POLL_INTERVAL_SEC = 0.6s`）。
4. 终态到达 → 抓本任务 artifact 列表 + inline 文本结论摘要，拼成回执串返回给主人格（含主要产物 `res_xxx` 句柄、状态标签、失败原因等）。
5. 超时未到终态 → 返回"任务仍在跑，到 webconsole 看进度"且**不强制中止**——任务继续在 Kanban 调度器里跑，主人格事后用 `artifact_get_recent` 即可追溯产物（已绑定本任务树）。

**为什么叶子根 vs 旧"根 + 1 子任务"双节点**：实测会话 e05e495b 主人投诉点——简单的"画一张图"派给 code_agent 时，看板上出现一棵任务#1 的根任务又生出一个子任务，"这种简单任务只需要一个根任务即可，完全不需要创建子任务"。叶子根模式把单步专业委派抽象成"一张任务卡"，避免冗余结构；多步任务（路径 A，显式 `register_kanban_task`）仍保持"根 + N 子任务"的标准结构。

**叶子根的状态机**：与子任务完全等价 —— `pending → running → completed/failed/waiting_approval`。
- `_run_one_task_node` 对叶子根做的事情和对子任务一样（条件 SQL 抢锁 → 绑 workspace → 跑代理 → 落终态 + relay）。
- `refresh_root_status` 检测到 `is_leaf_root` 时**跳过汇总**，让 `_run_one_task_node` 直接写终态。
- `recover_zombie_subtasks` 把"子任务 + 叶子根"一起从 running 复活到 pending（崩溃恢复）。
- `respond_subtask_approval` / `respawn_subtask` 都把叶子根视作子任务句柄（`_resolve_subtask` 在 `children=0` 但 `is_leaf_root(root, 0)` 时返回根任务本身）。
- 看板列：叶子根按 root 渲染一张卡，`subtask_count=0`、`subtask_done_count=0`、`kanban_column` 按其 status 算（pending → progress / running → progress / completed → Done / 等）。

**两条路径产物归属一致**：路径 A（显式 `register_kanban_task` + N 子任务）与路径 B（`create_subagent(agent_profile=...)` 叶子根）的产物都挂到根任务 root_task_id 下、看板可见、`artifact_get_recent` 可追溯。差别仅在结构（多步 vs 单步）。

`create_subagent` 不带 `agent_profile` 的调用形式（通用 Plan-Solve Agent）保留——它没有画像 / 工具 / workspace 绑定，仅供主人格本轮内部小调度使用，与"产物交付"语义无关。

### 3.6 人格转译 `_persona_relay`

能力代理输出是中性的执行报告，不直接发给主人。`kanban_executor._persona_relay(task, raw_result)` 的流程：

1. 读取 `task.persona_name`，用 `build_persona_prompt(persona_name)` 创建一次性转译 Agent（`session_id=None` —— 不写 session 日志，避免每次子任务完成都产生 ~67KB 噪声）。
2. **拉本子任务下登记的 artifact 并按优先级排序**：用 `AIAgentArtifact.list_for_task(task.id)` 把本任务的 artifact 全部列出，按 `图片落盘文件 > 其它落盘文件 > 纯 inline 文本` 三档排序，同档按时间倒序。第一个"落盘文件"被标为"⭐ 推荐发送"句柄并单独在 prompt 末尾高亮。原因：实测会话里 code_agent 同时 `artifact_put` 留下 PIL 产出 + HTML 预览图 + 元数据 JSON，转译 agent 不排序就会随机挑一个发，结果发的是 HTML 模板预览图而非 PIL 真实产物。
3. 给 relay agent 注入最小工具池 `[send_message_by_ai]`——该工具已统一支持 `image_id="res_xxx"`（Kanban artifact，自动读 payload + 转 RM）/ `img_xxx`（RM）/ `http(s)://` / `base64://` 四种来源，因此**单个工具就能覆盖文本 + 图片 + 文件**的发送。
4. 要求 relay agent 用角色口吻简短转告主人，**有⭐推荐句柄就只发推荐句柄、不要把 inline 文本 artifact 当图片发**（inline 文本无 `payload_path`，发送会失败）。
5. 明确提示"不要把自己当作做出该决定的人"。
6. 转译失败时 debug 记录并原样播报。

执行由无人格代理承担，不拒绝、不漂移；表达由人格承担，只是寒暄级转述并按需触发"发图 / 发文件"等下行通道，不需要人格承担专业推理。

**`send_message_by_ai` 的 `image_id` 双命名空间解析**：详见 `buildin_tools/message_sender.py`。
- `image_id` 以 `res_` 开头 → 先查 `AIAgentArtifact`：找到则读 `payload_path` 字节，`RM.register()` 自动转一次 RM 资源后发送；artifact 找不到再回退尝试 `RM.get(res_id)`（容错用户上传时被框架登记到 RM 但前缀写成 `res_` 的边界场景）。
- `image_id` 以 `img_` 开头 → 直接 `RM.get`。
- `image_id` 以 `http(s)://` / `base64://` 开头 → 直接 `MessageSegment.image`。
- inline 文本 artifact（无 `payload_path` 只有 `payload_inline`）会被工具拒绝并提示"用 `artifact_get` 取原文后用 `text` 参数发"。

这一段是"主人格透明发送能力代理产物"的实现基础——主人格不需要知道 RM / artifact 是两套存储，只要拿到 res 句柄直接发就行。

### 3.7 画像 source 三态权限

| source | 说明 | 权限 |
|--------|------|------|
| `builtin` | 框架内置 5 个画像 + 内部 `capability_evaluator` | 只读 |
| `plugin` | 插件注册画像 | 只读 |
| `user` | WebConsole 手工新建 / 复制模板画像 | 可 PATCH / DELETE |

来源由 `persistence.get_profile_source` 判定。用户画像与内置 / 插件画像同 `profile_id` 时，用户版本覆盖——这是设计意图，支持"复制内置模板再改"工作流。

### 3.8 启动接线

`planning/startup.py` 的 `init_planning()`：

1. 导入并注册 Kanban LLM 工具。
2. 调用 `register_builtin_profiles()` 注册 5 个内置画像。
3. 调用 `register_capability_evaluator()` 注册内部能力评估画像。
4. 调用 `load_user_profiles()` 加载用户画像；用户画像可覆盖同名内置画像。
5. 启动期 `recover_zombie_subtasks` 复活心跳过期的 running 子任务，再对所有 running / pending 根任务 `kick_root` 一次。
6. 调用 `restore_armed_templates()`，把所有 `recurring_status='armed'` 的模板重新挂回 APScheduler，保证进程重启不丢周期触发。
7. 注册每日 04:00 的 `kanban_artifact_ttl_cleanup` APScheduler job，调 `AIAgentArtifact.delete_expired()` 清理过期 artifact 行 + 落盘文件（见 §4 / §21 #11）。

---

## 4. Artifact Workspace 安全沙盒

### 4.1 路径与目录结构

`data/ai_core/artifacts/{root_task_id}/{task_id}/workspace/` 是任务节点**唯一**的可写目录。所有产物（中间代码 / 真实文件产物 / 落盘大文本 artifact）都共住这一层：

- 代理写代码 / 跑命令产出的中间文件直接落进 `workspace/`；
- `artifact_put(file_path="x.png")` **不再复制副本**，直接登记 `payload_path=workspace/x.png`；
- `artifact_put(payload="...")` 超过 4KB 时自动落盘到 `workspace/_artifact_<id>.<ext>`。

为什么打平：旧实现把 artifact 文件复制到 `{artifact_id}/payload.<ext>` 子目录，导致同一份产物存两份、webconsole 工作区视图看不到中间代码（实测会话 e05e495b 投诉点）。新实现一个目录覆盖全部，webconsole 文件列表一次性展示，主人格 `send_message_by_ai(image_id="res_xxx")` 拿到的就是 workspace 内的原文件。

`ensure_workspace` 不再按 `agent_profile` 分子目录——保留入参签名以维持兼容，但实际只创建 `workspace/`。

`gsuid_core/ai_core/planning/workspace.py` 集中实现：

| 函数 | 作用 |
|------|------|
| `task_workspace_root(root_id, task_id)` | 返回 `artifacts/<root>/<task>/workspace/` 绝对路径 |
| `ensure_workspace(root, task, profile="")` | 调度器在 `_run_one_task_node` 前创建并绑定到 `PlanRunContext`；`profile` 入参被忽略（仅作历史兼容） |
| `resolve_safe_path(req, fallback, ctx)` | 工具层强制把请求路径解析进 workspace；越界返回原因不写盘 |
| `snapshot_workspace` / `scan_workspace_changes` | 命令执行前后扫描变更 |
| `register_workspace_artifacts` | 把新增文件登记为 `workspace_file` artifact |
| `put_artifact(file_path=...)` | **不复制**，直接登记 workspace 内文件路径；越界文件返回 None |
| `put_artifact(payload=...)` | 短文本走 `payload_inline`；超 4KB 落盘到 `workspace/_artifact_<id>.<ext>` |
| `record_violation` | 把越界拒绝写入 `workspace_violation` 任务日志；累计达 `MAX_WORKSPACE_VIOLATIONS=3` 时直接 `mark_subtask_failed` |

### 4.2 工具层接入

**`file_manager.py`**：
- `_get_safe_path` 优先走任务上下文绑定的 workspace；planning 未就绪时退回原 `FILE_PATH` 沙盒。
- `write_file_content` 越界时调 `_record_workspace_violation` 写任务日志。
- `execute_file` 强制把 cwd 切到当前任务的 workspace；无任务上下文时回退到 `FILE_PATH`（绝不允许是 `Path.cwd()` / 项目根）。

**`command_executor.py`**：
- 执行命令前若处于任务上下文，强制覆盖 `work_dir` 为 workspace。
- LLM 显式传 `work_dir` 时强制白名单——必须落在 `FILE_PATH` 之下，绝对路径越界直接拒绝；相对路径会自动 prefix 上 `FILE_PATH`。
- 无任务上下文 + LLM 也未传 `work_dir` 时，兜底 cwd 为 `FILE_PATH`（**绝不能**是 `Path.cwd()`——后者会让代理在项目根跑 shell，污染主仓库）。
- 执行前拍快照、执行后调 `_register_workspace_changes` 把新增 / 修改文件登记为 `workspace_file` artifact。
- `ALLOWED_COMMANDS` 含 Windows `where`（等同 Unix `which`），便于代理定位解释器；其它白名单见模块顶端。

### 4.3 运行时上下文（PlanRunContext）

`gsuid_core/ai_core/planning/runtime.py` 的 `PlanRunContext` 字段：`task_id` / `step_id` / `root_task_id` / `artifact_workspace` / `allowed_write_roots` / `agent_profile`，由 `_run_one_task_node` 绑定 / 释放。

### 4.4 系统提示词交付边界

所有内置画像 prompt 都注入共享段落 `_DELIVERY_BOUNDARY`（写在 `profiles.py` 顶部），明确：
- 唯一可写目录是 Artifact Workspace。
- **绝不**直接和主人对话；唯一交付方式是 `artifact_put` + 函数返回值。
- 禁止调用 `send_message_by_ai` / `send_meme` 这类直接下发给主人的通道。

### 4.5 workspace patch 处理

`POST /api/ai/kanban/tasks/{id}/workspace/apply-patch` 仅登记 patch 为 `patch` artifact，**不自动 git apply**。自动改主仓库属于高风险动作，应在 WebConsole 上由主人手动审查 + 应用。

### 4.6 ad-hoc workspace（兜底路径）

正常运行时**不应有**新建的 ad-hoc artifact——主人格 `create_subagent(agent_profile=...)` 已经全部走 Kanban 路径（§3.5.1）。`_ensure_adhoc_workspace` contextmanager 仅作"插件代码 / 单测 / 启动早期 planning 未就绪时"的最后一道兜底：

- 路径：`data/ai_core/artifacts/adhoc_<sess_hash>/adhoc_<profile>_<ts>_<rand>/workspace/`
- `root_task_id`：`"adhoc_<sess_hash>"`（`sess_hash` 为 `ev.session_id` 的 MD5 前 10 位）
- `task_id`：`"adhoc_<profile>_<unix_ts>_<rand6>"`
- `agent_profile`：本次委派的画像 id（如 `"code_agent"`），仅作 `PlanRunContext` 元数据；workspace 目录不再按 profile 分层

ad-hoc workspace 仍受 `resolve_safe_path` 守护，命令前后扫描照常登记 `workspace_file` artifact。但因 `root_task_id` 不是任何真实 `AIAgentTask.id`：

- WebConsole `/api/ai/kanban/board` 看不到这些产物（树视图按 `node_kind="root"` 过滤）。
- 主人格 `artifact_get_recent` 默认按"主人活跃根任务"定位，找不到 ad-hoc 产物。
- 历史遗留的 ad-hoc artifact 仍能用 `/api/ai/artifacts/?root_task_id=adhoc_*` 检索。

ad-hoc artifact 同样受 §4.3 的 30 天 TTL 清理（见 §21）。

**注意**：如果你在开发中发现新代码触发了 ad-hoc 路径（warning 日志含 `建立 ad-hoc workspace`），多半是某条调用路径绕过了 `kanban_executor` 直接调 `run_capability_agent` ——请改走 `register_kanban_task` 或主人格 `create_subagent(agent_profile=...)`，统一所有产物归属。

---

## 5. Kanban 任务树（Manager + Executor）

### 5.1 Manager（`planning/kanban.py`）

| 函数 | 说明 |
|------|------|
| `create_kanban_tree(..., subtasks=[...], root_agent_profile="")` | **两种形态**：①`subtasks` 非空 + `root_agent_profile` 空 → 创建根 + N 子任务的常规多步树（子任务可带 `recurring_trigger` 表达子任务级周期）；②`subtasks=[]` + `root_agent_profile` 非空 → 创建"叶子根"（根任务自身就是执行节点，没有子任务）；两者互斥。根任务级周期模板（`recurring_trigger` 非空）不支持叶子根。**注册时硬拦"depends_on 周期子任务"的错误编排**（周期模板永不 completed，下游死锁） |
| `is_leaf_root(root, children_count)` | 判定根任务是否为叶子根（`node_kind=="root"` + `agent_profile` 非空 + `children_count==0`），供调度器 / 状态汇总 / 审批分流使用 |
| `is_recurring_subtask_template(task)` | 判定子任务是否为"周期子任务模板"（非克隆实例）。判据：`node_kind=="subtask"` + `recurring_trigger` 非空 + `template_subtask_id` 为空。模板不参与正常 ready 调度，由专门 arm 路径挂 APScheduler |
| `has_active_recurring_subtask(children)` | 任务树内是否还有 armed 周期子任务模板——根任务汇总状态用，armed 模板等同 "持续生产中"，所有 armed 子任务过期前根任务不允许 completed |
| `deps_satisfied_for(task, children)` | 判依赖是否满足。**新增子任务级周期语义**：依赖项若是 armed 周期模板等同 "持续生产中"视为满足，让下游一次性子任务（如带 `not_before` 的 final）可以与周期子任务并发计算 ready 状态 |
| `get_task_tree(root_id)` / `_query_children` | 拉取整树 |
| `get_ready_child_tasks(children, root_status, now=None)` | 判定子任务"可进入 progress"——依据 `dependency_task_ids` 全部 `completed/skipped`（或依赖项是 armed 周期模板）+ `pending` 状态 + `not_before` 为空或 `≤ now`；根任务在 `paused` / `failed` / `cancelled` / `waiting_approval` 时返回空。**显式排除周期模板子任务本身**（由 arm 路径处理） |
| `get_pending_recurring_templates_ready_to_arm(children, root_status)` | 挑出"依赖已满足、可以 arm 的周期子任务模板"。executor 在每次调度回合入口调用，把模板挂上 APScheduler 后 `recurring_status='armed'` |
| `arm_recurring_subtask(template, trigger_spec)` | 把周期子任务模板挂 APScheduler 并写库 armed；trigger 解析失败 / add_job 失败时模板自动转 disarmed（不阻塞下游） |
| `disarm_subtask_template(subtask_id)` | 主人手动停止 / `recurring_until` 到期：模板 `recurring_status='disarmed'` + 摘除 APScheduler job |
| `clone_subtask_for_fire(template)` | 周期子任务模板到点开火：在同一棵树下克隆一个执行实例子任务（无依赖、不带周期字段，`template_subtask_id` 反向指向模板），模板 `fire_count` 累加 |
| `list_armed_subtask_templates()` | 列出所有 armed 周期子任务模板（启动期 `restore_armed_subtask_templates` 用） |
| `next_not_before(children)` | 返回所有 pending 子任务里最早一次未到的 `not_before`；全无延后子任务时返回 None |
| `compute_kanban_column(task, deps_satisfied)` | 5 列映射（`waiting_approval` → Blocked、`paused` → Blocked、`failed` / `cancelled` → failed） |
| `mark_subtask_running` | 条件 SQL（`WHERE status='pending'`）防并发派活；同样适用于叶子根（不限制 `node_kind`） |
| `mark_subtask_completed` / `mark_subtask_failed` | 落终态 + 写日志；同样适用于叶子根；自动摘除该 task 的 `not_before` + 子任务级 recurring APScheduler job |
| `refresh_root_status(root_id)` | 根任务级短锁 + 子任务状态汇总；人工 `failed` / `cancelled` 状态不被覆盖；**叶子根状态由 `_run_one_task_node` 直接维护，本函数检测到 `is_leaf_root` 时跳过汇总**；**armed 周期子任务存在时根任务保持 running**（让"一棵树跑 N 天"的持续运行语义成立） |
| `respawn_child_task` | `DEFAULT_RESPAWN_LIMIT = 3`，超出自动转 `waiting_approval`；同样适用于叶子根 |
| `request_subtask_approval` / `approve_subtask` / `fail_task_tree` | 审批通路；`fail_task_tree` 是软终结，自动 disarm 所有 armed 周期子任务并摘除 APScheduler job |
| `hard_delete_task_tree(task_id, delete_files=True, include_instances=False)` | WebConsole 硬删除入口：传入根或子任务 id 均删除所属整棵树；删除 `AIAgentTask` / `AIAgentTaskLog` / `AIAgentArtifact` 行；根任务级 + 子任务级 recurring job 都先从 APScheduler 摘除；`include_instances=True` 时连同历史实例树删除；`delete_files=True` 时清理 `data/ai_core/artifacts/{root_task_id}/` |
| `pause_task` / `resume_task` / `abort_task` | 节点级状态操作，WebConsole 与主人格句柄共用；`abort_task` 自动摘除该任务对应的 not_before / subtask-recurring APScheduler job |
| `recover_zombie_subtasks(stale_minutes=15)` | 启动时扫描心跳过期的 running 子任务**和叶子根**（`node_kind=="root"` + `agent_profile` 非空），条件 SQL 重置 pending 并写日志。**周期子任务模板自身永远 pending，不会被本函数当僵尸**；执行实例子任务跟普通子任务一样跟着复活 |
| `clone_tree_for_fire(template_root_id)` | 根任务级周期触发时克隆整棵模板树（见 §5.4） |
| `list_armed_templates()` / `disarm_template(id)` | 管理根任务级周期模板 |

### 5.2 Executor（`planning/kanban_executor.py`）

- `execute_ready_tasks(root_id)`：三种调度形态 + 一个 "周期子任务 arm" 副步骤：
  1. **多步任务树**：扫描树 → **先调 `_maybe_arm_recurring_subtasks` 给依赖刚满足的周期子任务模板挂 APScheduler**（armed 模板不再进 ready 队列，等到点 fire 才克隆执行实例进入 ready）→ 计算 ready 集合 → `asyncio.gather` 并发执行 → 刷新根状态 → 递归 `_schedule_continuation` 最多 4 层，处理"上游刚完成、下游立即就绪"的级联（每层也会再 arm 一次新解锁的周期模板）。
  2. **叶子根**（`is_leaf_root(root, len(children))` 为真）：直接把根任务作为单一可执行节点送进 `_run_one_task_node(root, root)`，跳过子任务循环；状态由 `_run_one_task_node` 自己写完，不需要 `refresh_root_status`。
  3. **根任务级周期模板根**（`recurring_trigger` 非空 + `recurring_status='armed'`）：入口直接返回，不被调度；只由 `recurring._fire_template` 克隆整棵实例后才走形态 1/2。
- `_run_one_task_node(root, child)` 步骤（叶子根模式下 `root == child`）：
  1. 取节点锁（`asyncio.Lock`），防同回合并发。
  2. 条件 SQL 抢 `pending → running`。
  3. `ensure_workspace` 创建工作区（叶子根的 workspace = `artifacts/<root.id>/<root.id>/workspace/`），绑定 `PlanRunContext`。
  4. 收集上游 artifact（`_collect_upstream_artifacts`），拼装 prompt——叶子根没有上游，自然为空。
  5. 调 `run_capability_agent(profile_id, task, ev, bot)`。
  6. 若代理没显式 `artifact_put`，用 `raw_result` 兜底写一份 `output` artifact。
  7. 终态：成功 → `mark_subtask_completed`；失败 → `mark_subtask_failed` + `_notify_failure` 按 `failure_policy` 决定通知 / 整树终结。
  8. 成功且有产物时，调 `_persona_relay` 用主人格口吻播报。
- `_format_subtask_prompt(root, child, upstream)`：`root == child` 时输出"单步任务"格式（省略冗余的"任务树根目标"行）；否则输出多步子任务格式。
- `kick_root(root_id)`：调度入口。`register_kanban_task` / `respawn` / `approve` / `resume` / startup 崩溃恢复都靠它把推进权重新拿回来。

失败检测使用模块级常量 `CAPABILITY_AGENT_ERROR_PREFIX`（`capability_agents/runner.py`）：`run_capability_agent` 失败时返回 `f"{CAPABILITY_AGENT_ERROR_PREFIX}: {e}"`，executor 用 `raw_result.startswith(CAPABILITY_AGENT_ERROR_PREFIX)` 识别。

### 5.3 并发安全清单

| 机制 | 作用 |
|------|------|
| 任务节点级 `asyncio.Lock`（`get_task_node_lock`） | 防同回合并发 |
| 根任务级状态刷新短锁（`get_root_refresh_lock`） | 汇总时防竞态 |
| DB 行级条件 UPDATE（`mark_subtask_running`） | 防多进程双跑 |
| 启动期 `recover_zombie_subtasks` + `kick_root` | 进程崩溃恢复 |

### 5.4 周期触发模板（recurring_trigger）

周期触发有两个粒度，**新版强烈推荐子任务级**（一棵树包完整生命周期），根任务级仅作兼容保留：

#### 5.4.1 子任务级周期模板（**新版推荐**）

- **模板子任务**（`node_kind=="subtask"` + `recurring_trigger` 非空 + `template_subtask_id` 为空 + `recurring_status='armed'`）自身永远停留在 `status='pending'`，**不进入 ready 队列**——`get_ready_child_tasks` 显式排除它。它只是被克隆的样板。
- **arm 时机**：模板子任务的依赖（`dependency_task_ids`）全部 completed 后，由 `execute_ready_tasks._maybe_arm_recurring_subtasks` 调 `arm_recurring_subtask` 挂上 APScheduler，写 `recurring_status='armed'`。
- **fire**：APScheduler 到点调 `_fire_subtask_template(subtask_id, root_task_id)` → 校验 armed + 未过期 → 调 `clone_subtask_for_fire(template)` 在**同一棵树**下新建一个执行实例子任务（`template_subtask_id` 反向指向模板，依赖留空 → 立即 ready）→ `kick_root` 一次让 executor 把实例派出。
- **fire_count**：每次 fire 累加；执行实例 display_name 自动带 `#N` 后缀。
- **执行实例**（`template_subtask_id` 非空）是一次性子任务，与普通 Kanban 子任务行为完全一致；执行完即 completed/failed，不影响模板。
- **跨 fire 持久化**：业务状态（账户 / 持仓 / 流水 / 打卡日历等）靠 `record_*` 维护，与单个执行实例的生命周期解耦——所有实例读写同一组 `record:<集合>`。
- **整树状态**：`has_active_recurring_subtask` 任意 armed 模板存在时，根任务保持 running——这是"一棵树跑 N 天"持续运行语义的实现关键。
- **下游一次性子任务**（如 final 汇总）若需要与周期错开时间，必须用 `not_before` 而**禁止 `depends_on` 周期模板**（永远 armed 不 completed = 死锁），`create_kanban_tree` / `register_kanban_task` 都已硬拦该错误编排。

#### 5.4.2 根任务级周期模板（兼容路径，整棵树克隆）

- **模板根**（`recurring_trigger` 非空，`recurring_status='armed'`）自身永远不被调度——`execute_ready_tasks` 在入口就直接返回。它只是被克隆的样板。
- **克隆实例**（`template_root_id` 指向某模板）是一次性任务树，与传统 Kanban 行为完全一致；执行完即 completed/failed，不影响模板。
- 跨实例的业务持久化（账户 / 持仓 / 流水）由代理在 `record_*` 工具里维护，和 Kanban 树本身的生命周期解耦。
- **保留原因**：根任务级周期模板适合"每次开火都跑同一套子任务并独立结算"的纯周期场景（如每日发一份固定模板的早报，每次都从零开始）。如果跨次需要保留状态、且需要 init 只跑一次、final 在末尾跑一次——**应当用子任务级周期模板（§5.4.1）**而非把所有阶段塞进根级周期（每次开火都会重新跑 init，状态被反复重置）。

#### 5.4.3 周期触发桥（`planning/recurring.py`）

| 函数 | 作用 |
|------|------|
| `parse_trigger_spec(spec)` | 把 `"interval:1800"` / `"cron:0 9 * * 1-5"` 解析为 APScheduler `(trigger_type, kwargs)`；非法格式抛 `ValueError` |
| `schedule_template(template_root_id, spec, end_date)` | 根级模板：调 `aps.scheduler.add_job(replace_existing=True)` 把模板挂上 |
| `unschedule_template(template_root_id)` | 根级模板：摘掉 job（不删模板本身） |
| `_fire_template(template_root_id)` | 根级模板 APScheduler 回调：读模板 → 校验 armed + 未过期 → 调 `clone_tree_for_fire` → `kick_root(instance)`；任何异常不向 APScheduler 抛 |
| `restore_armed_templates()` | 启动期把所有 `armed` 根级模板重新挂回 APScheduler |
| `schedule_subtask_template(subtask_id, root_id, spec, end_date)` | **子任务级**模板：同上语义但 job_id 前缀 `kanban_subrecurring_`；ev / kick_root 用根任务 id |
| `unschedule_subtask_template(subtask_id)` | 子任务级模板：摘除 APScheduler job |
| `_fire_subtask_template(subtask_id, root_task_id)` | 子任务级模板 APScheduler 回调：读模板 → 校验 armed + 未过期 + 根任务未终结 → 调 `clone_subtask_for_fire` → `kick_root(root)` |
| `restore_armed_subtask_templates()` | 启动期把所有 armed 子任务级模板重新挂回 APScheduler |

**APScheduler job id 命名空间隔离**（防误删）：
- 根级周期：`kanban_recurring_<root_id>`
- 子任务级周期：`kanban_subrecurring_<subtask_id>`
- 子任务级 not_before 唤醒：`kanban_not_before_<subtask_id>`

#### 5.4.4 模板根 vs 克隆实例（根级路径，保留）

兼容路径细节同 §5.4.2，但流程图：模板根 `armed` → APScheduler 到点 → `_fire_template` → `clone_tree_for_fire` 克隆整棵实例 → `kick_root(instance)` → 实例按事件驱动执行直至完结。模板本身永远停留在 pending，`fire_count` 累加。

#### 5.4.3 克隆语义（`kanban.clone_tree_for_fire`）

每次开火复制整棵模板树：

1. 新建实例根（`template_root_id=<模板 id>`、`recurring_trigger=None`、`recurring_status=""`），与模板共享 owner / scope / bot / persona 上下文。
2. 按模板子任务顺序逐个新建子任务节点，构建 `tpl_child.id → new_child.id` 映射。
3. 重映射 `dependency_task_ids`（模板内子任务 id → 实例内新 id），保留依赖结构。
4. `fire_count += 1`，写两条 log（模板侧"开火 #N"，实例侧"实例创建"）。
5. 返回 `(instance_root, instance_children)` 给桥接器立刻 `kick_root`。

#### 5.4.5 `register_kanban_task` 工具签名

```python
register_kanban_task(
    goal: str,
    subtasks: List[KanbanSubtaskSpec],   # 每项含 description / agent_profile / depends_on / params_hint
                                          # / not_before / recurring_trigger / recurring_until
    broadcast_to_group: bool = False,
    recurring_trigger: Optional[str] = None,   # 根级周期模板（兼容路径），"interval:<s>" 或 cron
    recurring_until: Optional[str] = None,     # 根级周期模板的 ISO 截止时间
    confirm_one_shot: bool = False,            # 周期意图强校验的逃生口
)

class KanbanSubtaskSpec(BaseModel):
    description: str
    agent_profile: str
    depends_on: List[int] = []
    params_hint: Optional[Dict[str, Any]] = None
    not_before: Optional[str] = None            # 单次延后派出（绝对时刻）
    recurring_trigger: Optional[str] = None     # **子任务级周期模板**（一棵树内 init/recurring/final 共存）
    recurring_until: Optional[str] = None       # 子任务级周期截止时间
```

- 一次性任务（根级 `recurring_trigger=None` + 无子任务级 recurring）：创建后立即 `kick_root`，与旧行为一致。
- **子任务级周期模板**（任意 `subtasks[i].recurring_trigger` 非空）：模板子任务入库时 `recurring_status` 留空，依赖满足时由 executor `_maybe_arm_recurring_subtasks` 自动 arm 挂 APScheduler。一次 register 可创建多个子任务级周期模板 + 一次性 init + 一次性 final 共存于一棵树（**新版推荐**）。
- 根级周期模板：先校验 `recurring_trigger` 格式合法 + `recurring_until` ISO 可解析；通过则创建模板根并 `schedule_template`，**不**直接 kick。
- **子任务级 `not_before`**：每个 `KanbanSubtaskSpec` 可带 `not_before: "YYYY-MM-DDTHH:MM:SS"` ISO 字符串；解析失败直接拒绝创建。注册时框架自动调 `schedule_not_before_wakeup` 给每个子任务挂一个 APScheduler 单次 date job，到点回调 `_fire_not_before` 触发一次 `kick_root(root)`，随后 `get_ready_child_tasks` 自然把该子任务放进 ready 集合派出。子任务进入终态 / 被 respawn / 整树 fail / 硬删除时，对应 not_before job 会被自动摘除。
- **依赖编排硬约束**：禁止任意子任务 `depends_on` 周期子任务模板——后者永远 armed 不 completed，下游死锁。`register_kanban_task` 与 `create_kanban_tree` 两层都做了校验，违反直接拒绝并返回明确文案"请用 not_before 给下游设定开始时间错开"。
- **评估前置 + 模糊匹配**：必须先调过 `evaluate_agent_mesh_capability`；`register_kanban_task` 查 `get_recent_evaluation(owner, goal)` 时按 §3.4 描述的"先精确（前 200 字 strip 相等）后模糊（重叠系数 ≥ `_FUZZY_MIN_OVERLAP=0.30`）"匹配。**阈值从 0.45 降到 0.30**（2026-05-25）——原阈值挡住了"虚拟盘账户初始化"这种从 evaluate user_goal 派生出的"子任务级精简标题"，实测会话 a5696b00 主人格连续被错误拒绝。话题切换后必须重新评估；拒绝时把最近 3 次评估的标题列出来作为回头线索。
- **重复根任务防护**：owner 名下若已存在"活跃且 goal 文本重叠率 ≥ 0.6"的根任务，新调用直接拒绝，引导主人格走 `respawn_subtask` / `fail_task_tree`。阈值 0.6 比 evaluator 模糊匹配（0.30）严，避免误伤同主题但子任务结构差异大的合法新建。实测会话 b8cf57ca 一次对话连开两棵同主题根任务（结构上一棵是另一棵的子集），本防护即专门拦此类重复。
- **周期意图强校验**（2026-05-24 加固，2026-05-25 扩展子任务级支持）：当 goal 命中 `_RECURRING_HINTS_RE`（"每天 / 每隔 / 每开盘 / 持续 N 天 / cron / recurring" 等周期关键词）**且** 根级 `recurring_trigger=None` **且** 所有子任务都**没有 `recurring_trigger` 也没有 `not_before`** **且** `confirm_one_shot=False` 时，**直接拒绝**。返回文案给出四选一指引：
  - (A) **一棵树包完整生命周期**（**新版推荐**）：在 `subtasks` 列表里给周期更新子任务加 `recurring_trigger` 字段——一棵任务树既有一次性 init、周期触发子任务、一次性 final（带 `not_before`）。
  - (B) **整棵树周期模板**（兼容）：传根级 `recurring_trigger="cron:..."` 让整棵树按 cron 克隆实例。适合"每次开火都跑同一套子任务"的纯周期场景。
  - (C) **绝对时间一次性延后**：给每个子任务加 `not_before="<ISO 时间>"`。
  - (D) **立即一次性**（罕见）：显式传 `confirm_one_shot=True`。

  实测会话 17ed4f85（2026-05-24，周日）暴露的硬伤：用户说"每天 A 股开盘期间每半小时看盘"——主人格 register 时漏传 `recurring_trigger`，旧实现仅给软警告就接受，框架立即把 stock_agent 子任务派出执行了一次错误的"周日看盘决策"。本强校验通用拦截所有"看起来要周期执行但忘了表达周期"的领域（股票 / 健康打卡 / 学习计划 / 销售追踪 / 项目追踪等都受益）。
- **限流 + 同 evaluation 豁免**（2026-05-25 升级）：60 秒同 owner ≥ `_REGISTER_KANBAN_LIMIT_IN_WINDOW=5` 次硬限流（**从 3 提到 5**）。**同 evaluation 命中豁免**：本次 register 的 goal 跟最近一次 evaluate 在模糊匹配上命中时不计入窗口——主人格按 evaluator 输出顺序串行建多棵相关树时不会被无辜限流。文案不再硬编码单一根因，而是按 owner 最近的拒绝原因短码（`eval_miss` / `eval_failed` / `dup_active_root` / `recurring_miss` / `bad_args`）分类后给对症诊断（含"想在一棵树里同时表达 init + 周期更新 + 最终汇总请用子任务级 recurring_trigger"提示）。每次 register 拒绝时都会调 `_record_register_reject(owner, code)` 把原因压栈（最多保留 6 条）。原方案曾误诊主人格——实测 17ed4f85 真正根因是 evaluator 输出多 JSON，但限流文案硬指向 recurring_trigger，把主人格带进二次循环。

#### 5.4.5 子任务级 not_before 唤醒桥

`planning/recurring.py` 中除了周期模板桥，还有专门给子任务 `not_before` 用的 APScheduler 单次 date job 桥：

| 函数 | 作用 |
|------|------|
| `schedule_not_before_wakeup(subtask_id, root_task_id, not_before)` | 把一个子任务的 `not_before` 时间挂上 APScheduler 单次 date job；时间已过返回 False |
| `unschedule_not_before_wakeup(subtask_id)` | 子任务进终态 / 被 respawn / 整树终结时摘除 job |
| `_fire_not_before(subtask_id, root_task_id)` | APScheduler 回调：到点 `kick_root(root_task_id)`，由 `get_ready_child_tasks` 再判定一次依赖 / 状态 |
| `restore_pending_not_before_wakeups()` | 启动期把数据库里所有 `pending` 且 `not_before > now` 的子任务重新挂回 APScheduler；与 `restore_armed_templates` 一起跑 |

job id 命名：`kanban_not_before_<subtask_id>`，与周期模板 job id 前缀（`kanban_recurring_`）隔离，避免误删。

---

## 6. LLM 工具集

`planning/kanban_tools.py` 注册的 9 个工具（全部在保底池）：

| 工具 | category | 功能 |
|------|----------|------|
| `evaluate_agent_mesh_capability` | `self` | 能力覆盖评估（前置必备）；缓存按 owner 分组，TTL 15 分钟 |
| `register_kanban_task` | `buildin` | 注册任务树；按"精确 + 模糊（overlap ≥ `_FUZZY_MIN_OVERLAP=0.30`）"匹配最近 evaluate；60 秒同 owner ≥ 5 次硬限流（**同 evaluation 命中豁免**——按最近拒绝原因短码分类对症诊断）；owner 名下"活跃同主题"根任务重叠率 ≥ 0.6 直接拒绝；**goal 含周期意图 + 根级 `recurring_trigger=None` + 无任意子任务 `recurring_trigger` 或 `not_before` + `confirm_one_shot=False` 时直接拒绝**（给 A/B/C/D 四选一指引，其中 A 是新版推荐的"一棵树包完整生命周期"）；新增 `confirm_one_shot` 入参作"显式立刻一次性"逃生口；**子任务可带 `recurring_trigger` + `recurring_until` 表达子任务级周期（一棵树内 init/recurring/final 共存）**；子任务可带 `not_before` ISO 时间延后派出；硬拦 "depends_on 周期子任务"的死锁编排 |
| `respawn_subtask` | `buildin` | 复活 failed / waiting_approval 子任务（≥3 次自动审批） |
| `fail_task_tree` | `buildin` | 终结整树 + 级联 failed；只匹配**活跃**根任务（已 failed / cancelled / completed 的从候选过滤），避免主人格反复 fail 同一棵已 failed 树 |
| `respond_subtask_approval` | `buildin` | 转达主人对 `waiting_approval` 子任务的同意 / 拒绝 |
| `artifact_put` | `buildin` | 登记产出 artifact，三种模式：① `file_path=...` 登记 workspace 内的真实文件（**直接记原路径，不再复制副本**；按后缀自动推断 mime）；② `payload=...` ≤ 4KB inline 文本；③ `payload=...` + 显式 `mime` 大文本落盘到 `workspace/_artifact_<id>.<ext>` |
| `artifact_get` | `buildin` | 按 res 句柄取回 artifact 原文，强校验同 `root_task_id` |
| `artifact_list` | `buildin` | 列出当前任务树的 artifact |
| `artifact_get_recent` | `buildin` | 取根任务最近一份 artifact 原文，专给主人格追问溯源用 |

工具引用约定：
- 自然语言任务引用仅匹配根任务（`node_kind="root"`）。
- 子任务引用句柄形如 `"<root_ref>#sub<N>"`（N 为 1-based 下标）。
- artifact 用显式 `res_xxx` 句柄；`artifact_get` 强校验 `root_task_id` 一致——防跨树读取。
- ad-hoc artifact（`root_task_id="adhoc_<sess_hash>"`）现在仅作启动早期 / 单测 / 插件直接调 `run_capability_agent` 的兜底——`create_subagent(agent_profile=...)` 自 2026-05-24 已统一走 Kanban，正常对话不应再新建 ad-hoc artifact。历史 ad-hoc 数据仍能用 webconsole `/api/ai/artifacts/?root_task_id=adhoc_...` 检索。
- 主人格用 `send_message_by_ai(image_id="res_xxx")` 直接发任何 Kanban artifact——工具会先查 `AIAgentArtifact` 取 payload，再 `RM.register` 转一次 RM 资源后发出，主人格不需要关心两套存储的区分（见 §3.6）。

**`register_kanban_task` 防循环的三段硬约束**（实测会话 7a29c54d / 17ed4f85 / a5696b00 暴露的"主人格反复 register-fail"路径已封堵）：

1. **限流 + 同 evaluation 豁免**（2026-05-25 升级）：模块级 `_REGISTER_KANBAN_RECENT: dict[owner_id, list[float]]` 记录每个 owner 调用本工具的时间戳；新调用先过滤掉超出 `_REGISTER_KANBAN_WINDOW_SEC=60` 秒的旧记录，若剩余 ≥ `_REGISTER_KANBAN_LIMIT_IN_WINDOW=5`（**从 3 提到 5**）直接拒绝。**当本次 register 的 goal 跟最近一次 evaluate 模糊命中时本次调用不计入窗口**——主人格按 evaluator 输出顺序串行建多棵相关树时不会被无辜限流，避免实测 a5696b00 那种"3 棵相关树才创建完第一棵就被限流"的体验。文案按 `_REGISTER_KANBAN_REJECT_REASONS[owner]` 最近 N 条拒绝原因短码（`eval_miss` / `eval_failed` / `dup_active_root` / `recurring_miss` / `bad_args`）经 `_diagnose_register_loop` 归类后给对症诊断——避免误把主人格带进"按错诊文案行动→再失败"的二次循环。每次拒绝路径都会调 `_record_register_reject(owner, code)` 压栈，最多保留 `_REGISTER_KANBAN_REASON_KEEP=6` 条。**通用提示也追加"想在一棵树里同时表达 init+周期更新+最终汇总请用子任务级 recurring_trigger"指引**——把主人格从"必须拆三棵树才能表达持久化模拟"的旧思维里拉出来。
2. **周期意图强校验**（2026-05-24 加固，2026-05-25 扩展子任务级支持，详见 §5.4.5 末段）：`_RECURRING_HINTS_RE` 命中 + 根级 `recurring_trigger=None` + **所有子任务都无 `recurring_trigger` 也无 `not_before`** + `confirm_one_shot=False` → 直接拒绝。返回文案给"一棵树包完整生命周期（子任务级 recurring）/ 整棵树周期模板（根级 recurring）/ 绝对时间一次性延后（not_before）/ 显式立刻一次性"四选一指引，**不再走旧版的软警告路径**——旧版会让"虚拟盘每日看盘"任务在周日（非开盘日）被立刻派出执行一次错误决策。
3. **重复根任务防护**：owner 名下"活跃且 goal 文本重叠率 ≥ 0.6"的根任务存在时，新调用直接拒绝；引导改用 `respawn_subtask` / `fail_task_tree`。
4. **依赖编排死锁拦截**（2026-05-25 新增）：任意子任务 `depends_on` 周期子任务模板时直接拒绝——周期模板永远 armed 不 completed，下游无限等待。文案明确建议"用 not_before 给下游设定开始时间错开"，与子任务级周期模板的标准用法对齐。

**`register_kanban_task` 成功返回文本**含详尽的"接下来会发生什么"摘要——子任务编号 / agent_profile / 依赖关系 / 是 ready 还是 pending、预计 5–60 秒内有进展 / 不要立刻 fail 重建、改 args 应走 `respawn_subtask` 等。周期模板成功返回也会强调"模板本身不立即执行，到点才克隆实例"，避免主人格因为"没看到子任务在跑"就 fail。`confirm_one_shot=True` 跳过强校验的成功返回也会追加 "ℹ️ 你显式传了 confirm_one_shot 已按"立刻一次性"创建" 提醒，避免主人格忘了这棵树不会自动重跑。

---

## 7. 系统提示词决策树

`persona/prompts.py` 的 `SYSTEM_CONSTRAINTS` 决策树与 Kanban 关联的节点：

### 7.1 §3.1 专业域强制委派

即便工具池里出现某专业域代理的工具（如 `stock_agent` 注册后暴露了 `send_stock_info` / `get_stock_change_rate`），遇到该域的"调研 / 解释 / 决策 / 复盘 / 持续观察"类问题，**不得**由主人格自己串工具拼答案。

适用边界：
- **单步 / 即时专业问题**（一次委派即可返回，不需要持久化任务树、依赖边、周期触发或 Artifact 追踪）→ 走 `create_subagent(agent_profile=...)` 委派专业能力代理。
- **多步 / 可追踪 / 需依赖编排 / 周期性 / 长期托管** → 先 `evaluate_agent_mesh_capability`，再 `register_kanban_task` 创建 Kanban 任务树；周期多步任务优先传 `recurring_trigger`。

主人格只在以下三种情况绕过专业代理：用户明确说"你直接查"、工具返回值本身就是最终答案、追问已有代理产物的数值（走 §3.6 artifact 溯源）。

### 7.2 §3.4 scheduled_task ↔ Kanban 边界铁律

四象限工具选择表：

| | 一次性 | 周期性 |
|---|---|---|
| **简单（单步）** | `add_once_task` | `add_interval_task` |
| **复杂（多步 + 需编排）** | 一次性 Kanban 任务树 | `register_kanban_task(recurring_trigger=...)` |

**反枚举铁律**：单轮 `add_once_task` ≤ 2 次；≥ 3 个时间点必须走周期触发。

撞硬上限 / 评估 `covered=false` 时，**禁止退化为表格或方案 A/B 助手腔**——必须保持角色人设。

### 7.3 §3.5 复合多代理任务分支

**触发判据**（满足任一即应走 Kanban）：

- 单一画像跑不动；需要 2~N 个不同 `agent_profile` 接力或并行；
- 任务结论依赖多源数据汇总（爬取 / 分析 / 代码 / 渲染 / 日程 / 记忆 / 业务工具等的组合）；
- 用户要求"持续地 / 每天 / 自主地"做某事且单步搞不定；
- **持久化产物交付**类任务——任何"代理生成的产物会被主人收到 / 事后被追溯"（文件 / 图 / 报告 / 二进制数据 / 数据集快照）的场景，**两条路径产物归属一致**（详见 §3.5.1）：
    * **路径 A**（多步 / 周期 / 依赖编排）→ `register_kanban_task(subtasks=[...])`，主人格显式编排 N 个子任务（看板看到根 + N 子任务）；
    * **路径 B**（单步专业一句话委派）→ `create_subagent(agent_profile=..., task=...)`，框架内部**自动转为创建叶子根 Kanban 任务**并同步等待——看板只看到一张"任务卡"，没有冗余的子任务包装层。
  ad-hoc artifact 路径已不再用于代理人格派活——所有代理人格产物都挂 Kanban、看板可见、`artifact_get_recent` 可追溯，失败时 `respawn_subtask` 自动重试。判别口诀：「**主人会问"那个东西呢" → A 或 B 任选**；**只想要一段纯文本结论无追溯需求 → `create_subagent(task=...)` 不带 `agent_profile`**」。
- **「持久化状态 + 周期更新 + 最终汇总」类**任务——任何形如"维护一份数据集合 + 周期性更新它 + 期末做总结"的任务都属此类，常见形态包括但不限于：虚拟盘 / 模拟交易 / 健康打卡 / 学习计划 / 销售追踪 / 项目追踪 / 给你 N 单位让你管理 N 周期后考察 …。统一用 `record_*` 维护结构化集合，**用一棵树包完整生命周期 + 子任务级 recurring_trigger 编排**（**新版推荐**，与具体业务无关）：
    1. 一次性 **init 子任务**（`depends_on=[]`，立即跑）：`code_agent` 或对应业务画像用 `record_put` 建好本任务要维护的 `record:<集合名>` 集合（账户 / 打卡日历 / 进度表 / 客户名单 / 流水簿等）。
    2. **周期子任务**（`depends_on=[0]`，子任务 spec 带 `recurring_trigger="cron:<由用户描述时段反推>"` + `recurring_until="<截止日 ISO>"`）：init 完成后框架自动 arm 挂 APScheduler，每个 fire 时框架克隆一个执行实例做"查当前状态 → 决策或采集 → `record_append` 写流水 → 必要时 `record_update` 更新主表 → 汇报本次"。**周期子任务自身永远 armed**，整棵树持续 running。
    3. 一次性 **final 子任务**（`depends_on=[]`，带 `not_before="<结算时刻 ISO>"`）：到点自动派出，由 `internal_reporter` 调 `record_list` / `record_summary` 拉数据 + `render_markdown_to_image` 出报告。**严禁 `depends_on` 周期子任务**——周期永不 completed = 死锁；用 `not_before` 时间锚点错开。

  **cron 表达式必须从用户描述的时段反推**，常见模板：
    - "每个工作日上午 / 下午" → `cron:0 9-11,14-17 * * 1-5`
    - "每天早晚两次打卡" → `cron:0 8,21 * * *`
    - "每周一例会前" → `cron:0 9 * * 1`
    - 用户提到行业特定时段（证券开盘、医院门诊、餐饮翻台等）→ 按该行业实际时段写（如 A 股开盘可写 `cron:0,30 9-11,13-14 * * 1-5`）。

  禁忌：① 用旧版"三棵独立树"拆解（init / 周期 / final 各一棵 register）——容易撞 rate-limit、看板上看不出三棵树是同一任务、跨树调度需要主人格手动串接；新版一棵树包完整生命周期；② 把"初始化"塞进周期子任务（每次 fire 都会重置主集合 = 数据被反复清零）；③ 跨域错配时段（把"每天打卡"派进股市开盘时段、把"看盘决策"派进深夜或周末）；④ 用 `add_interval_task` 在主人格侧自己写循环——必须用 Kanban 子任务级 recurring 由框架克隆执行实例。

判定为是时：
1. 先调 `evaluate_agent_mesh_capability`；
2. `covered=false` **或** evaluator 返回失败 / 解析失败 → 二者**等价处理**：拒绝拆任务，如实告诉主人"框架缺什么能力 / 评估代理暂不可用"，**等主人决策**。**绝对禁止**绕过 evaluator 走旁路自己干（不能用 `record_put` + `add_interval_task` / `add_once_task` 拼"持久化状态 + 周期更新"循环；不能反复 `create_subagent` 模拟多步流程）——见 §21 #22；
3. `covered=true` → 调 `register_kanban_task` 创建任务树（事件驱动并发），每个子任务必须分配 evaluate 返回过的已存在 `agent_profile`；**把 evaluate 返回的 `suggested_subtasks[*].params_hint` 原样塞进** register 入参的对应子任务的 `params_hint` 字段——它会在派活时以 JSON 块的形式注入子代理 prompt，让代理拿到真正可抄写的具体指令；丢掉 params_hint = 让代理瞎猜 = 状态没建对；
4. 含周期意图时，**首选** `recurring_trigger` 路径（事件驱动 + 不撞 20 上限）；
5. 含一次性"等到某绝对时刻派出"语义时，**首选** 子任务级 `not_before` 字段（框架自动挂 APScheduler 单次唤醒），不要另开 `add_once_task` 包一层；
6. 失败 → `respawn_subtask` / 等主人审批 / `fail_task_tree` 三选一；
7. 禁止把任务编排细节写入长期人格记忆；
8. 长期记忆与 `record_*` / `state_*` 冲突时，优先采信当前 `record_*` / `state_*`。

### 7.4 §3.6 追问溯源分支

主人追问"为什么 / 基于什么 / 怎么得出"某个先前 Kanban 任务结果中的具体细节时，**必须**先调 `artifact_get_recent` 取回任务树最近一份 artifact 原文，再用角色口吻转告主人；需要逐份查阅时用 `artifact_list` + `artifact_get`。

**严禁**自己重新 `web_search` / `search_knowledge` 拼凑解释——那不是当时做决定的依据，会与原文矛盾。

### 7.5 单轮意图-行为一致性检测

`gs_agent.py` 中：

- 累积本轮模型 thinking 文本。
- 如果 thinking 中点名 `register_kanban_task` / `evaluate_agent_mesh_capability` / `create_subagent`，或出现"任务树""托管""委派""枚举时间点"等任务编排意图关键词，但本轮没有实际调用对应工具，则直接把 no-tool 计数顶到阈值。
- 下一轮立即触发强制提醒，文案补充"口头答应 ≠ 执行"。
- 关键词元组提升为模块级常量 `_INTENT_TRIGGER_KEYWORDS`，避免每轮重建。

---

## 8. Self-Model 演化层

### 8.1 存储结构

`self_model` 存在 `state_store` 表中：`scope = self:{bot_id}`、`state_key = self_model`、`value = Dict[str, List[str]]`。

四个字段：

| 字段 | 含义 | 何时写入 | 注入提示 |
|------|------|---------|---------|
| `commitments` | 对用户作出的承诺 | LLM 调 `update_self_note(note_type="commitment")` | "我的承诺"，取最后 5 条 |
| `preferences_learned` | 观察 / 被告知的偏好 | LLM 调 `update_self_note(note_type="preference")` | "我学到的偏好"，取最后 5 条 |
| `recurring_topics` | 反复出现的话题 | **动态计算**：每轮注入时 `build_self_cognition_context` 用本 scope 的 `group_profile.get_context_tags(scope_key, top_n=5)` 实时取累计 tag 频次的 top 5；group_profile 由 `memory/ingestion/worker.record_entity_tags` 在每次 ingestion 时累加。代码 / WebConsole 也可显式调 `add_self_note(..., field="recurring_topics")` 写静态兜底（动态拿不到时回退）。 | "反复出现的话题"，取动态 top 5（无 scope 时退回静态后 5 条） |
| `self_notes` | 自我复盘 / 反思 | LLM 调 `update_self_note(note_type="reflection")` | "我最近的反思"，取最后 3 条 |

### 8.2 写入入口

| 入口 | 函数 | 字段 |
|------|------|------|
| LLM 工具 | `ai_core/buildin_tools/self_info.py` 的 `update_self_note(content, note_type)` | 对应字段 |
| WebConsole 整字段覆盖 | `webconsole/agent_debug_api.py` 的 `/agent-debug/self_model` PATCH | 任一字段 |
| 代码内部直接追加 | `ai_core/self_cognition.add_self_note(bot_id, content, field)` | 任一字段 |
| 代码内部整字段覆盖 | `ai_core/self_cognition.overwrite_self_model_field(bot_id, field, items)` | 任一字段 |

### 8.3 写入限流

`ai_core/self_cognition.py` 提供三个保护：
- 单条最长 `_MAX_NOTE_CHARS = 200` 字符。
- 同条文本去重；重复写入时 remove + append，相当于"提到末尾视为最新"。
- 每字段最多保留 `_MAX_ITEMS_PER_FIELD = 20` 条，超出时丢弃最早一条。

非法字段名时，`add_self_note` / `overwrite_self_model_field` 会日志告警并返回 `False`，不会污染 `state_store`。

### 8.4 注入时机

每轮 `ai_router → handle_ai` 在拼 user_message 前调用 `build_self_cognition_context(bot_id, user_id, favorability, scope_key=...)`：

- `scope_key` 由 handle_ai 用 `make_scope_key(GROUP if event.group_id else USER_GLOBAL, ...)` 计算并传入；
- 函数内部用 `_compute_live_recurring_topics(scope_key)` 实时取 group_profile 的 top tags 作为 `recurring_topics`；
- 拿不到（scope_key 空 / group_profile 没积累）时退回静态 `self_model['recurring_topics']`。

该段与 `voice_anchor` 一样注入在**用户消息侧**，不写入 `system_prompt`，避免每轮变化的 self_model 抖动 prompt cache 或污染人格定义。

---

## 9. 动态口吻锚点（voice_anchor）

### 9.1 物理位置

`voice_anchor` 不写入 persona 的 `config.json`，最终位置是：

```text
data/ai_core/persona/<name>/voice_anchor.txt
```

原因：把裸字符串 `voice_anchor` 写进 `config.json` 会触发 `StringConfig` 的 `ValidationError → repair_config → update_config → repair_config` 递归死循环（最大递归深度超出）。口吻锚点与 persona 可变配置物理解耦，避免 `StringConfig` schema 死循环。

### 9.2 读取优先级

`persona/resource.py` 的 `_load_voice_anchor_from_disk`：

1. `voice_anchor.txt` 存在且非空 → 直接采用。
2. 否则读取 `persona.md` 正则兜底提取。
3. 都没有 → 返回空串，`handle_ai.py` 自动跳过注入。

### 9.3 `persona.md` 正则兜底提取

提取优先级：`Style (风格)` 块 → `Tone Markers (语气词)` 块 → `Identity` 行 → 空串。

实现细节：
- 三组正则 `_STYLE_BLOCK_RE` / `_TONE_BLOCK_RE` / `_IDENTITY_RE`，同时支持行内形式。
- `_pick_concrete_line` 跳过 `[SLOT: ...]` 模板占位符；跳过元说明前缀（"举出"/"禁止"/"示例"等）。
- 从剩余候选中取最长一行，优先信息密度。
- 缓存 `_voice_anchor_cache`；新增 `invalidate_voice_anchor_cache()`，在 `save_persona` / `delete_persona` 后清缓存。

### 9.4 注入位置

`handle_ai.py` 在 self_cognition 后追加口吻锚点段，注入在**用户消息侧**，不修改 `system_prompt` / persona 定义本身，避免 prompt cache 抖动。

```text
voice_anchor.txt / persona.md
→ get_voice_anchor(persona_name)
→ context_parts → rag_context → final_user_message → UserPromptPart
```

### 9.5 迁移函数

`persona/resource.py` 提供 `migrate_voice_anchor_from_config(persona_name) -> bool`，幂等地把旧版本 `config.json` 中的 `voice_anchor` 字段搬到 `voice_anchor.txt`，从 JSON 中删除该键。写 `.txt` 使用 `<path>.migrate` 临时文件 + `os.replace` 原子写回；失败不留半文件。

`persona/startup.py` 的 `init_default_personas` 在创建默认人格之后扫描整个 `PERSONA_PATH`，对每个目录调用迁移函数。

---

## 10. 历史截断的 tool 配对安全

### 10.1 问题

PydanticAI 里工具结果型消息除 `ToolReturnPart` 外，还有工具参数校验失败时生成的 `RetryPromptPart`。当 `RetryPromptPart.tool_name` 非空时，它绑定具体工具调用并带 `tool_call_id`，也必须有配对的 `ToolCallPart`。旧实现只识别 `ToolReturnPart`，导致孤儿 `RetryPromptPart` 触发模型 API `400 ... tool id not found`，且坏历史继续留在 `session.history` 中每轮崩。

### 10.2 现行实现

`gsuid_core/ai_core/gs_agent.py`：

- `_truncate_history_with_tool_safety` 在收集保留工具结果 id、定位孤儿索引时，把 `tool_name` 非空的 `RetryPromptPart` 一并纳入。`tool_name is None` 的 `RetryPromptPart` 是输出校验重试，不绑定具体工具调用，不计入配对检查。
- 新增 `_drop_orphan_tool_results(history)` 作为最终一致性兜底：丢弃所有找不到配对 `ToolCallPart` 的孤儿 `ToolReturnPart` 和 `tool_name` 非空的孤儿 `RetryPromptPart`；清理后 `ModelRequest.parts` 为空则整条空请求剔除。
- `extract_history()` 无论是否触发截断，都在末尾无条件调用 `_drop_orphan_tool_results`，确保送入 API 的 `message_history` 永远自洽。

---

## 11. Windows subprocess 兼容

### 11.1 根因

`core.py` 为规避 Windows ProactorEventLoop 关闭 socket 时的 `InvalidStateError`，强制设置 `WindowsSelectorEventLoopPolicy`。但 Windows 上 SelectorEventLoop 不支持子进程，`asyncio.create_subprocess_exec(...)` 必然抛 `NotImplementedError`，导致 `code_agent` 调用 `execute_shell_command` 和 `execute_file` 全部失败。

### 11.2 方案

不切回 Proactor，保持 WebSocket 稳定。改为分平台执行：

- POSIX：保留原生 `asyncio.create_subprocess_exec`。
- Windows：用同步 `subprocess.run`，通过 `asyncio.to_thread` 派到独立线程等待，避免阻塞主事件循环。
- Windows 子进程加 `subprocess.CREATE_NO_WINDOW`，避免弹黑窗口。
- `subprocess.TimeoutExpired` 转译为 `asyncio.TimeoutError`，保持上层异常处理契约不变。

### 11.3 改动范围

**`command_executor.py`**：`_IS_WINDOWS = platform.system() == "Windows"`。新增内部函数 `_run_subprocess_async` / `_run_subprocess_in_thread`，`execute_shell_command` 按 `_IS_WINDOWS` 分支。保留原有契约：命令白名单 / 危险模式检测 / 环境变量过滤 / 工作目录检查 / stdout+stderr 合并输出 / timeout / max_output 截断 / 返回码展示。

**`file_manager.py`**：同样新增 `_IS_WINDOWS`。`execute_file` 按平台分支。脚本类型分支保持：`.py` → `python`、`.pyw` → `pythonw`、`.bat`/`.cmd` → `cmd /c`、`.sh` → `bash`、`.ps1` → `powershell -ExecutionPolicy Bypass -File`。

---

## 12. 通用结构化集合原语（record_*）

### 12.1 设计动机

`state_*` 五件套足以表达单字段状态，但当代理需要持久化多条结构化记录（持仓表、交易流水、签到名单、积分日志……）时，扁平 KV 力不从心：无法按 id 更新/删除单条；竞态下"后写覆盖前写"；没有按字段过滤、排序、分页、聚合的能力。

`record_*` 在 `state_store` 之上叠一层**与领域无关**的"具名集合 + 记录"语义。框架不知道也不应该知道"账户/持仓/股票"等业务名词——任何插件或代理都可以在 `record_*` 之上自由构造业务结构。

### 12.2 存储模型

每个集合是 `AIPersistentState` 里的一行：`state_key = record:<collection_name>`，`value = {record_id: payload_dict}` 的 JSON 字符串。所有写操作走 `state_mutate` 的乐观锁，并发安全。**不新增任何数据库表**。

适用规模 ≤ 数千条记录；超出后建议在业务上分片（如按日期拆 `record:trade_log:202605`）。

### 12.3 工具集（`category="buildin"`，进保底池）

实现见 `gsuid_core/ai_core/state_store/record_tools.py`。

| 工具 | 形参 | 用途 |
|------|------|------|
| `record_put` | `collection`, `payload`, `record_id=""`, `scope="auto"`, `ttl_days=None` | upsert 一条记录（整条 payload 覆盖）；`record_id` 留空自动生成 UUID 并返回 |
| `record_append` | `collection`, `payload`, `scope="auto"`, `ttl_days=None` | **追加**一条新记录（自动生成 record_id，**绝不覆盖**已有）；用于"流水 / 日志"类只增不改的集合 |
| `record_update` | `collection`, `record_id`, `patch`, `scope="auto"` | 字段级**合并**（浅 merge）：保留 patch 未提及的字段；记录不存在返回 `"not_found"` 不创建新记录 |
| `record_get` | `collection`, `record_id`, `scope="auto"` | 按 id 取一条 |
| `record_list` | `collection`, `where_field=""`, `where_value=""`, `order_by=""`, `limit=50`, `offset=0`, `scope="auto"` | 列出 / 过滤 / 排序 / 分页 |
| `record_delete` | `collection`, `record_id`, `scope="auto"` | 按 id 删；返回 `"deleted"` / `"not_found"` |
| `record_summary` | `collection`, `sum_field=""`, `scope="auto"` | 返回 count + 首/末 `record_id`；可选对一个数值字段求和/均值 |

三种写入语义辨析：

| 想做的事 | 工具 | 语义 |
|---------|------|------|
| 按 id upsert，整条覆盖（确定要全量替换） | `record_put` | 写整条；未提及字段会丢 |
| 追加一条新流水（一定生成新 id，绝不冲突已有） | `record_append` | 等价于 `record_put(record_id="")` 但显式表达 append 意图 |
| 改某条记录的几个字段（如 status / 余额），其它字段保留 | `record_update` | 浅合并；记录不存在返回 `"not_found"` |

### 12.4 与 `state_*` 的取舍

| 场景 | 推荐 |
|------|------|
| 单字段、单值状态（计数、最近一次执行时间） | `state_set` / `state_get` |
| 顺序追加但无需查询单条（尾部日志） | `state_append` |
| 多条结构化记录、需按 id 更新 / 按字段过滤 / 简单聚合 | `record_*` |
| 大规模写入（≥ 1 万条 / 集合） | 按时间 / 主键分片成多个 `record_*` 集合 |

### 12.5 清理责任与膨胀风险（**真实缺口**，详见 §21 #29）

与 `AIAgentArtifact` 的"每日 04:00 自动清理 job"不同，`AIPersistentState` 表（承载 `state_*` / `record_*` 全部业务数据）**目前没有任何后台 job**做过期或膨胀清理：

- **TTL 机制是懒清理**：`state_store/store.py` 的 `_fetch` 只在读到一条已过期记录时才删除该行；从不主动扫描。`expire_at` 字段虽然有 index，但没有"按 `expire_at < now` 全表 sweep"的 job。
- **默认 `ttl_days=None` = 永久保留**：`record_put` / `record_append` / `record_update` / `state_set` / `state_append` 全套工具的 `ttl_days` 默认值都是 None；代理 / 插件如果不显式传 ttl，记录就会永久驻留 DB。
- **`record_append` 是同一行内的 JSON 累积**：每个 `record:<collection>` 对应**一行** `AIPersistentState`，整个集合（最高数千条）在 `value` 列里是一段 JSON 字符串。`record_append` 是"读 → push → 写回"——并发安全靠乐观锁，但单行 JSON 会随追加次数线性增长。**超过数千条记录后，单次写入的 IO + JSON parse/serialize 成本会显著上升**（数十 KB → 数百 KB → MB 级），最终触发性能急剧下降或单行字段超出 DB 列限制。
- **`state_append` 同理**：列表追加也压在单行的 JSON 列里；`max_length` 参数提供"环形日志"语义，但同样要求调用方显式传。

**当前的缓解责任在调用方**（代理 prompt / 插件代码）：

1. **流水类长尾集合按时间分片**：在业务 prompt / 插件代码里规定，每月 / 每周开一个新集合（如 `record:stock:trade_log_<owner>_202605`），旧分片可一次性显式删除或加 TTL；不要让单个集合无限增长。
2. **显式传 `ttl_days`**：在 prompt 里把"流水类用 90 天 TTL、状态类永久"写成硬规则；评估器 / 业务画像 prompt 应当带这条要求。
3. **使用 webconsole `/api/ai/state-store/entry` DELETE 兜底**：人工巡检兜底，但不是常规手段。

**计划中的强化**（尚未实现，欢迎贡献）：

- 类似 `kanban_artifact_ttl_cleanup` 的每日 sweep job：在 `state_store/startup.py`（待创建）注册 APScheduler，每日扫 `AIPersistentState` 删除 `expire_at < now` 的行。
- 单行 JSON 体积阈值告警：`record_*` 工具在写入前 check `len(serialized) > _STATE_VALUE_SOFT_LIMIT`（如 1 MB）时返回告警并提示分片。

**编排准则**：新代理 / 新插件 prompt 必须明确"长尾追加类集合**默认带 TTL** + **按时间分片**"，否则会重现旧版 v1 长任务 step 表的膨胀问题。本节缺口已记入 §21 #29，是后续要补的工程债。

---

## 13. WebConsole API

4 个 Kanban / 持久化状态相关 API 模块挂载在 `webconsole/setup_frontend.py`，另有能力代理画像 API：

### 13.1 `webconsole/kanban_api.py` —— `/api/ai/kanban/*`

**任务详情 artifact 字段**（前端直接渲染图片 / 预览文本所需，详见 `_artifact_card`）：
- `id` / `kind` / `summary` / `mime` / `size_bytes` / `created_at` / `from_profile` / `task_id`
- `is_image`：mime 是否 `image/*`，前端据此走 `<img>` 渲染或文本预览
- `has_inline` / `has_payload_path`：标识 artifact 存储类型
- `payload_preview`：inline 文本直接吐 ≤ 8KB；落盘文件**仅对文本类 mime**（`text/*` / json / xml / yaml）且 ≤ 64KB 才读出，二进制不读避免乱码 / OOM
- `raw_url`：所有落盘 artifact 都附 `/api/ai/artifacts/{id}/raw`，前端 `<img>` 直接挂这个 URL 渲染图片，文本 / PDF 走同一通路下载

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/ai/kanban/board` | 5 列看板（根 + 子任务卡片混排，按列分桶） |
| GET | `/api/ai/kanban/tasks/{task_id}` | 单任务详情（含子任务列表、日志、artifact + `payload_preview` + `raw_url` + `is_image`，根任务详情额外含 `root_artifacts` 整树合集——前端在根详情页一次性展示整棵任务树所有产物，不必为每个子任务再发请求） |
| POST | `/api/ai/kanban/tasks` | 管理端绕过 LLM 直接创建任务树 |
| POST | `/api/ai/kanban/tasks/{task_id}/pause` | 暂停 |
| POST | `/api/ai/kanban/tasks/{task_id}/resume` | 恢复并触发一次调度 |
| POST | `/api/ai/kanban/tasks/{task_id}/fail` | 软终结（根任务 `cascade=true` 级联 fail，保留表数据 / artifact） |
| DELETE | `/api/ai/kanban/tasks/{task_id}/hard?delete_files=true&include_instances=false` | 硬删除任务树（删任务 / 日志 / artifact 表行，可选删 workspace / payload 文件；子任务 id 会解析到所属根树） |
| POST | `/api/ai/kanban/subtasks/{task_id}/respawn` | 重派 |
| POST | `/api/ai/kanban/subtasks/{task_id}/approve` | 审批 |
| PATCH | `/api/ai/kanban/subtasks/{task_id}` | 修正字段（display_name / goal / agent_profile / 依赖 / params） |
| POST | `/api/ai/capability-agents/evaluate-mesh` | 前端按钮触发评估 |
| GET | `/api/ai/capability-agents/kanban-candidates` | 可用代理列表（排除 evaluator） |

刷新机制：**不做 WebSocket**；前端按按钮 / setInterval 调 GET。

### 13.2 `webconsole/artifacts_api.py` —— `/api/ai/artifacts/*`

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/ai/artifacts?root_task_id=...` | 按根任务列出 |
| GET | `/api/ai/artifacts?task_id=...` | 按任务节点列出 |
| GET | `/api/ai/artifacts/{res_id}` | 元数据 + 8000 字预览 |
| GET | `/api/ai/artifacts/{res_id}/raw` | 下载落盘 payload |
| DELETE | `/api/ai/artifacts/{res_id}` | 删除（含本地落盘） |
| POST | `/api/ai/artifacts/{res_id}/extend-ttl?days=N` | 延长保留 |

### 13.3 `webconsole/workspace_api.py` —— `/api/ai/kanban/tasks/{id}/workspace/*`

| Method | Path | 说明 |
|--------|------|------|
| GET | `.../workspace/files` | 列出 workspace 文件 |
| GET | `.../workspace/files/raw?path=...` | 下载单文件（防越界） |
| POST | `.../workspace/import` | 上传到 workspace，并自动登记 artifact |
| POST | `.../workspace/apply-patch` | 提交 patch 为 `patch` artifact（不自动 git apply） |

### 13.4 `webconsole/capability_agents_api.py` —— `/api/ai/capability-agents/*`

能力代理画像 CRUD。画像 source 权限见 §3.7。

### 13.5 `webconsole/state_store_api.py` —— `/api/ai/state-store/*`（2026-05-25 新增）

代理人格通过 `state_*` / `record_*` 工具写到 `AIPersistentState` 表里的所有持久化业务数据（虚拟账户、持仓、流水、签到名单、积分日志、学习进度等）通过本组 API 向主人 / 前端开放可见性。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/ai/state-store/scopes` | 列出所有 scope（按 key 数倒序） |
| GET | `/api/ai/state-store/keys?scope=...&prefix=...&include_expired=false` | 列出某 scope 下的 key 列表（含元信息但不展开 value） |
| GET | `/api/ai/state-store/get?scope=...&state_key=...` | 取单条 (scope, key) 的完整 value（已 JSON 解析） |
| GET | `/api/ai/state-store/records?scope=...&collection=...&limit=50&offset=0[&where_field=...&where_value=...]` | `record_*` 集合分页拍平展示（与 `record_list` LLM 工具同语义） |
| DELETE | `/api/ai/state-store/entry?scope=...&state_key=...` | 删除单条（兜底清理用，谨慎使用） |

设计原则：**只读 + 删除，不提供写入端点**——写入由代理 / 插件通过工具完成，避免人工改值导致 UI 与代理逻辑状态分裂。响应每条 row 附 `is_record_collection` / `record_collection_name` / `value_type` 字段供前端决定渲染方式。详细文档见 `gsuid_core/webconsole/docs/38-state-store.md`。

**用户可见性闭环**（设计目标）：
- **Kanban API**（§13.1）：看任务树进度、artifact——回答"代理跑到哪一步了？产物是什么？"
- **Artifact API**（§13.2）：看具体产物——回答"那张图 / 那段报告原文长什么样？"
- **State Store API**（本节）：看代理维护的持久化业务状态——回答"虚拟账户余额是多少？持仓里有几只股票？打卡了几天？"

三组 API 一起覆盖"代理人格做事 → 产物可追溯 + 业务状态可查"的完整可见性，是 2026-05-25 架构升级落地用户原始诉求"现在依旧没有相关的网页 API 允许前端查看 agent 新建的账户"的关键。

---

## 14. 主人格回告与通知主人

Kanban 调度器派活时绕过主人格会话，结果如何回到主人：

1. **子任务执行完成**：`kanban_executor._run_one_task_node` 在终态后调 `_persona_relay(fresh, raw_result)` 让一次性主人格 Agent 转译能力代理产出，再由 `_notify(fresh, spoken)` 通过 `bot.send` 推送给主人。relay agent 使用 `session_id=None`（不写 session 日志，避免每次子任务完成都产生 ~67KB 的转译会话噪声）；并被注入 `send_message_by_ai` 工具——后者已统一支持 `image_id="res_xxx"` 自动从 Kanban artifact 读 payload + 转 RM 发送。
2. **子任务失败**：`_notify_failure(root, child, reason)` 按 `failure_policy="notify_persona"` 把 `failure_reason` 用人格口吻转告主人，并提示主人格可走 `respawn_subtask` / `fail_task_tree` / 等待审批。
3. **每轮对话**：`planning.context.build_task_context` 在每轮 user_message 前注入主人名下活跃**根任务**摘要，并对每个根附加：
   - 标注"周期模板（等 cron 触发）"还是"一次性任务"——主人格看到周期模板"没动"是正常的，不要 fail；
   - **每个根下子任务的状态桶**（`运行中×2、已完成×3、失败×1`）和最近 4 条 `running` / `waiting_approval` / `failed` 子任务的明细行（含 `agent_profile` + 短描述）——主人格据此判断"任务在跑 / 还在等依赖 / 已经全完成 / 个别失败"，避免子任务正在跑时盲目 fail 重建。
4. **追问溯源**：主人格按决策树 §3.6 调 `artifact_get_recent` 把任务树最近一份 artifact 原文查回来，用 `artifact_list` + `artifact_get` 可逐份查阅。

---

## 15. 主人审批通路（HITL）

子任务进入 `waiting_approval` 状态的两个触发场景：
- 重派次数达 `DEFAULT_RESPAWN_LIMIT = 3` 后自动转。
- 主人格 / 框架调 `request_subtask_approval(task, approval_prompt)`（高风险动作或显式拦截）。

主人有两条审批通路：

1. **WebConsole 操作**：在 Kanban 看板 Blocked 列点击卡片，走 `POST /api/ai/kanban/subtasks/{id}/approve`（请求体 `{approved, note}`）。底层调 `kanban.approve_subtask`；批准后自动 `kick_root` 一次。
2. **对话回复 bot**：主人直接对 bot 说"同意 / 拒绝（附说明）"，主人格调 `respond_subtask_approval(approved, note, subtask_ref="")`——后端等价于 WebConsole 端点。`subtask_ref` 留空时框架自动定位主人名下唯一的 `waiting_approval` 子任务；多个时主人格按 `<root_ref>#sub<N>` 格式给出引用。

批准 → 子任务退回 `pending` 进入下次调度；拒绝 → 子任务 `failed`，主人格再决定是否 `fail_task_tree` 整树终结。

---

## 16. Kanban 与定时任务的边界

Kanban 的任务树执行是**事件驱动 + 时间唤醒**的并发调度系统。事件驱动由 `kick_root` 推动；时间唤醒由两条 APScheduler 桥提供：

- 根任务级**周期模板**（`recurring_trigger`）：到点克隆一棵实例树，覆盖"工作日每小时跑一遍多步流程""每周一出周报"等多步周期编排。
- 子任务级**`not_before` 一次性延后**：到点 `kick_root` + `get_ready_child_tasks` 过滤通过后派出，覆盖"初始化后等明天某时刻再跑第一次"这种**一次性**延后语义。

准确边界：

| 场景 | 怎么做 |
|------|-------|
| "明天 9 点出周报"（无任务树） | 主人格调 `add_once_task("...09:00:00", "...")` 一次性唤醒，唤醒时再视情况触发 Kanban 或单步处理 |
| "明天 9 点跑一棵任务树" | 创建 Kanban 树时把唯一的根子任务带 `not_before="...T09:00:00"`，框架自动到点 `kick_root` 派出，**无需**额外 `add_once_task` |
| "每天 6 点做单步复盘提醒" | 主人格调 `add_interval_task(1, "...", "days", start_time="...06:00:00")` |
| "工作日上午每小时跑一棵多步任务树"等多步周期任务 | `register_kanban_task(recurring_trigger="cron:0 9-11 * * 1-5", recurring_until="...")`——cron 按用户描述时段反推 |
| 「持久化状态 + 周期更新 + 最终汇总」类（虚拟盘 / 健康打卡 / 学习计划 / 销售追踪等） | **一棵树包完整生命周期**：init 子任务 + 周期子任务（带子任务级 `recurring_trigger`）+ final 子任务（带 `not_before`）；整棵树持续 running 直到所有 armed 周期子任务过期。详见 §7.3 / §5.4.1 / `evaluator` prompt |
| 子任务有依赖关系 | Kanban 自然支持：`subtasks[i].depends_on=[j]` 表示 i 依赖 j |
| 子任务有"等到 N 小时后再跑"语义 | 子任务带 `not_before` ISO 时间——框架自动挂 APScheduler 单次 date job，到点 `kick_root` 一次 |

**周期模板克隆实例时不复制 `not_before`**：周期 cron 已经决定开火时间，再叠 `not_before` 会让所有实例的子任务都被一个早就过期的绝对时间放行/挂起，没意义且容易卡死。

**`add_once_task` 单轮节流**：`add_once_task` 在单轮对话中最多调用 `PER_TURN_ONCE_TASK_LIMIT = 2` 次，超限直接拒绝并提示走 Kanban。`AIScheduledTask` 最大执行次数上限 `MAX_EXECUTION_LIMIT = 150`。

---

## 17. 插件接入：注册业务能力代理

业务画像由插件自行注册，框架不内置业务数据。注册晚于 `init_planning` 也可以，因为画像只在 `kanban_executor._run_one_task_node` 运行时查询。插件可以注册新画像，也可以用同名 `profile_id` 覆盖内置画像。

业务画像在自身 system_prompt 里写"持久化数据用 `record_*` 集合维护"——具体 `collection` 命名按业务命名（账户类、训练日志类、客户档案类、库存类等），框架不关心命名约定，但所有"持久化状态 + 周期更新 + 最终汇总"形态的任务都应当走 §7.3 的"一棵树包完整生命周期"通用模板（init + 子任务级周期 recurring + final）。

下面以金融场景为例（其它领域如健康打卡 / 学习计划 / 销售追踪同构，把 send_stock_info 换成相应的业务工具即可）：

```python
from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,
    register_capability_agent,
)

FINANCE_PROMPT = """你是一个严谨的「量化操盘代理」。你没有任何角色人格，
只对任务结果负责，不做角色扮演、不加语气词。

【工作流】
1. 规划：先输出 <TODO_LIST>，把任务拆成 2~5 步。
2. 执行：依次调用工具完成每一步：
   - 行情查询：send_stock_info / send_my_stock / search_stock
   - 估值：send_stock_PB_info（PB/PE/PS）
   - 资金流向：send_cloudmap_img（板块资金云图）
   - 市场情绪：get_vix_index（A 股 VIX）
3. 决策必须基于工具数据：选股、加减仓、止损止盈都要回答清楚
   "从哪个工具的哪段数据得到的结论"，禁止只凭 web_search 的新闻标题做决定。
4. 高风险动作（实盘下单 / 修改持仓）一律不自己执行，在交付摘要里显式列出
   "需要主人决策的动作"，让主人格转告主人定夺。
5. 持久化模拟类任务（虚拟盘、模拟交易、长期跟踪等）必须使用 record_* 持久化
   本业务的状态集合；命名按本业务自定，原则是"集合名带业务前缀避免冲突"：
   账户 → record_put("stock:account_<owner>", ...)
   持仓 → record_put("stock:position_<owner>", ..., record_id=code)
   流水 → record_put("stock:trade_log_<owner>", ...)
   编排请按 §7.3 "一棵树包完整生命周期"通用模板拆（init 子任务 + 子任务级周期 recurring + final 子任务带 not_before）。

【交付格式】
① 决定 / 推荐（简洁可执行）；② 依据：逐条列理由 + 数据来源；③ 风险提示。
"""


def register_finance_agent() -> None:
    register_capability_agent(CapabilityAgentProfile(
        profile_id="finance_agent",
        display_name="操盘助手",
        when_to_use="需要查行情、做仓位决策、每日复盘的金融任务",
        system_prompt=FINANCE_PROMPT,
        match_keywords=["炒股", "操盘", "股票", "金融", "行情", "选股"],
        tool_names=[
            "send_stock_info", "send_my_stock", "send_my_stock_img",
            "send_stock_PB_info", "search_stock", "get_vix_index", "send_cloudmap_img",
        ],
        tool_query="",
        max_iterations=25,
        max_tokens=40000,
    ))
```

插件未注册时，`agent_profile="操盘"` 会经 `resolve_profile` 回退到 `research_agent`，其"专业域诚实底线"和 evaluator 的双重保险会拒绝给出专业决策，并明确告诉主人"框架未挂载金融数据工具，建议安装对应插件"。

### 17.1 插件画像 prompt 写作要点（实测教训）

为防止业务画像复制 5 个内置画像的形式但忽略关键约束，插件实现 prompt 时**必须**写入以下硬约束（顺序无所谓，但都不能缺）：

1. **`_DELIVERY_BOUNDARY` 段必须拼进画像 prompt**：能力代理交付边界（不直接和主人对话、不调 `send_message_by_ai`、唯一可写目录是 Artifact Workspace）。从 `gsuid_core.ai_core.capability_agents.profiles._DELIVERY_BOUNDARY` 直接导入即可。**否则**画像会自己调 `send_message_by_ai` 给主人发消息，绕过主人格转译，导致语气和上下文断裂。
2. **持久化必须用 `record_*` 不要回退到 `state_*`**：旧实测案例 17ed4f85 中 `stock_agent` 在新任务里直接 `state_list("stock:")` 看到了**旧任务遗留的**状态键就直接复用了——主人本意是"30 万新虚拟盘"，但 stock_agent 把账户读成了上次的 10 万本金。prompt 应**显式禁止**：「禁止用 `state_set` / `state_list` 维护账户/持仓/流水类多条结构化数据；必须用 `record_put` / `record_append` / `record_update` 把每个集合按 `<业务前缀>:<集合名>_<owner>` 维护。如果 `record_get` 取不到，就**新建**而不是回退去翻 `state_*`」。
3. **跨次状态读写顺序**：每次开火（周期模板克隆实例）的子任务都是独立的；要读上次状态请用 `record_get`，要写新流水请用 `record_append`，要改主表请用 `record_update`——三件套语义不要混用。
4. **不要假设画像有 evaluate / scheduler 工具**：业务画像默认不持有 `evaluate_agent_mesh_capability` / `register_kanban_task`——那些是主人格层的工具。业务画像只在 Kanban 派出的子任务里跑，**不要**在 prompt 里写"如果需要更多步骤请自己开 Kanban 任务"。
5. **诚实底线**：业务专业域里如果发现框架未挂载关键外接工具（如插件本身被禁用了某个 API），必须在交付摘要里明说"我做不到这步"，**不要**靠 `web_search` 拼凑结果——评估器 + 主人格 §3.1 决策树已经有"诚实底线"，画像层也要落实。

**虚拟盘类任务的特别提醒**：业务画像（如 sayustock 的 `stock_agent`）已经在 prompt 里写明"虚拟盘必须用 record_*"，但 LLM 仍可能在新任务里看到 `state_list("stock:")` 有旧数据就复用——这是 LLM 行为问题，框架不强制清 state。推荐做法：
- 主人格在 `register_kanban_task` 的"初始化树"子任务 `description` 里**显式声明键名**（如"用 record_put 写入 `stock:account_user_web_01_<task_id>` 资金 300000"）；
- 业务画像 prompt 里把"先查 record_get、查不到再建"写成硬规则。

业务画像注册仍由插件负责，不内置进框架——任何向框架内置"虚拟盘助手"等业务画像的尝试都会破坏框架的领域中立性（详见 §21 #18）。

---

## 18. StringConfig 加固

`StringConfig.repair_config` 自愈化（`gsuid_core/utils/config/utils.py`）：

- 已知 `config_list` 键的值不是 dict → 重置为默认值，避免 `.keys()` 抛 `AttributeError`。
- 不属于 `config_list` 且不是 dict 的旁路字段 → 删除并记录 warning（早期 `config.json` 里遗留的 `voice_anchor` 字符串会在此被清理）。
- 不属于 `config_list` 但形如 dict 的字段 → 保留，可能是模板下线后的历史 GSC 条目，应由显式迁移处理。

这样 `update_config → repair_config → update_config` 每次都至少推进一步，必然收敛。历史排查阶段的 `_decode_with_passthrough` 应急旁路已撤掉。

---

## 19. 向后兼容性

- **旧 `AIAgentTask` 行**：启动期 ALTER + UPDATE 后退化为 `node_kind="root"` / `root_task_id=自身 id` 的只有根节点的退化树，能在 Kanban 视图正常渲染。
- **v1 长任务**：`last_artifact` / `next_run_at` / `not_before` 字段和 `AIAgentTaskStep` 表的 Python 模型已从 `planning/models.py` 移除；旧库 DB 列保留为不再使用的死列（SQLite 不强制 DROP），下次完整重建 schema 时自然消失。
- **v1 长任务 LLM 工具**（`register_long_task` / `task_*` 系列）已全部移除；主人格自动改走 `register_kanban_task` 链路。
- **WebConsole API**：`/api/ai/long-tasks/*` 已移除；`/api/ai/kanban/*` / `/api/ai/artifacts/*` / `/api/ai/capability-agents/*` 为现行入口。
- **voice_anchor**：旧 `config.json` 字段启动期自动迁移；新装机无 `.txt` 时走 `persona.md` 兜底。
- **capability_agents 注册表**：同名 `profile_id` 由后写覆盖前写——内置 5 个画像仍可被插件 / 用户用同 `profile_id` 重写覆盖。
- **Windows subprocess**：依赖 `asyncio.to_thread`，Python 3.9+ 稳定；GsCore 当前 Python 版本满足。
- **FILE_PATH 沙盒**：`file_manager` / `command_executor` 在没有 `PlanRunContext` 时（极早期启动 / planning 未就绪）强制兜底为 `FILE_PATH`（`data/ai_core/file/`），**绝不**回退到 `Path.cwd()` / 项目根。`create_subagent(agent_profile=...)` 路径已统一走 Kanban，由 `kanban_executor._run_one_task_node` 在派活时 `bind_plan_context` 真实任务 workspace；ad-hoc workspace（`capability_agents/runner._ensure_adhoc_workspace`）只在极少数兜底场景生效（§4.6）。无论哪条路径代理实际执行时都有 workspace 绑定。
- **用户画像目录不存在时**：`load_user_profiles` 返回 0，不报错。

---

## 20. 模块文件布局

### 20.1 核心规划层

| 文件 | 作用 |
|------|------|
| `gsuid_core/ai_core/planning/__init__.py` | 模块导言 |
| `gsuid_core/ai_core/planning/models.py` | `AIAgentTask` / `AIAgentTaskLog` / `AIAgentArtifact` |
| `gsuid_core/ai_core/planning/kanban.py` | 任务树 manager |
| `gsuid_core/ai_core/planning/kanban_executor.py` | 并发调度执行器 |
| `gsuid_core/ai_core/planning/kanban_tools.py` | 9 个 LLM 工具集 |
| `gsuid_core/ai_core/planning/workspace.py` | Artifact Workspace 路径守卫 / 登记 / 越界升级 fail |
| `gsuid_core/ai_core/planning/resolver.py` | 自然语言任务引用解析（仅匹配根任务） |
| `gsuid_core/ai_core/planning/runtime.py` | `PlanRunContext` 绑定 |
| `gsuid_core/ai_core/planning/context.py` | 每轮根任务摘要注入（`build_task_context`） |
| `gsuid_core/ai_core/planning/startup.py` | 初始化：注册工具 / 画像 / evaluator + 崩溃恢复 |
| `gsuid_core/ai_core/planning/recurring.py` | APScheduler 桥接：周期触发模板管理 |

### 20.2 能力代理层

| 文件 | 作用 |
|------|------|
| `gsuid_core/ai_core/capability_agents/__init__.py` | 公开入口导出 |
| `gsuid_core/ai_core/capability_agents/registry.py` | 画像注册表 |
| `gsuid_core/ai_core/capability_agents/profiles.py` | 5 个内置画像 |
| `gsuid_core/ai_core/capability_agents/runner.py` | 能力代理运行器 |
| `gsuid_core/ai_core/capability_agents/evaluator.py` | 能力评估代理 + 缓存 |
| `gsuid_core/ai_core/capability_agents/persistence.py` | 用户画像持久化 |

### 20.3 状态存储层

| 文件 | 作用 |
|------|------|
| `gsuid_core/ai_core/state_store/record_tools.py` | 7 个 `record_*` 工具（put / append / update / get / list / delete / summary） |
| `gsuid_core/ai_core/state_store/__init__.py` | re-export `record_*` 工具 |

### 20.4 WebConsole API 层

| 文件 | 作用 |
|------|------|
| `gsuid_core/webconsole/kanban_api.py` | Kanban 看板 / 任务 / 子任务 REST |
| `gsuid_core/webconsole/artifacts_api.py` | Artifact Hub REST |
| `gsuid_core/webconsole/workspace_api.py` | Artifact Workspace REST |
| `gsuid_core/webconsole/capability_agents_api.py` | 能力代理画像 CRUD REST |
| `gsuid_core/webconsole/state_store_api.py` | **持久化状态浏览器 REST**（`AIPersistentState` 表 + `record_*` 集合的只读 + 兜底删除接口；前端用它看代理建的虚拟账户/打卡日历等所有持久化业务数据） |

### 20.5 周边联动文件

| 文件 | 改动 |
|------|------|
| `gsuid_core/ai_core/buildin_tools/__init__.py` | 导入 / 导出 Kanban 工具、`record_*` 七工具集；删除 v1 工具导入 |
| `gsuid_core/ai_core/buildin_tools/file_manager.py` | `_get_safe_path` 接 workspace；`execute_file` 强制 cwd = workspace + **执行前后扫描 workspace 变更登记 workspace_file artifact**（2026-05-25 新增——修复实测会话 a5696b00 中 code_agent 写了 .py 文件但主人格 artifact_list 只看到 .png 的可见性 bug）；兜底为 `FILE_PATH`，绝不退 `Path.cwd()`；越界写日志；`list_directory(dir_path="")` 默认解析为沙盒根目录；`write_file_content` 写入完成后**立刻把新文件登记为 workspace_file artifact**（同样为修复同一 bug），让主人格 artifact_list / 看板工作区视图能即时看到中间代码 |
| `gsuid_core/ai_core/buildin_tools/command_executor.py` | 强制 cwd = workspace；LLM 传 `work_dir` 强制白名单（必须在 `FILE_PATH` 之下）；无任务上下文 + 无 work_dir 时兜底为 `FILE_PATH`；命令前后扫描 workspace 变更登记 artifact；Windows 线程包装；`where` 加入命令白名单 |
| `gsuid_core/ai_core/buildin_tools/scheduler.py` | `MAX_EXECUTION_LIMIT=150`；`PER_TURN_ONCE_TASK_LIMIT=2`；单轮节流检查；20 上限改写提示 |
| `gsuid_core/ai_core/buildin_tools/subagent.py` | `create_subagent(agent_profile=...)` **自动转为创建 Kanban 叶子根任务并同步等待**（`_dispatch_via_kanban`）——产物自动挂 Kanban、看板可见、`artifact_get_recent` 可追溯。**叶子根 = 根任务自身就是执行节点**，看板只看到一张任务卡，不再有冗余的"根 + 1 子任务"双节点结构。轮询超时 `_KANBAN_INLINE_WAIT_TIMEOUT_SEC=180s`，超时后**不强制中止**，返回"任务继续在跑、到 webconsole 看进度"提示。**新增 `transient: bool = False` 显式开关**（2026-05-25）：`transient=True` 时绕过 Kanban 走 `_dispatch_transient_capability_agent` 直跑代理（ad-hoc workspace，无任务卡）——用于"把 X 列出来 / 读一下"等纯 lookup 任务，避免在看板堆出"获取/查看/列出"的脏卡片；**任何会生成文件 / 持久化状态变更的任务必须保持默认 False**。无 `agent_profile` 的通用 Plan-Solve 路径保留（仅内部小调度用） |
| `gsuid_core/ai_core/buildin_tools/message_sender.py` | `send_message_by_ai` 双命名空间解析：`res_xxx` 先查 `AIAgentArtifact` 取 payload 转 RM，找不到再回退 `RM.get`；`img_xxx` 直走 RM；inline 文本 artifact 提示用 `text` 参数发——主人格无需关心两套存储区分（详见 §3.6） |
| `gsuid_core/ai_core/state_store/record_tools.py` | 在 `record_put` 之外补 `record_append`（追加、绝不覆盖）/ `record_update`（字段级浅合并）两件；`_parse_payload` 对常见误用给友好错误：传 list 时提示「集合按需创建，不要传空 list 预占位」；传空 dict 时提示「空 record 没有持久化意义」 |
| `gsuid_core/ai_core/self_cognition.py` | `build_self_cognition_context` 新增 `scope_key` 参数；`recurring_topics` 按 scope 实时从 `group_profile.get_context_tags` 取 top 5；拿不到才退回静态 self_model |
| `gsuid_core/ai_core/handle_ai.py` | 在自我认知注入处计算 `scope_key`（group 或 user_global）并传给 `build_self_cognition_context` |
| `gsuid_core/ai_core/capability_agents/runner.py` | 新增 `_ensure_adhoc_workspace` contextmanager；无 plan_ctx 时建 `data/ai_core/artifacts/adhoc_<sess>/...` 临时 workspace 并 `bind_plan_context` |
| `gsuid_core/ai_core/capability_agents/evaluator.py` | 评估缓存改 per-owner 列表（最多 4 份）；`get_recent_evaluation` 先精确后模糊（重叠系数 ≥ `_FUZZY_MIN_OVERLAP=0.30`，**2026-05-25 从 0.45 降到 0.30** 解决"虚拟盘账户初始化"这种从 evaluate user_goal 派生的精简标题被错误拒绝问题）匹配 register goal；新增 `_extract_first_json_object(text)` 通用兜底：先匹配 ```json 围栏（覆盖会话 e05e495b 暴露的"markdown 标题 + json 围栏"格式漂移），否则从首个 `{` 截起；`_parse_evaluator_output` 用 `json.loads` → `raw_decode` 取首个完整对象，覆盖"多 JSON 拼接 + 后置闲话"两类；`evaluate_capability` 解析失败时**自动重试一次**（`_EVAL_MAX_ATTEMPTS=2`），重试时在 system / user 两侧追加更严格的"裸 JSON"口令；prompt 顶部加严约束「**严格只输出一个 JSON 对象**，结束 `}` 后立即停止」；prompt 已改为「持久化状态 + 周期更新 + 最终汇总」类任务的**"一棵树包完整生命周期"通用编排模板**（init + 子任务级 recurring + final，与具体业务无关）+ cron 表达式按用户描述时段反推规则 + 禁止跨域错配时段（如把日常打卡派进股市开盘时段）硬约束 + 周期触发提示要求把 cron 直接写在对应子任务 spec 的 `params_hint.recurring_trigger` |
| `gsuid_core/ai_core/capability_agents/profiles.py` | code_agent 工具白名单移除 `render_html_to_image` / `render_markdown_to_image`，仅保留代码/命令/state/record；code_agent prompt 强调"artifact_put 必须用 file_path 模式登记真实文件，不要拿 JSON 元数据冒充"|
| `gsuid_core/ai_core/planning/kanban_tools.py` | `register_kanban_task` 拒绝时回打最近 3 次 evaluate 标题；**`_REGISTER_KANBAN_LIMIT_IN_WINDOW=5`**（2026-05-25 从 3 提到 5）+ **同 evaluation 命中豁免**（`_REGISTER_KANBAN_SAME_EVAL_EXEMPT=True`，避免主人格按 evaluator 输出顺序串行建多棵相关树被无辜限流）；文案改为按 owner 最近的拒绝原因短码（`eval_miss` / `eval_failed` / `dup_active_root` / `recurring_miss` / `bad_args`）分类后对症诊断（不再硬指向单一根因）+ 追加"想表达 init+周期+final 请用子任务级 recurring_trigger"指引；**周期意图强校验**——goal 命中 `_RECURRING_HINTS_RE` 且**根级 `recurring_trigger=None` 且所有子任务无 `recurring_trigger` 也无 `not_before`**（2026-05-25 扩展到子任务级）且 `confirm_one_shot=False` 时**直接拒绝**（旧版仅给软警告，导致 17ed4f85 周日虚拟盘任务被立刻派出），返回文案给四选一指引（A 一棵树包完整生命周期/B 整棵树周期模板/C not_before 一次性延后/D 显式立刻一次性）；新增 `confirm_one_shot` 入参作"显式立刻执行"逃生口；**同 owner 活跃同主题根任务重叠率 ≥ 0.6 直接拒绝**（防同主题反复建树）；`KanbanSubtaskSpec` 新增 `not_before` ISO 字段 + **`recurring_trigger` / `recurring_until` 字段（子任务级周期模板）**（2026-05-25 新增）；注册时硬拦"`depends_on` 周期子任务模板"的死锁编排；成功返回文本对带 `not_before` 的子任务显示"等待到 ... 自动派出"、对带 `recurring_trigger` 的子任务显示"周期模板（cron 表达式），依赖满足后 armed 等到点 fire"，本树含子任务级周期时追加整树持续运行的说明；`artifact_put` 支持 `file_path` 模式登记真实文件并按后缀推断 mime；`fail_task_tree` 过滤已 failed/cancelled/completed 候选 |
| `gsuid_core/ai_core/planning/kanban_executor.py` | `execute_ready_tasks` 检测到叶子根时直接把根任务作为单一可执行节点送进 `_run_one_task_node(root, root)`；**新增 `_maybe_arm_recurring_subtasks(root, children)`**（2026-05-25）：每次调度回合入口 + `_schedule_continuation` 每层入口都调一次，把"依赖刚满足的周期子任务模板"批量挂上 APScheduler 转 armed（arm 失败时模板自动 disarmed 不阻塞下游）；`_format_subtask_prompt` 在 `root == child` 时输出"单步任务"格式（省略冗余"任务树根目标"行）；`_persona_relay` 把本子任务 artifact 按"图片落盘 → 其它落盘 → inline 文本"三档排序、第一个落盘文件标 ⭐ 推荐；只给 relay agent 装 `send_message_by_ai` 单工具（已统一支持 res / img / http / base64）；`session_id=None` 不写转译会话日志；`_format_subtask_prompt` 把 `params_override` 渲染成 JSON 块、并显式声明"真实文件用 artifact_put(file_path=...)、纯文本用 payload、持久化业务数据用 record_*" |
| `gsuid_core/ai_core/planning/context.py` | `build_task_context` 在每个活跃根任务下追加子任务状态桶 + 最近 4 条 `running/waiting_approval/failed` 子任务明细行；周期模板显式标注"等 cron 触发"——让主人格看清子任务在跑、避免盲目 fail 重建 |
| `gsuid_core/ai_core/planning/models.py` | `AIAgentTask` 新增 `not_before` 字段（子任务级延后派出）+ **`template_subtask_id` 字段**（2026-05-25 新增，子任务级周期模板实例反向指针）；`recurring_trigger` / `recurring_until` / `recurring_status` / `fire_count` 字段扩展语义到子任务级（同一字段在 root / subtask 都用）；`AIAgentArtifact.delete_expired` 删除过期 artifact 行 + 落盘文件（多 artifact 共享同一 `payload_path` 时按路径去重，不重复 unlink；每日 cleanup job 调用） |
| `gsuid_core/ai_core/planning/kanban.py` | `create_kanban_tree` 新增 `root_agent_profile` 入参支持创建**叶子根**（根任务自身就是执行节点、没有子任务，专给 `create_subagent(agent_profile=...)` 用）；新增 `is_leaf_root(root, children_count)` helper；**2026-05-25 子任务级周期模板新增**：`is_recurring_subtask_template`、`has_active_recurring_subtask`、`deps_satisfied_for`、`get_pending_recurring_templates_ready_to_arm`、`arm_recurring_subtask`、`disarm_subtask_template`、`clone_subtask_for_fire`、`list_armed_subtask_templates`；`refresh_root_status` 在有 armed 周期子任务时保持根任务 running（让"一棵树跑 N 天"语义成立）+ 叶子根跳过汇总；`recover_zombie_subtasks` 同时复活子任务和叶子根（周期模板自身永远 pending 不被误判）；`get_ready_child_tasks` 包含 `not_before > now` 过滤 + 显式排除周期子任务模板本身；`next_not_before` 取最早未到的 not_before；`create_kanban_tree` 接受 spec 内 `not_before`/`recurring_trigger`/`recurring_until`（含死锁编排硬拦）；`mark_subtask_completed/failed/abort_task/respawn_child_task/fail_task_tree/hard_delete_task_tree` 均会摘除对应 not_before + 子任务级 recurring APScheduler job；`fail_task_tree` 兜底把所有 armed 周期子任务 disarm；`clone_tree_for_fire` 不复制 not_before；新增 `_drop_subtask_recurring_job` helper |
| `gsuid_core/ai_core/planning/recurring.py` | 新增子任务级 `schedule_not_before_wakeup` / `unschedule_not_before_wakeup` / `_fire_not_before` / `restore_pending_not_before_wakeups`（job id 前缀 `kanban_not_before_`）；**2026-05-25 新增子任务级 recurring 触发桥**：`schedule_subtask_template` / `unschedule_subtask_template` / `_fire_subtask_template` / `restore_armed_subtask_templates`（job id 前缀 `kanban_subrecurring_`，与根级 `kanban_recurring_` + not_before `kanban_not_before_` 三组命名空间隔离避免误删） |
| `gsuid_core/ai_core/planning/workspace.py` | `ensure_workspace` 不再按 `agent_profile` 分子目录（统一打平到 `workspace/`，所有中间代码 + 真实产物 + 落盘大文本 artifact 同住一层）；`put_artifact(file_path=...)` **不再复制副本**，直接登记 workspace 内原文件路径，越界文件被拒；`put_artifact(payload=...)` 大文本落盘到 `workspace/_artifact_<id>.<ext>` 而非单独子目录；`task_artifact_dir` 函数已移除 |
| `gsuid_core/ai_core/planning/startup.py` | 注册 `kanban_artifact_ttl_cleanup` APScheduler job（每日 04:00）；启动期调 `restore_pending_not_before_wakeups` 把未到期子任务重新挂回 APScheduler；**2026-05-25 新增** `restore_armed_subtask_templates` 把所有 armed 周期子任务模板重新挂回 APScheduler（让"管虚拟盘一个月"等长生命周期任务跨进程重启依然推进） |
| `gsuid_core/ai_core/persona/prompts.py` | 决策树 §3.1 专业域强制委派 / §3.4 工具选择铁律 + **"一棵树包完整生命周期"通用模板**（一次性 init + 子任务级 recurring + final 带 not_before；告别旧版"三棵树"折中）/ §3.5 复合多代理（含"持久化产物交付"判据 + cron 表达式按用户时段反推规则 + **`create_subagent(agent_profile=...)` 自动转 Kanban 叶子根任务的两路径表 + transient=True 子开关**用于纯 lookup 任务避免在看板堆"获取/查看"脏卡片 + workspace_file artifact 自动登记说明 + **evaluator 失败 / 解析失败 = covered=false 等价处理，禁止用 record_*/add_interval_task/反复 create_subagent 走旁路自己干**）/ §3.6 追问溯源；rate-limit 文案从"3 次"更新为"5 次（同 evaluation 豁免）" |
| `gsuid_core/ai_core/gs_agent.py` | `_INTENT_TRIGGER_KEYWORDS` 扩展；`turn_id` 生成；`clear_turn_throttle` 清理 |
| `gsuid_core/utils/database/startup.py` | Kanban 字段 ALTER + 周期触发字段 ALTER + `not_before` 字段 ALTER + `root_task_id` UPDATE 兜底 |
| `gsuid_core/webconsole/setup_frontend.py` | `_import_webconsole_apis` 含 4 个新 API 模块；删除 `long_tasks_api` 导入 |
| `gsuid_core/webconsole/agent_debug_api.py` | Orchestration Board 改读 Kanban |

---

## 21. 后续注意事项

1. **Kanban 支持三层时间唤醒**：
   - **子任务级 `recurring_trigger`**（2026-05-25 新增，**新版推荐**）——一棵树内某个子任务挂 cron 模板，依赖满足后由框架 arm 挂 APScheduler，每次 fire 时框架克隆一个执行实例。适合"管虚拟盘 30 天 / 每日打卡 / 学习计划"等"一棵树包完整生命周期"形态。
   - **根任务级 `recurring_trigger`**（兼容保留）——整棵树作为模板按 cron 克隆出全新实例。适合"每次开火都跑同一套子任务、不需要跨次保留 init 状态"的纯周期场景。
   - **子任务级 `not_before`**——单次延后派出，一次性等到绝对时刻（如"等开盘 / 等下班"）。

   单步周期提醒（无任务树）继续走 `AIScheduledTask` + `add_interval_task`。新场景一律优先用子任务级 recurring；只有当用户描述明确是"每次都从零开始重跑"时才用根级 recurring。
2. **能力代理产物不要直接发给主人**：应走 `_persona_relay`，否则会丢人格口吻。`_persona_relay` 现在会自动把本子任务的 artifact 句柄塞进转译 prompt，并给 relay agent 注入 `send_message_by_ai` 工具（该工具已统一支持 `res_xxx`/`img_xxx`/`http(s)://`/`base64://` 四种 image_id 来源），让人格化转译能同时把产物发给主人——详见 §3.6。
3. **新增业务画像不要内置进框架**：走插件自注册模式，参考 §17。
4. **WebConsole 写操作不要直接改 DB 字段**：应走 `kanban.*` 函数（`pause_task` / `resume_task` / `abort_task` / `respawn_child_task` / `approve_subtask` / `fail_task_tree` / `hard_delete_task_tree`），否则 Kanban 状态汇总、周期 job 与 workspace/artifact 清理会不一致。
5. **修改内存注册表不要直接访问 `_PROFILES`**：使用 `unregister_capability_agent(profile_id)` 等公开 API。
6. **Kanban 任务完成后的自动复盘 / `self_notes` 联动**：v1 长任务移除后这条链路尚未在 Kanban 上重新接线，需要时再在任务树终结时（`fail_task_tree` / 全 `completed` 汇总后）新增一段一次性 Agent 复盘流。
7. **能力代理之间不直接通信**：多代理协作通过 Kanban 子任务依赖边 + Artifact Hub 实现。
8. **Windows 兼容层不要轻易改回纯 `asyncio.subprocess`**：SelectorEventLoop 下会重新触发 `NotImplementedError`。
9. **v1 长任务的旧任务**（如果存在）已退化为只有根节点的退化树；`node_kind=subtask` 的旧 row（若有）请在 WebConsole 手动归并或物理删除。
10. **所有"代理人格派活"产物归属一致**：`create_subagent(agent_profile=...)` 已经改为**自动转 Kanban 叶子根任务**（§3.5.1）——产物总是有 root_task_id 锚点、看板可见、`artifact_get_recent` 可追溯，看板上只是一张任务卡（没有冗余子任务）。ad-hoc workspace 现在仅作"启动早期 / 单测 / 插件直接调 `run_capability_agent`"的兜底（§4.6）。如果你看到日志里出现 `建立 ad-hoc workspace` warning，说明有非常规调用路径绕过了 Kanban 调度器，请改走 `register_kanban_task` 或 `create_subagent(agent_profile=...)`。
11. **Artifact TTL 自动清理**：`planning/startup.init_planning()` 会注册每日 04:00 的 APScheduler job `kanban_artifact_ttl_cleanup`，调 `AIAgentArtifact.delete_expired()` 删除 `expires_at < now` 的行并清理对应落盘文件；TTL 默认 30 天（`workspace.DEFAULT_TTL_DAYS`），主人可通过 `/api/ai/artifacts/{res}/extend-ttl` 手动延长。
12. **`agent_profile_hint` 工具元字段**：本框架**有意不实现**——专业域强制委派依靠 `persona/prompts.py` §3.1 决策树 + evaluator 的双重保险即可，工具注册层加一层 hint 会让 schema 变复杂而收益有限。请勿再次在 `ToolBase` / `ai_tools()` 上添加此字段。
13. **register_kanban_task 的循环防护不可轻易放开**：当 LLM 工具调用 schema 被某些模型误解（如把 `Optional[str]` 误读成 `object | null`），主人格会反复 `register_kanban_task(recurring_trigger=null)` → `fail_task_tree` → 再 register，每轮产生孤儿子任务 + ~67KB 的 relay 会话日志。本框架已用模块级 `_REGISTER_KANBAN_RECENT` 做 60 秒窗口、≥ 5 次（2026-05-25 从 3 提到 5）直接拒绝；**同 evaluation 命中豁免**——主人格按 evaluator 顺序串行建多棵相关树时不会被无辜限流。`fail_task_tree` 也只匹配活跃根任务（已终结的不算）。如未来调宽限制，请先评估"模型若卡住，能否自己跳出循环？" ——绝大多数情况下答案是不能。
14. **不允许直接调 `send_original_pic`**：框架内已无此工具——一律走 `send_message_by_ai(image_id=...)`，由 `buildin_tools/message_sender.py` 统一识别 `res_` / `img_` / `http(s)://` / `base64://` 四种来源（见 §3.6）。任何在 prompt / 文档中写"用 send_original_pic"都属悬空引用，请改为 `send_message_by_ai`。
15. **能力代理产物的"真实文件 vs 元数据冒充"红线**：`artifact_put` 必须按 §2.3 的三种模式择一使用——真实文件用 `file_path=...`，纯文本结论用 `payload=...`。**绝不能**用 `artifact_put(payload='{"file": "x.png", ...}')` 这种 JSON 元数据假装登记了产物——主人格之后 `send_message_by_ai(image_id=res_xxx)` 时只有真正的 `payload_path` 才能被发出去，inline 文本 artifact 会被工具明确拒绝。code_agent 的 prompt 已经强调该规则，所有插件业务画像注册时也应当在自家 prompt 里写明同一规则——否则主人会以为产物丢了。
16. **「持久化状态 + 周期更新 + 最终汇总」类任务的"一棵树包完整生命周期"通用模板**（2026-05-25 升级，旧版"三棵树"折中已淘汰）：①一次性 init 子任务（`depends_on=[]`，立即跑，建好 record 主集合）+ ②周期子任务（`depends_on=[0]`，子任务 spec 带 `recurring_trigger="cron:..."`、`recurring_until="<截止日 ISO>"`，init 完成后框架自动 arm 挂 APScheduler，每次 fire 时框架克隆一个执行实例做"查状态→更新流水→汇报本次"）+ ③一次性 final 子任务（`depends_on=[]`，带 `not_before="<结算时刻 ISO>"`，到点自动派出汇总）。**所有阶段在一棵树内**，整棵树持续 running 直到所有 armed 周期子任务过期。**禁止**让 final `depends_on` 周期子任务（永远 armed 不 completed = 死锁，框架已硬拦）；**禁止**把"初始化"塞进周期子任务（每次 fire 都会重置主集合）；**禁止**跨域错配时段（把"每天打卡"派进股市开盘、把"看盘决策"派进深夜或周末等）；**禁止**用 `add_interval_task` 在主人格侧自己写决策循环。这套模板**与具体业务无关**——虚拟盘 / 健康打卡 / 学习计划 / 销售追踪 / 项目追踪等所有"建状态→周期更新→最后总结"形态都套同一组结构，evaluator + persona/prompts.py §3.5 + register_kanban_task docstring 三处都已写入硬约束。**用户原始诉求落地**：用户曾抱怨"两个都标记已完成了"——本质是旧版"三棵树"模式让 init 树跑完即 completed，用户以为整个虚拟盘任务结束了。新版一棵树包完整生命周期后，根任务状态始终 running 直到 final 跑完才 completed，用户看到的就是"任务持续运行中"。
17. **`register_kanban_task` 重复根任务硬拦**：owner 名下"活跃且 goal 文本重叠率 ≥ 0.6"的根任务存在时，新调用直接拒绝。阈值 0.6 比 evaluator 模糊匹配（0.30）严，是因为后者用来"找最近一次评估"，本拦截用来"防同主题反复建树"——前者宽容、后者严格。如果将来要让插件实现真正的"业务子任务多树并行"，请显式扩入参（如 `allow_dup=True` 并走特殊审计路径），不要把阈值放宽。
18. **不要给框架 prompt 写业务特化的硬编码示例**：evaluator / persona / register_kanban_task docstring 里凡是涉及"什么时段跑""什么集合命名""调哪个业务工具"的指令，一律必须**抽象成域无关模板** + 让 LLM 从用户描述里反推。曾犯错：早期版本把"A 股开盘 cron `0,30 9-11,13-14 * * 1-5`"和"`stock_agent` 调 `record_put('virtual_fund', ...)`"硬塞进 evaluator 系统提示，结果用户问健康打卡 / 学习计划等其它领域时模型机械套用证券时段。修订原则：① 模板结构（一棵树包完整生命周期 / params_hint / 子任务 not_before / 子任务级 recurring_trigger）可硬写——它们是 Kanban 编排语义而非业务知识；② 业务时段、集合命名、工具名 → 一律写成 `<由用户描述反推>` 占位符或"按用户描述的实际时段"指引；③ 列举多个领域示例时务必涵盖至少 3 个互不相干的领域，避免单一示例被模型当成万能模板。

19. **evaluator 输出格式漂移有三层兜底**：实测累计暴露过三种漂移——(a) 17ed4f85 同一份 JSON 输出两份（`Extra data`）；(b) e05e495b JSON 前加 markdown 标题（`**Capability Evaluation ...**\n\n\`\`\`json{...}\`\`\``，导致 `json.loads` 在首字符 `**` 处 `Expecting value`）；(c) JSON 后加闲话。三层兜底：① `_extract_first_json_object` 先匹配 ```json 围栏，否则从首个 `{` 截起；② `_parse_evaluator_output` 用 `raw_decode` 取首个完整对象；③ `evaluate_capability` 解析失败时自动重试一次（`_EVAL_MAX_ATTEMPTS=2`），重试时在 system / user 两侧追加"只输出裸 JSON"口令。**任何新发现的漂移模式都应在这三层里加，不要直接判 `covered=false`**——后者会让主人格陷入"评估失败→自己干→数据混乱"旁路（见 §21 #22）。evaluator prompt 顶部的"只输出一个 JSON 对象"硬约束保留。

20. **`register_kanban_task` 限流文案不要再硬编码单一根因**：本框架经历过两次"误诊"事故——
    - 早期文案说"模型把 recurring_trigger 反复传成 null"；实测 17ed4f85 真正根因是 evaluator 多 JSON 解析失败。
    - 当前实现按 owner 最近 N 次拒绝原因短码（`eval_miss` / `eval_failed` / `dup_active_root` / `recurring_miss` / `bad_args`）分类后**对症诊断**。
    每次 register 失败时都应 `_record_register_reject(owner, code)` 把原因压栈。新增拒绝路径时**必须**同步加一个短码并在 `_diagnose_register_loop` 加分支——否则限流时拿不到对症提示，又会把主人格带进死循环。

21. **周期意图强校验拒绝模板必须保留四选一指引（A/B/C/D）**：当前 `register_kanban_task` 周期意图强校验拒绝时给的 (A) **一棵树包完整生命周期**（子任务级 recurring_trigger，**新版推荐**）(B) **整棵树周期模板**（根级 recurring_trigger 兼容路径）(C) **绝对时间一次性延后**（not_before）(D) **立刻一次性**（confirm_one_shot 逃生口）四选一是 2026-05-25 升级后的稳定模板——之前只有"加 recurring_trigger"一条指引（无法表达 init + 周期 + final 共存），后来扩展到三选一仍不够直观（用户/主人格搞不清三棵树和一棵树的差异），最终四选一是覆盖"持久化任务编排所有合理意图"的最终设计。**不要简化也不要回滚到三选一**。`confirm_one_shot` 是**逃生口而非常用参数**——文案里要明确说"绝大多数情况下不要传 True"。

22. **evaluator 失败 = covered=false 等价处理；禁止主人格走旁路自己干**：实测会话 e05e495b 暴露的最严重缺口——evaluator 因 markdown 标题前缀解析失败时返回 `covered=false`，主人格读到后**绕过 Kanban 自己**调 `record_put("virtual_fund", ...)` + `add_interval_task(...)` 拼出"持久化状态 + 周期更新"循环。这条旁路**完全废了任务**：(a) webconsole 看不见这棵假任务树；(b) `artifact_get_recent` 找不回任何产物；(c) 到点不会自动结算；(d) 没有失败重试 / 审批通路；(e) 长期任务跨重启完全无状态。**主人格在评估失败时唯一允许的行为**是如实告诉主人"框架缺什么能力 / 评估代理暂不可用"并**等主人决策**——主人可能说"再试一次"、"先放着"、"换简单做法"。本约束已写进 `persona/prompts.py` §3.5 的第②步。任何新增"主人格在评估失败时的自救路径"都属于复发本 bug，必须先在本架构文档评审。

23. **叶子根任务**（`create_subagent(agent_profile=...)` 的承载形态）：根任务自身就是执行节点（`node_kind="root"` + `agent_profile` 非空 + 无子任务），看板只显示一张任务卡，**不再有冗余的"根 + 1 子任务"双节点结构**（实测会话 e05e495b 投诉点）。判定通过 `kanban.is_leaf_root(root, children_count)`；调度器、状态汇总、僵尸恢复、审批分流、prompt 拼装都已统一处理叶子根（详见 §3.5.1 / §5.1 / §5.2）。多步任务（路径 A，显式 `register_kanban_task` 传 `subtasks=[...]`）仍保持"根 + N 子任务"结构。**禁止**给周期模板（`recurring_trigger` 非空）创建叶子根——周期模板要被克隆，叶子根没有子任务可克隆，语义不通；`create_kanban_tree` 已硬拦该组合。

24. **artifact 与 workspace 同住一个目录**（实测会话 e05e495b 投诉点之二）：旧实现把中间代码留在 `workspace/` 而把 `artifact_put(file_path=)` 登记的文件 copy 一份到 `{artifact_id}/payload.<ext>`，导致 webconsole 工作区视图只能看到最终产物、看不到中间代码（主人想直接在网页检查代理写的 `weather_legend.py` 找不到）。新实现：(a) `put_artifact(file_path=)` **不复制**——直接登记 workspace 内的原文件路径，越界文件被拒；(b) `put_artifact(payload=)` 大文本溢出落盘到 `workspace/_artifact_<id>.<ext>` 而非单独子目录；(c) `ensure_workspace` 不再按 `agent_profile` 分子目录（统一打平）；(d) `delete_expired` 多 artifact 共享同 `payload_path` 时按路径去重。**任何"再把 artifact 文件搬到独立子目录"的提议都属于复发本 bug**——主人会再次找不到中间代码。

25. **子任务级周期模板**（2026-05-25 新增，**用户原始诉求"一棵树包完整生命周期"的落地**）：旧版"三棵独立树"折中（init / 周期 / final 各 register 一次）的硬伤：①  主人格在 60 秒内连开 3 棵相关树容易撞 rate-limit；② 看板上 3 棵树是分离的，主人看不出是同一任务的不同阶段；③ init 树一跑完就 completed，用户以为整个虚拟盘任务结束了（实测会话 a5696b00 主人投诉点）。新版核心：**`KanbanSubtaskSpec` 接受子任务级 `recurring_trigger` + `recurring_until`**，让一棵树内同时有一次性 init 子任务、周期触发子任务、一次性 final 子任务。周期子任务依赖满足时由 `_maybe_arm_recurring_subtasks` 自动 arm 挂 APScheduler；fire 时框架克隆一个执行实例子任务到同一棵树下；模板自身永远 armed 不 completed；`refresh_root_status` 检测到 armed 周期子任务时保持根任务 running，整棵树呈现"持续运行"状态。**禁止任何子任务 `depends_on` 周期子任务模板**——周期永不 completed = 死锁，`create_kanban_tree` 与 `register_kanban_task` 两层都已硬拦该错误编排，错误文案明确建议"用 not_before 给下游设定开始时间错开"。子任务级 + 根级两种 recurring 共存：根级保留给"每次开火都跑同一套子任务"的纯周期场景；新场景一律优先子任务级。

26. **State Store WebConsole API**（2026-05-25 新增，**用户原始诉求"现在依旧没有相关的网页 API 允许前端查看 agent 新建的账户"的落地**）：见 §13.5。代理人格通过 `state_*` / `record_*` 工具写到 `AIPersistentState` 表里的所有持久化业务数据（虚拟账户、持仓、流水、签到名单等）通过 `/api/ai/state-store/*` 五个端点对外开放（scopes 列表、keys 列表、单条 get、record_* 集合分页拍平、单条 delete 兜底）。**只读 + 删除，不提供写入端点**——写入由代理 / 插件通过工具完成，避免人工改值导致 UI 与代理逻辑状态分裂。前端 UI 应做"scope 选择 → key 列表 → record_collection 表格"三层展开，与 Kanban API + Artifact API 一起构成"代理人格做事 → 产物可追溯 + 业务状态可查"的完整可见性闭环。文档：`gsuid_core/webconsole/docs/38-state-store.md`。

27. **`create_subagent(transient=True)` 显式跳 Kanban 开关**（2026-05-25 新增）：实测会话 a5696b00 主人投诉点之一——主人格用 `create_subagent(agent_profile="code_agent", task="获取任务树 xxx 的 workspace 中的 Python 代码文件")` 想做一个简单的 workspace lookup，但默认走 Kanban 自动建了一张"获取任务树 xxx workspace 的 Python"任务卡，看板被脏卡片污染。新增 `transient: bool = False` 参数：`transient=True` 时绕过 Kanban 走 `_dispatch_transient_capability_agent`，代理在 ad-hoc workspace 直跑，结果文本直接返回主人格，**不建任务卡**。规则：①"把 X 列出来 / 读一下 / 看一眼" → `transient=True`；②"用 X 画图 / 生成报告 / 跑分析" → `transient=False`（默认，自动建任务卡）；③ 拿不准用默认值——多一张任务卡可以删，少一份产物追溯没法补救。主人格 prompts.py §3.4 已写入指南。

28. **workspace_file artifact 自动登记覆盖完整性**（2026-05-25 修复）：实测会话 a5696b00 暴露的 bug——code_agent 用 `write_file_content("guangzhou_weather.py", code)` 写了 .py 文件，然后用 `execute_file("guangzhou_weather.py")` 执行生成 .png，最后只 `artifact_put(file_path="guangzhou_weather_7days.png")` 显式登记了 .png；主人格用 `artifact_list` 查任务产物时只看到 .png artifact，看不到 .py 文件，以为代理"没生成代码"。**根因**：旧版 workspace 自动扫描只挂在 `execute_shell_command` 上，`write_file_content` 和 `execute_file` 都不触发扫描。**修复**：`write_file_content` 写入完成后立刻调 `_register_single_workspace_file(safe_path)` 把新文件登记为 workspace_file artifact；`execute_file` 在执行前快照 + 执行后扫描差异调 `register_workspace_artifacts` 批量登记新增 / 修改文件。三处 workspace 写入入口（`execute_shell_command` / `write_file_content` / `execute_file`）现在都触发自动登记，主人格 `artifact_list` 能看到代理在 workspace 里产生的所有文件。**新加文件写入工具时，必须接同样的自动登记钩子**——否则该 bug 会复发。

29. **`record_*` / `state_*` 的清理责任与膨胀风险**（2026-05-25 审阅暴露的工程债，详见 §12.5）：与 `AIAgentArtifact` 的每日 04:00 自动 cleanup job 不同，承载所有 `state_*` / `record_*` 业务数据的 `AIPersistentState` 表**目前没有任何后台扫描 job**——`expire_at` 只在 `_fetch` 路径上做懒清理。配合工具默认 `ttl_days=None=永久保留`、以及 `record_append` 是"单行 JSON 累积追加"的存储模型，长跑代理（多月虚拟盘、长期签到、销售追踪）会在 DB 里堆出**单行数百 KB ~ MB 级的 JSON 列**，触发 IO 与 parse/serialize 性能急剧下降。**当前唯一兜底**是调用方在 prompt / 插件代码里手工分片（按月开新集合，旧集合带 TTL）。**禁止**复刻"懒清理 + 默认永久"的设计去存任何新的长尾结构（如行情快照、对话历史、积分流水），必须先有 sweep job 或硬限长再加新写入入口。**后续工程债**：在 `state_store/startup.py` 注册类似 `kanban_artifact_ttl_cleanup` 的每日 sweep job + 在 `record_*` 工具里加单行 JSON 体积阈值告警。本条与 §12.5 互为索引，**不要**因为"懒清理已经在用"就误以为膨胀风险已经解决。

30. **复合意图与并行/串行工具调用**（决策树≠单工具调用的澄清）：`SYSTEM_CONSTRAINTS` 的决策树（§7 / `persona/prompts.py`）是**分支判定**结构（"是 → 执行 X，否 → 继续判定下一步"），不是"单轮内只能调一个工具"的约束。PydanticAI 在单轮内既支持并行工具调用，也支持串行多次工具调用——LLM 自己决定。当用户抛出"分析这只股票，顺便查明天天气，下雨就明早 7 点叫我"这类**复合意图**时，正确处理是 LLM 在同一轮内：① 给股票部分走 §3.1 专业域委派（`create_subagent(agent_profile="stock_agent", ...)` 或 `register_kanban_task`）；② 给天气部分直接调天气工具（A 类——工具输出即答案）；③ 给闹钟部分调 `add_once_task`，**三件事并列处理、不互斥**。框架不会因为"走过一次决策树第 3 步"就拒绝再走第 4 步。如果发现某模型在复合意图下只挑一件做（即"first-match 截断行为"），那是模型能力问题而非框架约束——可在主人格 prompt 显式加一条"复合意图必须逐一处理，不要漏需求"。这条目前没有显式写进 `SYSTEM_CONSTRAINTS`，但 §3.5 "复合多代理"判据已经隐含——**不要**为防"漏掉副意图"再在决策树里加一条"逐项处理"的硬规则，那会让简单意图也被拖累。

31. **寒暄门控只控制向量记忆检索，不影响多轮会话历史**（防止维护者误解）：`handle_ai.py` 的 `_should_retrieve_memory` 仅决定"是否走 `dual_route_retrieve` 做跨会话向量搜索"——它**与多轮 conversation history 无关**，session 内的工具调用 / 工具结果 / 上一轮 user/assistant 消息**永远完整加载**（由 `gs_agent.extract_history` 把 `message_history` 直接送给 PydanticAI）。所以"继续昨天那个"会被 `_FORCE_RETRIEVE_RE` 命中"昨天"强制召回；"买它"这种极短指代即便没命中正则，也只是跳过**跨会话长期记忆**的向量检索，**本轮对话的"我刚才说要买什么"在 session 历史里仍在**。**误解的反向**：如果有人提议"把寒暄门控放宽到默认全部跳过"，要明确知道：跳过的是**长期记忆**，不是当下上下文。多轮断裂的真正风险点在 §10 历史截断（工具配对安全）与 §21 #32 fallback summary——不在本门控。新发现的"漏召回"模式应往 `_FORCE_RETRIEVE_RE` / `_EMOTION_RETRIEVE_RE` / `_ENTITY_HINT_RE` 三组正则里加，而**不要**把门控逻辑改成"任何短指代都召回"——后者会让简单寒暄付出每轮一次向量检索 + reranker 的成本。

32. **Token 兜底 fallback 与持久化状态的边界**（不要把"会话压缩"误判为"任务作废"）：`gs_agent._fallback_agent`（`gs_agent.py:954`）在 token 极限时被触发——抛掉 `message_history=[]`，用 `_extract_run_context` 把 ToolReturnPart / TextPart 抽出来当 final_message 喂给一次性精简 Agent。**这一步会丢失会话连续性**（主人格无法继续在同一会话内对话），但**绝不影响**：① Kanban 任务树（独立 DB，跨重启都活着）；② `record_*` / `state_*` 持久化状态（同上）；③ artifact（同上）；④ `AIScheduledTask`（同上）；⑤ self_model / 长期记忆（同上）。换言之，**用户实际正在做的"虚拟盘 / 学习计划 / 销售追踪"业务状态不丢**——只是当前这次对话被压缩了。主人格在下一轮 user_message 时仍能通过 `build_task_context` 拉回所有活跃根任务摘要继续推进。**唯一会丢的**是本次对话的中间推理链；**禁止**因此就在主人格 prompt 里加"任何复杂场景禁用 fallback"——那会让 token 超限时整个对话直接 500，而不是降级到"精简回复 + 任务继续在后台跑"。如果你确实想保留更长的对话推理链，应当往**主动压缩** / **轮次切换前主动总结**方向加（如每 N 轮把更老的回合摘要化）而不是去掉 fallback。

33. **追问溯源的"压力测试"边界与禁止放宽**（防剧本 D 越狱风险）：§3.6 / §21 #22 已经写"追问"为什么"必须先 `artifact_get_recent` 取原文、严禁自己重新 `web_search`/`search_knowledge` 拼凑"。这条约束**完全依赖主人格 prompt 遵循度**，框架没有也无法在工具层强制（不能 detect "LLM 是否在重新拼凑"）。已知失效模式：主人强力施压（"别给我念报告，你自己算一遍！" / "我就是要听你自己的逻辑！"）时，LLM 受"讨好型人格"驱动可能突破 prompt 约束。**已经做的多层防御**：① `artifact_get_recent` 取原文是工具调用层面的"前置必备"（决策树明写）；② evaluator 失败 / artifact 缺失时禁止走旁路（§21 #22）；③ 失败也保人设（§7.3 防"突然变成助手腔"）。**禁止后续放宽**："如果主人 N 次明确要求你直接给答案，可以绕过 artifact 溯源"这类"逃生口"**不要**加——一旦加上，越狱压力可以稳定复现，整个"执行/表达分离"的安全底线塌方。**新增"主人施压绕过"的诉求**必须先在本架构文档评审，并提供工具层的"工具调用记录回溯校验"等技术兜底，而不是仅靠 prompt 让步。如果业务确实需要"主人格自由解释"模式（比如复盘心理分析类），应当走**独立画像**（不走 `artifact_get_recent`，而是直接 `register_kanban_task` 让 `internal_reporter` 重做一份分析）——不要让主人格自己越权。

34. **技术层兜底矩阵：哪些"硬约束"不仅是 prompt**（防"LLM 智商掉线"复发）：以下硬约束都已经从 prompt 提升到 schema / 工具白名单 / DB 校验层，**LLM 漂移也无法突破**——

    | 硬约束 | 技术层实现 | 文件 / 位置 |
    |--------|-----------|-------------|
    | 能力代理不能调 `send_message_by_ai` / `send_meme` | 画像 `tool_names` 白名单根本不含这两个工具；运行时不会被装配进 Agent | §3.3 行 144；`capability_agents/profiles.py` |
    | 能力代理不能跨 `root_task_id` 读 artifact | `artifact_get` 强校验 `plan_ctx.root_task_id` 匹配 | §2.3；`planning/kanban_tools.py` |
    | 能力代理不能在 workspace 外写文件 | `resolve_safe_path` 解析后越界返回 None；3 次累计自动 `mark_subtask_failed` | §4.1 / §4.2；`planning/workspace.py` |
    | 主人格不能 60s 内 register ≥ 5 棵任务树 | `_REGISTER_KANBAN_RECENT` 模块级计数 + 拒绝路径 | §5.4.5 / §21 #13；`kanban_tools.py` |
    | 主人格不能 register "depends_on 周期子任务" | `create_kanban_tree` / `register_kanban_task` 两层校验，违反直接拒绝 | §5.4.1 / §6；`kanban.py` + `kanban_tools.py` |
    | 主人格不能 `add_once_task` 超过 2 次/轮 | `PER_TURN_ONCE_TASK_LIMIT` 节流，第 3 次直接拒绝 | §16；`scheduler.py` |
    | `respawn_count ≥ 3` 强制转 `waiting_approval` | `respawn_child_task` 检查 `DEFAULT_RESPAWN_LIMIT` | §5.1；`kanban.py` |
    | 评估器输出格式漂移 | `_extract_first_json_object` + `_parse_evaluator_output` + `_EVAL_MAX_ATTEMPTS=2` 三层兜底 | §3.4 / §21 #19；`evaluator.py` |
    | 历史截断后工具配对自洽 | `_drop_orphan_tool_results` 无条件兜底 | §10；`gs_agent.py` |

    **仍只靠 prompt 约束的红线**（容易被 LLM 漂移突破，需要持续在 prompt + decision tree 加固）：① 追问"为什么"必须先 `artifact_get_recent`（§21 #33）；② 评估失败禁止走 `record_put + add_interval_task` 旁路（§21 #22）；③ 失败汇报必须保人设（§7.3）；④ 复合意图不漏副意图（§21 #30）。这四条是**剩余的提示词依赖区**，新做风险评估时优先关注。

---

## 22. 关联文档

| 主题 | 文档 |
|------|------|
| AI 触发流转图 | `docs/AI_TRIGGER_FLOW.md` |
| 插件接入 API | `docs/ai_core_api_for_plugins.md` |
| 能力代理画像 WebAPI | `gsuid_core/webconsole/docs/34-capability-agents.md` |
| Kanban WebAPI | `gsuid_core/webconsole/docs/35-kanban.md` |
| Artifact Hub WebAPI | `gsuid_core/webconsole/docs/36-artifacts.md` |
| Artifact Workspace WebAPI | `gsuid_core/webconsole/docs/37-workspace.md` |
| State Store WebAPI（持久化状态浏览器） | `gsuid_core/webconsole/docs/38-state-store.md` |
