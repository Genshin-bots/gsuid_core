# GsCore AI Core：OOC 两通道 / 能力委派闭环 / 意图上下文 — 更新与交接

> **日期**：2026-07-24
> **状态**：工作区未提交改动汇总（以源码为准）
> **读者**：后续继续改 `ai_core` 的开发者 / Agent
> **关联**：
> - 生命周期总览：[`docs/AI_AGENT_LIFECYCLE_SEQUENCE.md`](AI_AGENT_LIFECYCLE_SEQUENCE.md)
> - 红线：[`docs/LLM.md`](LLM.md)
> - 开发技能：`docs/skills/gscore-development/`（触发流 / Agent / 工具注册）
> - 历史评审：`docs/AI_CORE_CHANGE_REVIEW_20260712.md`

---

## 0. 一句话摘要

本批更新解决三类生产问题：

1. **OOC（出戏）**：模型把 JSON / 表 / fence 数据写进角色台词，污染语域并「教坏」后续 history。
2. **委派失效**：能力代理专属工具仍可被主人格直调 → `create_subagent` 入池但几乎 0 调用。
3. **意图误判**：省略跟进（「然后呢」）被当闲聊砍工具；软触发每句都打 LLM 浪费。

并附带：**框架去域词化**（提示/分类不再硬编码股价等业务词）、**统计/控制台**小补丁、**LLM.md 注释规范**清理、单测与 pyright 修复。

---

## 1. 背景：为什么要改

### 1.1 现象（来自 session 日志 / 评测）

| 现象 | 后果 |
|------|------|
| 台词里混 ` ```report ` / 裸 JSON / 制表线 | 用户看到「分析员」而不是角色；history 固化后越来越像报表 |
| 主人格工具池含 stock/code 等专属工具 | 捷径直调，不走 `create_subagent`，专业链路与边界失效 |
| 「然后呢」「改成明天」判闲聊 | 工具规程 LITE / 无 progressive tools，跟进办不成 |
| 软触发每条短消息都过轻量 LLM 门 | 成本高；纯「哈哈」类也应零模型沉默 |
| 框架 prompt / 分类词表塞业务词（股价/大盘…） | 框架与插件域耦合，换场景要改核心 |

### 1.2 设计原则（本批统一立场）

1. **结构证据优先于域关键词**
   是否「数据」看 JSON/表/kv 密度；是否「可跟进」看真实 `ToolCallPart` 轨迹；是否委派看工具归属节点，不看股票词表。

2. **两通道（台词 / 制品）**
   用户可见与 history 只留台词；表格/JSON 进 `<report>` 或 fence 制品 → 出图；history 全量抹结构，**发送与否**只影响 `metadata.sent_reports`。

3. **委派是流程强制，不是 prompt 希望**
   静态池剥离 exclusive + `find_tools`/`RetrievableToolset` 禁止回灌 + roster 注入真实 `node_id`。

4. **LLM.md**
   新代码避免 `getattr`/无注解；`#` 注释 ≤2 行、精简；类型用 `Protocol`/`Sequence`/`isinstance`。

---

## 2. 改动总览

### 2.1 规模

- 约 **63** 个已跟踪文件变更，+1400 / −1050 量级。
- 新文档：`docs/AI_AGENT_LIFECYCLE_SEQUENCE.md`（消息 12 阶段）。
- 新单测：
  - `tests/test_ooc_structured_intercept.py`
  - `tests/test_intent_context.py`
  - `tests/test_reactive_gate_prefilter.py`
  - `tests/test_capability_delegation_flow.py`
- 新 API：`gsuid_core/webconsole/ops_diagnostics_api.py`（运维诊断）等控制台补丁。

### 2.2 主题地图

