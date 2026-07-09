# AI 会话日志「逻辑会话链」重构 + `/ai-history` Trace 瀑布重构 — 交接文档

> 日期：2026-07-08　范围：`gsuid_core`（后端）+ `gsuid_hub`（前端控制台）两仓库
>
> 本文是一次跨两仓库改动的**完成/交接/注意/下一步**说明。源码永远是唯一事实源，本文是设计意图与落地记录。

---

## 一、背景与目标

三个诉求（用户提出，已在实现前对齐）：

1. **会话日志轮转的「中断感」**：旧实现对活跃会话按 `MAX_ENTRIES_PER_FILE = 500` 条**硬切**滚动文件，且每次滚动分配**全新 `session_uuid`**。webconsole 列表按 `session_uuid` 去重 → 一段忙碌会话被切成许多张卡片；且每次滚动还把 `system_prompt + 末尾 15 条`复制成 seed，造成上下文重复。500 也过小——一次重工具调用的 run 就能产生上百条 entry。
2. **`/ai-history` 详情不直观**：旧详情是「聊天气泡时间线」，信息密度低，看不清一次 agent run 的调用栈/耗时/token。目标是重构成 **Pydantic Logfire 式 Trace 瀑布**（紧凑行式、点击展开、甘特条、token 徽章、provider 标签）。
3. **记录「历史重置」操作**：`/clear` 清空、人格切换、Agent 自动裁剪超长历史——这几类「上下文被重置/压缩」的行为要落进 session log，供前端在时间线里画**不同色块**区分。

对齐结论（用户确认）：轮转做**完整方案**（链 + 拼接 + 懒加载）；三类重置**分别用不同标记**；前端做**完整 Logfire 瀑布**。

---

## 二、核心设计：物理分段 ≠ 逻辑会话

把「物理文件」与「逻辑会话」解耦：

- **物理分段（segment）**：单文件仍有体积/条数上限（`MAX_ENTRIES_PER_FILE`，已 500 → **2000**），达上限滚动到新分段文件——纯为限单文件体积与活跃会话常驻内存。
- **逻辑会话链（chain）**：同一会话窗口内滚动出的多个分段共享一个稳定 `chain_id`（`segment_index` 递增、`prev_segment` 指向上一分段）。webconsole 按 `chain_id` **归并成一张卡片**，详情按 `segments` 顺序**拼接/懒加载**。分段对用户不可见。

这样「单文件体积/内存可控」（原轮转的初衷，防 24/7 活跃群单文件无限膨胀，见 commit `c2023c8`）与「一段会话 = 一张卡片」（语义连续）二者兼得，同时**取消了 seed 上下文复制**（连续性由 chain 归并承载）。

---

## 三、完成内容（按区域 + 文件）

### A. 后端 · 会话日志序列化器
`gsuid_core/ai_core/session_logger.py`
- 新增 chain 三元组实例属性 `chain_id` / `segment_index` / `prev_segment`，并写入 `_build_data()` 头部（放在 `entries` 之前，保持「头部快速读取」契约）。
- 三条创建路径都设置 chain 身份：
  - **A 续写**（窗口内、未达上限）：沿用磁盘文件的 `chain_id`/`segment_index`/`prev_segment`（旧格式无 chain_id → 以 `session_uuid` 兜底）。
  - **B 创建即滚动**（窗口内但旧分段已满）与 **`_roll_to_new_file()`**（运行期达上限）：继承旧 `chain_id`，`segment_index + 1`，`prev_segment = 旧文件名`，**不再 seed**，新分段不重复记 `system_prompt`。
  - **C 全新**：`chain_id = 首段 session_uuid`，`segment_index = 0`。
- `MAX_ENTRIES_PER_FILE` 500 → **2000**；删除 `SEED_TAIL_ENTRIES` 与 `_build_seed_entries()`。
- 新增 entry 类型 **`history_reset`** 入白名单 `SESSION_ENTRY_TYPES`；新增 `log_history_reset(reason, detail)` 方法与 `HistoryResetReason = Literal["user_clear","persona_switch","auto_compact"]`。
- 模块 docstring 的「会话窗口规则 / 文件格式契约」两节已同步重写。

`gsuid_core/ai_core/models.py`
- `SessionLogFileData` 增加 `chain_id` / `segment_index` / `prev_segment`（`NotRequired`，兼容旧文件）。