```text
用户消息
  │
  ├─ handle_ai：同用户 prior + 近几轮 ToolCall → 意图
  ├─ 软触发 → reactive_gate 规则预筛（零 LLM）→ 必要时轻量模型
  ├─ context_assembly：TOOL_ORCHESTRATION LITE/全文（不进持久 history）
  └─ gs_agent.run
        ├─ 静态池装配 → 剥离 exclusive → 注入 create_subagent + roster
        ├─ find_tools 受 blocked_tool_names 约束
        ├─ 工具返回后 POST_TOOL 契约（只约束通道，不认业务词）
        └─ 发送/入史：_split_speech_and_artifacts + _compact_report_blocks_in_history
```

---

## 3. 主题 A：OOC 结构化两通道

### 3.1 目标

- 任意 ` ```lang ` 名都不可信；**body 形态**决定是否进制品通道。
- 可执行代码（如 `def hello`）留在台词。
- 未闭合 fence **不得吞掉**尾部台词。
- 入史**总是**抹数据块（防教坏）；`sent_reports` **仅**实际发送时写 metadata。

### 3.2 关键实现

| 符号 | 文件 | 作用 |
|------|------|------|
| `_fence_body_is_data` | `utils.py` | fence body 是否数据（JSON≥2 键 / 制表 / 密度） |
| `_extract_embedded_structured_blocks` | `utils.py` | 闭合 fence → 未闭合 fence → 段落密度 → 裸 JSON |
| `_split_speech_and_artifacts` | `utils.py` | XML `<report>` + 上表嵌入块 |
| `_compact_report_blocks_in_history` | `utils.py` | 全量抹结构 + 条件写 `sent_reports` |
| `_POST_TOOL_OUTPUT_CONTRACT` | `gs_agent.py` | 本轮有 ToolReturn 后注入：数据进 report 通道 |

### 3.3 为什么这么写

| 决策 | 原因 |
|------|------|
| 不认 `report`/`json` 语言标签 | 模型乱写 lang 名；靠标签会被绕过或误杀 |
| 不认「股价」等域词 | 框架层不得绑定插件业务 |
| 未发送的结构也从 history 删除 | 留着会正反馈教模型继续写 fence |
| 标题进 `metadata` 而非正文「已发资料图」 | 避免假声明；装配层用 titles 提示「用户可能问图」 |
| 未闭合 fence 用 JSON span / 空白分段切尾部 | 截断输出常见；整段 `$` 吞台词是真 bug |

### 3.4 简 diff（语义）

```diff
# utils：两通道拆分（示意）
+ def _fence_body_is_data(body: str) -> bool:
+     # JSON dict / 制表线 / 密度 — 无 lang、无域词
+
+ def _extract_embedded_structured_blocks(text):
+     # 1) 闭合 ```...```
+     # 2) 未闭合 ```：仅数据前缀，台词尾部回 speech；体内已有 ``` 则跳过
+     # 3) 段落密度 / 裸 JSON 配平
+
  def _compact_report_blocks_in_history(messages, sent_texts=None):
-     # 旧：可能写「已发资料图」进正文；未发送块策略与教坏冲突
+     # 新：一律 speech=去结构；sent_reports 仅 was_sent 时写入 metadata
```

```diff
# gs_agent：工具返回后的通道契约（示意）
+ _POST_TOOL_OUTPUT_CONTRACT = (
+     "…开口只保留角色台词；表格/JSON/…必须 <report>…"
+ )
+ # 本轮出现 ToolReturn 后 append 到 user 侧（事件驱动，非业务关键词）
```

### 3.5 回归测试

- `tests/test_ooc_structured_intercept.py`
  - 三键 JSON fence → 制品
  - 表 fence 任意 lang
  - XML report
  - python 代码 fence 留台词
  - 未闭合 fence + 尾部台词
  - history compact：未发送不写 `sent_reports`，但结构仍抹

- `eval/agent/ooc_long_test.py`：长会话 OOC / 泄漏指标增强

---

## 4. 主题 B：能力代理委派闭环

### 4.1 目标

主人格交互会话（`create_by in Chat|Agent|Test`）：

1. 池中**不能**出现能力代理 exclusive 工具（`task_basics` / `self` / `buildin` / `meta` 共享除外）。
2. 需要时强制可见 `create_subagent` + **真实** `node_id` 清单。
3. `find_tools` **不能**把 exclusive 再拉回来（生产曾因只 strip 静态池而闭环失败）。

### 4.2 关键实现

| 符号 | 文件 | 作用 |
|------|------|------|
| `_capability_exclusive_tool_names` | `gs_agent.py` | 各 node 工具 − 共享基建 |
| `_format_capability_roster` | `gs_agent.py` | user 侧可抄的 node_id 列表 |
| `ToolContext.blocked_tool_names` | `models.py` | 本轮禁止动态暴露的名字 |
| `find_tools` 过滤 | `dynamic_tool_discovery.py` | 加载结果去掉 blocked |
| `RetrievableToolset(exclude=静态∪blocked)` | `gs_agent.py` | 双保险不暴露 |

### 4.3 为什么这么写

| 决策 | 原因 |
|------|------|
| exclusive = node 工具 − shared packs | 主人格仍需 web_search 等基建，不能一刀切空池 |
| blocked 写在 ToolContext | `find_tools` 与 RetrievableToolset 共享同一口径，避免只修一端 |
| roster 用反引号 node_id | 日志里模型自造 `agent_profile` 名导致 0 委派 |
| 输出契约只谈 report 通道 | 避免契约本身带金融词把模型「拉」出角色 |

### 4.4 简 diff（语义）

```diff
# models.ToolContext
  dynamic_tool_names: Set[str] = ...
+ blocked_tool_names: Set[str] = field(default_factory=set)

# gs_agent._execute_run_once 开头
+ _blocked = _capability_exclusive_tool_names() if interactive else set()
+ context = ToolContext(..., blocked_tool_names=_blocked)

# 静态池
+ tools = [t for t in tools if t.name not in exclusive]

# progressive
- RetrievableToolset(exclude_names=set(tool_names))
+ RetrievableToolset(exclude_names=set(tool_names) | set(context.blocked_tool_names))

# find_tools
+ loaded_names = [n for n in loaded_names if n not in ctx.deps.blocked_tool_names]
```

### 4.5 create_subagent 默认行为

```python
_TRANSIENT_DEFAULT_PROFILES = {
    "research_agent", "internal_reporter",
    "memory_curator", "scheduler_assistant",
}
# use_transient = transient or pid in _TRANSIENT_DEFAULT_PROFILES
# 文本结论类 → 默认同步 ad-hoc；code/plugin_dev 仍走 Kanban
_KANBAN_INLINE_WAIT_TIMEOUT_SEC = 60.0  # 同步等看板从 180→60
```

**为何**：看板被「查一下/列文件」刷满；查询类不必建卡。
**交接注意**：docstring 仍偏「默认 False 才可追溯」——与「profile 默认 transient」并存时，以 `_TRANSIENT_DEFAULT_PROFILES` 为准；需要产物追溯请显式 `transient=False` 或走 code/plugin 类 profile。

### 4.6 回归测试

- `tests/test_capability_delegation_flow.py`
  - exclusive ∩ task_basics = ∅
  - roster 含 `create_subagent` / `agent_profile` / 反引号
  - 契约无业务词
  - blocked / exclude 口径

---

## 5. 主题 C：意图上下文 + 省略跟进

### 5.1 目标

- 分类器**不能只喂当前句**。
- 「然后呢」在上轮真用过工具（或上文用户句本就是工具向）时 → **结构升级为工具**。
- 「是吗 / 可以吗」是确认，**不是**省略跟进。

### 5.2 关键实现

| 符号 | 文件 | 作用 |
|------|------|------|
| `collect_prior_user_turns` | `mode_classifier.py` | 同 user_id 近期原文（Protocol，直接属性） |
| `_is_ellipsis_followup` | `mode_classifier.py` | 闭类 + 极短「呢/咋样/怎样」 |
| `IntentService.predict_async(...)` | `mode_classifier.py` | prior 拼接 / SoloOverride / Structural |
| `handle_ai` 意图段 | `handle_ai.py` | prior + 扫近 6 条助手消息是否有 ToolCall |

### 5.3 为什么这么写

| 决策 | 原因 |
|------|------|
| prior 按 user_id 过滤 | 群聊别人的「然后呢」不能拼进当前用户上下文 |
| 去掉 bare `吗` | 「是吗」+ prev_tools 会误强制工具意图、过装配 |
| 扫多条 ModelResponse 找 ToolCall | 最后一条常是纯文本总结，只看 last 会丢「上轮用过工具」 |
| 工具规程：闲聊 LITE，否则全文 | 闲聊允许轻工具；真工具轮给全规程；均在 user 侧且 relean 剥离 |

### 5.4 简 diff（语义）

```diff
# mode_classifier
- return fullmatch(r".{0,4}(呢|吗|咋样|怎样)...")
+ return fullmatch(r".{0,4}(呢|咋样|怎样)...")  # 无 bare 吗