### B. 后端 · 历史重置标记接线
- `gsuid_core/buildin_plugins/core_command/core_ai_control/__init__.py`
  - `clear_ai_session`（`/clear`、`清空会话`）：`remove_ai_session` **之前**对活跃 logger 打 `history_reset("user_clear")`。
  - `switch_persona`（`persona`、`人格切换`）：打 `history_reset("persona_switch", {"persona_name": 新人格})`。
- `gsuid_core/ai_core/gs_agent.py::extract_history`
  - 仅在「因超长而真正裁剪且确有条目被丢弃」时打 `history_reset("auto_compact", {"before", "after"})`；纯孤儿工具结果清理 / `max_history<=0` 的 stateless 模式**不打**（避免噪声）。`extract_history` 每 run 调一次，频率可控。

### C. 后端 · WebConsole API 链归并
`gsuid_core/webconsole/ai_session_logs_api.py`
- `SessionLogSummary` 增加 `chain_id` / `segment_index` / `segment_count?` / `segments?`；新增 `SegmentMeta` TypedDict。
- `_parse_log_file_base` / `_build_summary_from_memory`：填充 `chain_id`（旧文件回退 `session_uuid`）/ `segment_index`。
- 新增 `_seg_meta` / `_aggregate_chain` / `_group_segments_into_chains`；**重写 `_build_unified_list`**：先合并「单分段」（内存覆盖磁盘同 uuid + is_active 修正 + linked_agents enrich），再**按 chain_id 归并成链卡片**。`overview` / `categories` 复用同一归并列表，天然改为「按链计数」。
- 详情接口仍以**单分段**为粒度（前端按 `segments` 逐段取），内存详情分支补 `chain_id`/`segment_index`/`prev_segment`。

### D. 前端 · Trace 瀑布 + 链列表 + 重置色块
- `gsuid_hub/src/lib/api.ts`：`SessionLogEntryType` 加 `'history_reset'`；新增 `HistoryResetReason`、`SegmentMeta`；`SessionLogSummary` 加 chain 字段；`SessionLogDetail` 加 `chain_id?`/`segment_index?`/`prev_segment?`。
- `gsuid_hub/src/components/ai-history/TraceWaterfall.tsx`（**新文件**）：`buildTrace()` 把扁平 entries 重建为 span 树（run → chat / tool / subagent 子 span），行式密排渲染（时间 · 缩进+展开+子数 · 图标+标签 · token 徽章 Σ↗↙ · 甘特条 · 时长），点击展开内容/子 span，子 Agent 懒加载嵌套子瀑布；`history_reset` 提升为顶层色块（按 reason 着色）。
- `gsuid_hub/src/pages/AIHistoryPage.tsx`（**重写详情区**）：列表按 `chain_id` 归并（显示分段数/子Agent数徽章）；选中链先加载**最新分段**、可「加载更早的分段」并按 `segment_index` 升序拼接；详情区改用 `TraceWaterfall`。删除旧气泡时间线组件。
- i18n：`zh-CN` / `en-US` / `ja-JP` 三份 `aiHistory.json` 补 `entryType.sessionResumed`、`waterfall.*`（含 `reset.userClear/personaSwitch/autoCompact`）、`segmentsCount` / `subAgentsCount` / `loadEarlierSegments` / `downloadSession` / `downloadSuccess` / `selectSessionHint`。
- 主题：甘特条/图标/色块用 tailwind 颜色 + `dark:` 变体（`darkMode:["class"]` 已启用），并复用 `muted`/`primary` 等 CSS 变量，亮暗两套均可读。

---

## 四、契约速查

- **文件头新增字段**：`chain_id: str`、`segment_index: int`、`prev_segment: str|null`。
- **新 entry 类型**：`history_reset`，`data.reason ∈ {user_clear, persona_switch, auto_compact}`；`persona_switch` 带 `persona_name`，`auto_compact` 带 `before`/`after`。
- **列表卡片新增字段**：`chain_id`、`segment_index`、`segment_count`、`segments[]`（`SegmentMeta`）。
- **前端取分段详情**：`memory` 分段用真实 `session_id`+`session_uuid`（取实时）；`disk` 分段用 `file_name` 去 `.json` 的 stem 作 `session_id`（O(1)）。按 `segment_index` 升序拼接 `entries`。

---

## 五、注意事项 / 坑