+ def collect_prior_user_turns(records: Sequence[_UserTurnRecord], ...):
+     # rec.role / rec.user_id / rec.content 直接访问

# handle_ai
+ prior = collect_prior_user_turns(history, event.user_id)
+ prev_tools = any(ToolCallPart in recent assistant msgs)
+ intent = await predict_async(query, prior_user_turns=prior, prev_turn_used_tools=prev_tools)
```

### 5.5 回归测试

- `tests/test_intent_context.py`

---

## 6. 主题 D：软触发规则预筛

### 6.1 目标

`run_reactive_gate` 先零 LLM：

- 空 / 纯语气词 / 纯标点 → **沉默**（`False`）
- 短句 + 接续特征（你/多少/怎么样/那个…）→ **放行**（`True`）
- 其余 → `None` 交给轻量模型

### 6.2 关键

`heartbeat/decision.py` → `_reactive_gate_rule_prefilter`
测试：`tests/test_reactive_gate_prefilter.py`

### 6.3 为什么 / 残留风险

- **为何**：软触发量大，全 LLM 贵且慢。
- **风险**：短句含「多少/那个」的**人与人闲聊**可能被规则放行，增加误应答。后续可加「第二人称 + 接续」或依赖 bot 上一句锚点收紧。

---

## 7. 主题 E：提示与框架去域词化

### 7.1 范围（抽样）

- `persona/prompts.py`：工具编排、聊天域隔离改为**口吻**约束，不禁止办事。
- `mode_classifier` 功能名词 / 规则：去掉股价、大盘、金价等。
- `scheduler` / 部分 capability docs：业务示例改为中性表述。
- 软触发 note **挪到**工具规程之后（近因：默认路过压过前面上下文）。

### 7.2 为什么

框架层词表绑定业务域 → 换插件场景要改核心；插件工具描述 / 检索应自己带域语义。

### 7.3 回归影响

纯「查股价」可能更依赖 ML + 插件描述，规则层不再直接打「工具」。建议插件侧保留清晰 tool description，并视需要加 eval 样例。

---

## 8. 主题 F：装配与语域

`context_assembly.assemble_dynamic_context`：

- 闲聊且无上轮工具 → `TOOL_ORCHESTRATION_LITE` + 口吻提醒（**不**禁止查/办）。
- 工具/问答/上轮用过工具/活跃任务 → 全文 `TOOL_ORCHESTRATION_CONSTRAINTS`。
- 上一轮 `metadata.sent_reports` → 「你发过资料图：…」提示。
- 软触发 note **最后**注入。

---

## 9. 主题 G：Subagent / Kanban / 其它

| 项 | 说明 |
|----|------|
| Kanban 内联等待 | 60s；超时撤销 interactive relay，执行体完成后推群 |
| 文本 profile 默认 transient | 见 §4.5 |
| statistics / dashboard | 管理端统计与健康信息扩展 |
| `ops_diagnostics_api` | 运维诊断 API（新文件） |
| persona_api / memory_api | 控制台接口小补 |
| global_val_models | 全局值模型字段补充 |
| consolidation_worker / multimodal | 记忆链路小增强 |
| `List`→`Sequence`（history 参数） | 修复 list 不变性导致的 pyright；符合「从类型根上修」 |

---

## 10. 主题 H：工程卫生（LLM.md / 类型 / 注释）

### 10.1 注释

- 生产路径 `#` 注释：**一块 ≤2 行**，去掉 `====` 分隔长文。
- ruff E501 对中文按**东亚宽度**计；过长中文注释会被标红，已压到可过检。

### 10.2 类型 / 测试

- `collect_prior_user_turns`：`Protocol` + 直接属性（禁 `getattr`）。
- history API：`Sequence[ModelMessage]`。
- tests：`isinstance` / `Any` 假 bot / TypedDict 构造；对齐 compact 新语义。
- `ruff check tests gsuid_core/ai_core eval`、关键文件 pyright 已清零（以本地环境为准）。

### 10.3 本机 pytest 注意

缺依赖时（如 `jieba` / `pydantic_ai_skills`）部分用例 import 失败——用项目 venv / `uv run pytest`。

---

## 11. 文件清单（按主题）

### 11.1 必读（核心逻辑）

| 文件 | 主题 |
|------|------|
| `gsuid_core/ai_core/utils.py` | OOC 两通道、history compact、发送拆分 |
| `gsuid_core/ai_core/gs_agent.py` | exclusive 剥离、roster、契约、blocked、装配 |
| `gsuid_core/ai_core/models.py` | `blocked_tool_names` |
| `gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py` | find_tools 过滤 |
| `gsuid_core/ai_core/classifier/mode_classifier.py` | prior / 省略跟进 / 结构升级 |
| `gsuid_core/ai_core/handle_ai.py` | 意图入口接线 |
| `gsuid_core/ai_core/context_assembly.py` | 规程分级 + 软触发近因 |
| `gsuid_core/ai_core/heartbeat/decision.py` | 软触发规则预筛 |
| `gsuid_core/ai_core/buildin_tools/subagent.py` | transient 默认 / 等待超时 |
| `gsuid_core/ai_core/persona/prompts.py` | 编排/口吻文案 |
| `docs/AI_AGENT_LIFECYCLE_SEQUENCE.md` | 单消息生命周期 |

### 11.2 测试

| 文件 | 覆盖 |
|------|------|
| `tests/test_ooc_structured_intercept.py` | 两通道 / compact |
| `tests/test_intent_context.py` | prior / 省略 |
| `tests/test_reactive_gate_prefilter.py` | 规则预筛 |
| `tests/test_capability_delegation_flow.py` | 委派不变量 |
| `tests/test_review_fixes_20260717.py` | compact 语义对齐更新 |
| `eval/agent/ooc_long_test.py` | 长会话 OOC 指标 |

### 11.3 控制台 / 周边

`webconsole/dashboard_api.py`、`ai_statistics_api.py`、`ops_diagnostics_api.py`、`persona_api.py`、docs 下 dashboard/statistics/capability/state-store 说明同步等。

---

## 12. 交接：如何继续改、如何回归

### 12.1 本地命令

```bash
# 类型与风格
ruff check tests gsuid_core/ai_core eval
pyright tests gsuid_core/ai_core/utils.py gsuid_core/ai_core/gs_agent.py

# 本批核心单测（在完整依赖环境中）
uv run pytest \
  tests/test_ooc_structured_intercept.py \
  tests/test_intent_context.py \
  tests/test_reactive_gate_prefilter.py \
  tests/test_capability_delegation_flow.py \
  -q
```

### 12.2 生产行为验收清单