1. **向后兼容**：旧格式文件无 `chain_id`，一律回退 `chain_id = session_uuid` → 每个旧文件各自一张卡片（与旧版行为一致），随 8 天日志清理自然淘汰。**不需要迁移脚本**。
2. **活跃分段的实时性**：链最新分段活跃时，前端用真实 `session_id` 走内存分支取实时 entries；磁盘上的该分段文件可能滞后至多一个持久化周期（增量追加不刷表头）——这是既有语义，列表活跃状态以内存为准。
3. **内存边界靠分段上限**：本次**未**做「内存只留尾窗」的裁剪（活跃会话仍把当前分段全部 entries 驻留内存）。常驻内存由 `MAX_ENTRIES_PER_FILE=2000` + 单条已有的体积约束（图片外置、tool_return 截断 2000 字）共同限定在数 MB 量级。若未来单会话内存仍偏大，见「下一步」。
4. **`auto_compact` 频率**：`extract_history` 每 run 调一次，仅在真正裁剪时打标；长会话进入裁剪期后可能每 run 一条。前端已把 `auto_compact` 画成低调灰色小色块；如仍嫌多可在后端做「同 run 内去重」。
5. **`history_reset` 不切分段**：它只是「时间线内的重置标记」，**不**触发新分段（分段仅按体积/条数滚动）。若未来想让 `/clear` 成为链边界（clear 前后各一张卡片），需在 resume 逻辑里识别「上段因 reset 收尾」并起新 chain——当前**刻意未做**，以保持链连续。
6. **`HistoryManager._enforce_token_limit`（消息历史的 token 上限裁剪）未打标**：那是与 `GsCoreAIAgent.history` 独立的另一条历史（且该处拿不到 session logger）。本次「Agent 主动丢弃 history」只覆盖 `gs_agent.extract_history`（喂给模型的 pydantic 历史）。见「下一步」。
7. **标准环境外无法 import webconsole API 模块**：本机裸 Python 直接 `import ai_session_logs_api` 会因 FastAPI/Starlette 版本不匹配（`Router.__init__() got unexpected keyword 'on_startup'`）失败——这是**环境问题、非本次改动引入**。纯归并函数已单测通过（见验证），但**端点级 smoke-test 需在运行中的 Core 实例里做**（见下一步清单）。

---

## 六、验证情况

- **后端 logger（隔离临时目录 + 降低上限强制滚动）**：`chain_id` 跨 3 个分段保持不变、`segment_index = [0,1,2]`、`history_reset` 正确落盘。✅
- **后端 API 归并纯函数单测**：多分段聚合的 `segment_count` / `entry_count` 求和 / `type_counts` 合并 / `linked_agents` 跨段去重 / 活跃态取最新分段 / 身份取最新分段，全部符合预期。✅
- **前端**：`tsc -p tsconfig.app.json --noEmit` 我方改动文件 **零类型错误**（仓库既有的 `EChartsWrapper`/`use-toast` 错误与本次无关）；`vite build` **成功**；`eslint` 改动文件 **0 error 0 warning**。✅
- **`py_compile`**：`session_logger.py` / `models.py` / `gs_agent.py` / `core_ai_control` / `ai_session_logs_api.py` 全部通过。✅

**尚未做（需在运行实例里验证）**：真实 Core 起服务后 `/api/ai/session_logs` 列表/详情端点返回新字段、前端瀑布对真实日志的渲染与子 Agent 懒加载、`/clear`·人格切换·超长自动裁剪三类色块的实际呈现。

---

## 七、下一步 / 后续可做

1. **端点 smoke-test**（必做）：起 Core，对活跃与历史会话各验一次列表（含 `segments`）+ 详情 + 子 Agent 展开 + 三类 `history_reset` 色块。
2. **精确 chat 时长**：当前「对话」span 时长由 `ModelRequestNode` 时间戳到下一节点近似。若要精确，可在 `gs_agent` 为模型请求补显式 span start/end 时间戳（新 entry 字段或新 `node_transition` 细分）。
3. **超大链的详情分页**：现按「整分段」为最小加载单元（分段≤2000 条，够用）。若要更细，可加按 `entries` offset 的分段内分页。
4. **内存尾窗**：若单活跃会话内存偏大，可在持久化后把内存 `entries` 裁到尾窗，并让「活跃分段详情」改为 磁盘段 + 内存尾窗 合并读取。
5. **补 `HistoryManager` token 裁剪标记**：给 `_enforce_token_limit` 也接一个 `auto_compact` 标记（需把 session logger 引用传进 HistoryManager，或改为在调用点打标）。
6. **长瀑布虚拟化**：极长 trace（数千行）可对顶层 span 列表做虚拟滚动。
7. **可选：`/clear` 作为链边界**（见注意事项 5），若产品希望 clear 前后分成两张卡片。

---

## 八、相关文件清单

**gsuid_core**
- `gsuid_core/ai_core/session_logger.py`、`gsuid_core/ai_core/models.py`
- `gsuid_core/ai_core/gs_agent.py`
- `gsuid_core/buildin_plugins/core_command/core_ai_control/__init__.py`
- `gsuid_core/webconsole/ai_session_logs_api.py`
- 文档：`gsuid_core/webconsole/docs/23-ai-session-logs.md`（API 契约，已更新）、本文件

**gsuid_hub**
- `src/lib/api.ts`
- `src/components/ai-history/TraceWaterfall.tsx`（新）
- `src/pages/AIHistoryPage.tsx`（重写详情区）
- `src/i18n/locales/{zh-CN,en-US,ja-JP}/aiHistory.json`

---

## 九、2026-07-09 后续修订（视觉打磨 + 列表性能）

一轮基于真实数据的复盘，动了 4 处：

### A. 前端 · 瀑布可读性
`gsuid_hub/src/components/ai-history/TraceWaterfall.tsx`
- **层级竖线**：每行按 `depth` 渲染浅色竖线（`bg-border/70 dark:bg-border`，`inset-y-0` 贯穿全行），
  位置 `RAIL_LEFT(80)+i*INDENT(14)` 对齐各祖先层展开箭头；顶层容器去掉 `gap-0.5`（改 `py-1` 撑行距）
  以保证竖线连续。`INDENT` 常量统一 padding/margin/竖线三处间距。
- **图标/文字分色**：`kindColor()` 从 `{icon,bar}` 扩为 `{icon,bar,text}`，`text` 与 `icon` 同色系
  差一档（如 chat=`sky-700/300` 配 icon `sky-500`），亮暗均可读；generic 里 `thinking`/`text_output`
  各给独立图标（`Lightbulb`/`AlignLeft`）与配色，不再一片灰灯泡。删除失效的 `nested` prop。
- **i18n**：`spanLabel` 的 generic 分支补 `thinking`/`text_output`/`token_usage`/`node_transition`
  → `aiHistory.entryType.*`，不再直显英文原始类型。

### B. 前端 · 侧边栏卡片重构
`gsuid_hub/src/pages/AIHistoryPage.tsx`
- 从「Radio/FileCheck 图标 + 三行彩色小图标 meta」改为**persona 头像 + 脉冲绿点（活跃）+ 两行紧凑布局**
  （行1 名称+类型徽章…右对齐时间；行2 人格·条数+分段/子Agent 紧凑徽章）。选中态用 `ring-inset` 不再位移。
  清掉 4 个不再用的图标 import（Radio/FileCheck/Clock/Database）。

### C. 后端 · 列表接口冷启动从 ~8s 降到 ~0.4s（本轮重点）
`gsuid_core/webconsole/ai_session_logs_api.py`
- **根因**：约 1.1k 主 + 13.7k subagent ≈ 14.9k 文件；内存摘要缓存只在进程内有效，Core 每次重启
  首次构建列表要重读全部文件头部（实测 ~7.9s，即「列表要等十几秒」）。
- **两处修复**：
  1. **sidecar 持久化摘要缓存**：`_load_persist_cache`/`_save_persist_cache` 把 `(mtime,size)+摘要`
     落到 `data/ai_core/session_logs_summary_cache.json`（在 `session_logs/` **之外**，不被扫描/清理）。
     重启先载入、只对 mtime/size 变化或新增文件重解析；仅在缓存有增删（`_cache_dirty`）时才写盘。
  2. **`os.scandir` 替代 `iterdir()+path.stat()`**：`_iter_log_files_with_stat` 一次拿到 name+stat 透传给
     `_parse_log_file_base(path, st)`，缓存命中连文件都不开（warm-path stat 0.55s→0.08s）。
- **实测**：冷启（无 sidecar，一次性）~5s；**重启（有 sidecar）~0.4s**；进程内 warm ~0.3s。
  `_build_unified_list` / `_build_log_index` 均已接入。