1. **交互会话**加载含能力代理的插件后：主人格工具列表不应出现 exclusive 名；`find_tools` 报告加载也不应包含这些名。
2. 模型输出 ` ```任意lang ` + JSON + 尾部台词：用户侧台词在，数据出图。
3. 工具轮后用户发「然后呢」→ 意图偏工具；发「是吗」→ 不应被结构升级硬抬成工具。
4. 软触发「哈哈」→ 沉默；「你说的那个怎么样了」→ 规则放行。
5. history 中 assistant 消息无残留 fence/JSON 教坏；仅成功发送的 round 有 `sent_reports` metadata。

### 12.3 修改时的红线

| 不要 | 要 |
|------|-----|
| 用「股价/report 语言标签」判定数据 | body 形态 / 密度 |
| 只 strip 静态池、不管 find_tools | 同步 `blocked_tool_names` + exclude |
| 省略跟进再加宽到 bare「吗」 | 闭类表 + 呢/咋样/怎样 |
| 长篇 `#` 注释复述代码 | ≤2 行点明坑 |
| `getattr` / `cast` / `type: ignore` 糊类型 | Protocol / isinstance / 改签名 |
| 把 TOOL 规程写进 system 持久前缀 | user 侧注入 + relean 剥离 |

### 12.4 已知未闭环 / 建议后续

| 项 | 说明 |
|----|------|
| 软触发 PASS 规则偏宽 | 人-人短句误放行风险，见 §6.3 |
| 域词从表移除后的意图 | 依赖插件描述 + ML；可补 eval |
| create_subagent docstring vs 默认 transient | 建议对齐文档与 `_TRANSIENT_DEFAULT_PROFILES` |
| 60s Kanban 等待 | 长 code 任务更多「仍在执行 + 事后推群」 |
| 高密度「代码里的 dict 字面量」 | 闭合 fence 非 data 时靠「体内有 ```」防 open 误切；裸 JSON 提取仍可能碰到代码中的 dict——有测试护栏，改密度阈值时注意 |
| `context_assembly` 规程注入 `except Exception` | 失败仅 debug，接线错误可能静默无规程 |

---

## 13. 评审中已修的关键 bug（便于 git blame）

| Bug | 修复要点 |
|-----|----------|
| find_tools 回灌 exclusive | `blocked_tool_names` + find_tools 过滤 + Retrievable exclude |
| 未闭合 fence 吞台词 | open fence 只切数据前缀；体内已有 ``` 不走 open |
| 「是吗」结构升级 | 去掉 bare `吗` |
| compact 测试期望「已发资料图」正文 | 改为 metadata + 全量抹结构 |
| 长注释 / pyright 红海 | 注释折叠 + Sequence/isinstance 等 |

---

## 14. 与生命周期文档的对应关系

单条消息阶段（见 `AI_AGENT_LIFECYCLE_SEQUENCE.md`）：

| 阶段 | 本批触点 |
|------|----------|
| ⑥ 意图分类 | prior + ToolCall 扫描 + 省略结构升级 |
| ⑦ 软触发沉默门 | 规则预筛 |
| ⑧ 装配 | LITE/全文规程、roster、口吻、软触发近因 |
| ⑨ Agent.run | exclusive 剥离、blocked、契约、两通道发送 |
| ⑩ 收尾 history | compact 抹结构 + sent_reports |

改 handler / handle_ai / gs_agent / assembly / 两通道逻辑后：**请同步生命周期文档与本文 §12 验收清单**。

---

## 15. 交接签名栏（人工填写）

| 项 | 内容 |
|----|------|
| 作者 / 会话 | （填写） |
| 基线 commit | `git log -1`（提交前） |
| 是否已跑通 uv pytest 核心四文件 | ☐ |
| 是否已在真实群验证委派 + OOC | ☐ |
| 遗留 issue 跟踪 | （链接） |

---

*本文描述 2026-07-24 工作区行为；合并后若有冲突，以源码与单测为唯一真相。*