### D. 后端 · `_find_existing_log_on_disk` 只解析最新候选
`gsuid_core/ai_core/session_logger.py`
- 旧实现每次建 logger 都 `json.load` **所有同前缀文件**（链化后同一 session_id 分段变多，成本随之涨）。
- 改为 `os.scandir` 只取 name+mtime，按 mtime 由新到旧**只解析最新 1 个**（mtime≥updated_at，是可靠的
  最近写入信号）；最新超窗则直接短路新建，最新损坏才回退更旧候选。已过 newest-wins / 损坏回退 /
  超窗→None / 无匹配→None 四类隔离测试。

**验证**：改动文件 `tsc`(app 配置) + `eslint` 0 error；`vite build` 成功；后端三处 `py_compile` 通过；
列表冷/重启/热三态与 `_find_existing_log_on_disk` 四类用例均实测通过（隔离数据/临时目录）。
sidecar 缓存文件位于 gitignore 的 `data/` 下，不入库。

### E. 前端 · 「对话 → 思考/文本」嵌套一层
`gsuid_hub/src/components/ai-history/TraceWaterfall.tsx`
- 根因：gs_agent 里 thinking/text_output/tool_call 在 **CallToolsNode** 阶段落盘，而旧 `buildTrace`
  在 CallToolsNode 的 `node_transition` 处就关闭了 chat，导致它们掉到 run 级、与「对话」同级。
- 改法：`node_transition` 只在 **ModelRequestNode**（新轮次）与 **End** 处开/关 chat，CallToolsNode
  **不再关闭**；thinking/text_output 从 `chat.contentEntries` 改为 `chat.children.push(...)`，
  嵌套成「对话」的子 span（工具调用仍留在 run 级，保留独立时长条）。chat 生命周期由
  run_start/run_end/End/下一个 ModelRequestNode 兜底关闭，无跨轮泄漏。
- 配套：`TraceWaterfallInner` 加 `useEffect` 在数据变化时**默认展开所有 run 与 chat**（子 Agent 仍
  懒加载不自动展开），让嵌套层级一进来就可见。

### F. 交互模式变化 tag（主动 ↔ 被动）——后端权威标记 + 前端双模式兼容
- 背景：主 session 在内存时，`proactive/emitter.py` 把主动消息作为 `proactive_emission` **直接写入
  Chat 会话**（与用户触发的 run 交错在同一文件，见 emitter 文档）。
- **后端权威标记**（`gsuid_core/ai_core/session_logger.py`）：logger 维护 `_interaction_mode`，
  `log_run_start` 标记 **reactive**（主 session 的 run 皆用户发话触发）、`log_proactive_emission`
  标记 **proactive**；**仅在已知模式翻转时**打一条 `mode_change` entry（`data.mode`/`data.from`）。
  新增 entry 类型 `mode_change` 入白名单 + `InteractionMode` Literal。续写/滚动/重启时
  `_infer_mode_from_entries` 从既有 entries 末态**重建模式**，避免跨分段/重启后误判首次翻转
  （连磁盘回退的 `log_standalone_proactive` 路径也因此正确打标）。subagent 无模式概念、直接跳过。
- **前端双模式兼容**（`TraceWaterfall.tsx`）：`buildTrace` 遇 `mode_change` 直接按位置插权威 tag 并置
  `sawExplicitMode`；**有权威标记就跳过**末尾的 kind 推断，**没有**（旧日志）才回退到前端推断——
  两种来源同一套 `MODE_STYLE`（`to_reactive`「进入被动聊天」/`to_proactive`「转为主动发言」，居中虚线细条）。
  `api.ts` 加 `'mode_change'` 到 `SessionLogEntryType` + `InteractionMode` 类型。
- `EntryBlock` 为 `proactive_emission` 单列渲染：展开时显示来源(heartbeat/scheduled/kanban/tool) +
  触发原因 + 正文，让主动/被动在展开态也一眼可辨。
- i18n：三语 `aiHistory.json` 的 `waterfall` 下新增 `mode.toReactive`/`mode.toProactive`；
  `spanLabel` 的 generic 分支补齐 `thinking`/`text_output`/`token_usage`/`node_transition` 的 i18n。

**E/F 验证**：`tsc`(app) + `eslint` 0 error（api.ts 既有的 2 处 any/1 warning 与本次无关）、
`vite build` 成功；三语 `mode.*` 键补齐并过 JSON 校验；后端 `py_compile` 通过 +
`_infer_mode_from_entries`（空/末 run_start/末 proactive/mode_change 四例）与 `_mark_interaction_mode`
（首次不打、同模式不打、翻转打 mode+from）隔离单测通过。chat 生命周期经代码走查确认。
