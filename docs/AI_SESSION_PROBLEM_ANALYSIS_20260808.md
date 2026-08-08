# GsCore AI 实测问题分析报告（2026-08-08）

> **读者**：继续改 `ai_core` 的开发者 / Agent
> **输入**：
> - 目标基线：`plans/clear type.md`（旧目标，多数仍有效）
> - 代码风格：`docs/LLM.md`
> - 本批改动：`agent_run` 拆分 + prompts / research / web_search / 配置微调
> - 实测日志：`data/ai_core/session_logs/web_web_web-client-001_private_user_web_01_4a97e94e_20260808_220805.json`
> - 关联子代理：`subagents/capagent_research_agent_…76cf8b07…`、`capagent_render_agent_…a498fa…a0caedd5…`
> **原则**：归因走流程与提示词工程；**禁止**用业务域关键词/正则特判当主修复。

---

## 0. 一句话结论

本批工作区改动里，**工程拆分（`agent_run`）基本达成**、**research → render 主路径在「厄尔尼诺」重任务上能跑通**；但相对 `clear type.md` 的产品目标，**群友感 / 沉默纪律 / 工具召回 / token&cache / 出图质量稳定性仍大面积未达标**。
用户点名的 OOC 句「`zzz…没啥好画的…数据也没刷出来…别折腾我…`」不是偶发口癖，而是 **「结构出图 nudge 误触发 × 框架注入轮没有沉默协议 × 懒人格把抱怨当台词」** 的交汇；应输出 `<SILENCE>` 的轮被当成了可对 C 端说话的用户轮。

---

## 1. 目标对照：`clear type.md` 达成度

| # | 目标（摘自 clear type） | 本批/实测状态 | 说明 |
|---|------------------------|---------------|------|
| G1 | 长数据/报告 → 主动出图，不念表 | **部分** | 厄尔尼诺：research 事实包 + render 出图并 `send_message_by_ai` 成功；广州气温：失败后念口头推测、误触发出图 nudge |
| G2 | 禁业务词/正则特判 | **大体遵守** | 新改动以结构信号（FileOS/表/多段）+ 委派契约为主；但「结构」信号过粗导致误触发（见 P1） |
| G3 | 去掉 `<report>`，改工具委派 | **已做** | system 明确禁 `<report>`，走 `create_subagent` |
| G4 | HTML 出图像信息图，不是记事本 | **部分改善** | render_agent 用了分区 KPI/暗色 token；质量依赖模型单次 HTML，仍无版式评分/迭代环 |
| G5 | system_prompt 会话内永不改 | **达成** | 主 session 仅 1 次 `system_prompt`；动态信息在 user 侧 |
| G6 | 人格 + 重要工具规范进 system | **达成** | `SYSTEM_CONSTRAINTS` + `TOOL_ORCHESTRATION` + 人设 |
| G7 | 动态上下文减噪、高置信优先、需要再查 | **未达标** | 每轮仍塞长期记忆片段、相关对话、口吻/身份锚、Kanban 摘要；工具池 22→40 膨胀 |
| G8 | 工具池不宜每轮三四十个 | **未达标** | 寒暄 22；有 Kanban 时 40（含 artifact/kanban 全家桶 + 若干插件工具） |
| G9 | 工具搜索能找到对的工具 | **未达标** | `find_tools("查询广州实时天气和气温")` → 异环 NTE 账号/角色/体力 等完全无关工具 |
| G10 | 群聊能 `<SILENCE>`、不全插嘴 | **本 session 未测群** | 私聊测到：框架内部轮/收尾轮仍对用户吐槽或发 `zzz…`，沉默协议不完整 |
| G11 | 角色有意志，不全盘照做 | **部分** | 懒口癖保留；但「正当请求必须履约」与懒人格冲突时，常变成「嘴上抱怨仍办事」或「抱怨代替沉默」 |
| G12 | 工具返回不污染口癖/history | **未达标** | 中间态台词声称「详情让 render 出了个图」时图尚未出；nudge 轮把校验文案当对白题材 |
| G13 | token 省、cache ≥70% | **未达标** | 寒暄轮 cache_read 114 / input 10725 ≈ **1%**；重任务轮因历史复用升高，但 system 稳定收益未体现为首轮高 cache |
| G14 | web 用可靠检索；价量优先专域 API | **文案已加强** | research/web_search docstring 已写「摘要≠现价」；天气仍无专域工具可用时 thrash web |

**总评**：本批更像「主路径可跑通的骨架 + 工程可读性重构」，**不是** clear type 产品目标的闭环验收。

---

## 2. 代码变更 Review

### 2.1 本批实际改了什么

| 区域 | 性质 | 预期效果 |
|------|------|----------|
| `gsuid_core/ai_core/agent_run/*` | **结构重构** | 拆 `_execute_run_once` 为 prepare/tools/loop/settle；语义宣称等价 |
| `gs_agent.py` 大幅瘦身 | 配套 | 重试/闸门收尾/工厂仍留主类 |
| `persona/prompts.py` | 行为 | DELEGATION_FIRST、重任务禁长正文、身份锁、懒≠拒做 |
| `capability_agents/profiles.py` | research | 专域 API 优先、web 禁当现价、artifact 必交 res_ |
| `web_search.py` | 工具文案 | 摘要数字降权 |
| `subagent.py` | 交付完整性 | `res_` / artifact 登记不算 incomplete |
| `ai_config.py` | 呈现层 | `render_long_markdown_as_image` 默认关，避免丑 markdown 图双发 |
| `context_assembly.py` | user 侧锚 | 每轮「身份：你是某某」防绰号污染 |
| 文档/测试 | 配套 | lifecycle、`test_session_fix_20260808` 等 |

### 2.2 是否达成「本批自述目标」

`docs/AI_AGENT_RUN_REFACTOR_20260808.md` 的**非目标**写得很清楚：不改工具五层、不改 pre_send_gate 顺序、不改假完成/出图 nudge 语义。
因此：

- **拆分目标**：基本达成（模块边界清晰，行为等价意图明确）。
- **clear type 产品目标**：本批**本来就没完整覆盖**；提示词微调是「加厚契约」，不是端到端修复。
- **风险**：把「出图 nudge / 假完成 / FileOS 折叠」原样搬进 `settle`/`loop` 时，**原有误触发逻辑被固化**，日志已打脸。

### 2.3 是否可能引入新 bug

| 风险 | 严重度 | 说明 |
|------|--------|------|
| FileOS 折叠 ⇒ 一律 `saw_structured_return=True` | **高** | 任意大体积 tool_return 都会武装 render nudge，与是否「可出图事实」无关（本 session 天气轮实证） |
| 墙钟软预算 vs research+render 两段委派 | **高** | research 返回后注入「不要再调工具」；模型先口头总结，再冒险调 render，或放弃出图 |
| 异步 render 与用户插话并发 | **中高** | 图未回灌时用户问新问题；旧任务完成注入与新问题交织，台词顺序混乱 |
| 纠正重跑 `fake_done_retry=True` 的 user 注入 | **中** | 注入文案像「系统校验请出图」，模型对 C 端抱怨「别折腾我」——**框架轮缺少沉默默认** |
| research 工具池混入游戏插件工具 | **中** | research log 可见 `send_waves_abyss_info` 等，干扰注意力与 token |
| `find_tools` 语义召回错域 | **高（旧问题）** | 天气 → NTE；非本批引入，但本批未治 |
| 中间 TextPart 在 tool 前发送 | **中** | 「图出了」假完成台词可在 create_subagent(render) 之前发出 |
| LLM.md 红线 | **低~中** | `agent_run` 无 `cast`/`type: ignore`/`getattr`；但 `settle`/`loop` 保留大量 `try/except` 兜底发送/纠正路径（多由旧代码迁入） |

### 2.4 LLM.md 风格合规

| 红线 | agent_run 现状 |
|------|----------------|
| 禁止 try-except 吞类型问题 | 发送失败、FileOS、假完成纠正等仍 broad `except Exception`——**运维兜底风格，与红线张力仍在** |
| 禁止 cast / type: ignore | 本包未见 |
| 禁止 getattr / 乱 dict.get | 本包基本直读；`extra` 用 `in` 再下标（符合文档意图） |
| 完全类型注解 | `RunOnceState` / `RunOnceHost` 方向正确 |
| 注释 ≤2 行 88 字 | 大体遵守；少量阶段说明略长但仍克制 |

**结论**：拆分**方向符合** LLM.md 的类型化与分层；**并未**借重构把「异常兜底文化」清干净。行为修复不应再堆 try/regex，而应把契约变成**状态机可判定**的阶段。

---

## 3. 实测 Session 时间线（主 log）

会话：`web…4a97e94e`，人格「早柚」，私聊主人 `user_web_01`，模型 `MiniMax-M3`。

| 轮 | 用户 | 工具 | 对用户可见结果 | 问题标签 |
|----|------|------|----------------|----------|
| R1 | sayu早上好 | 无 | 「唔…晚上好…都几点了还早上好…zzz」 | 人设 OK；cache ~1% |
| R2 | 你知道什么是厄尔尼诺吗 | 无 | 常识短答 +「跟我没关系」 | 未检索也可接受；偏懒 |
| R3 | 详细总结下半年影响 | research →（墙钟）→ 口头摘要 → render 异步 → **正确 `<SILENCE>`** | 口头摘要已像「表念了一半」；且称「详情让 render 出了个图」时图未出 | **P2 墙钟** **P3 假完成台词** |
| R4 | 广州最近气温 | find_tools 错域 → web thrash → 失败 | 「卷轴翻不到」+ **编造**「三十三四度」 | **P4 召回** **P5 零编造破口** **P6 thrash** |
| R4b | （框架 render nudge） | 无 | **`zzz…没啥好画的…别折腾我…`** | **P1 误 nugde** **P0 框架轮应沉默** |
| R5 | （任务完成回灌） | `send_message_by_ai` 发图 | 「画好了…」 | 收尾 OK |
| R5b | （同轮/后置） | 无 | **`zzz…`** | **P0 收尾后应 SILENCE** |

### 3.1 Token / Cache 快照

| 阶段 | input | cache_read | cache 占比 | output |
|------|------:|-----------:|-----------:|-------:|
| R1 寒暄 | 10725 | 114 | ~1% | 225 |
| R2 常识 | 11506 | 128 | ~1% | 155 |
| R3 委派 | 39203 | 35584 | ~91% | 1282 |
| R4 天气 thrash | 163670 | 141952 | ~87% | 866 |
| R4b nudge | 14664 | 128 | ~1% | 262 |
| R5 发图 | 29461 | 24832 | ~84% | 334 |
| research 子代理 | 87685 | 52982 | ~60% | 5810 |
| render 子代理 | 41375 | 23364 | ~56% | 5830 |

观察：

1. **前两轮几乎无 cache**：system 理应稳定，却未形成高 cache_read → 要么 provider/cache 前缀策略未生效，要么请求组装每轮改变了前缀（工具 schema 列表每轮变长是元凶候选）。
2. **R4 input 16 万**：多轮 web 折叠句柄 + Kanban 工具全家桶 + 历史，典型「工具污染上下文」。
3. 子代理 research 一次 **9 次 web + artifact**，成本高但交付质量尚可；主人格却因墙钟/插话没能干净交付。

### 3.2 用户点名 OOC 的直接因果链

```text
R4 web_* 返回体较大
  → FileOS persist_and_fold 成功
  → loop.py: st.saw_structured_return = True   # 只要折叠成功就 True
  → 主人格用短句结束（未 render）
  → settle: saw_structured_return ∧ tool_call_list ∧ ¬delegated_render
  → 注入 _RENDER_DELEGATE_NUDGE 作为「用户发言」再跑一轮
  → 模型：没有可画事实 + 懒人格 → 对用户抱怨
  → 期望：框架内部纠正轮默认 <SILENCE> 或仅允许 tool，不允许对白
```

对应源码（现行）：

- `agent_run/loop.py`：FileOS 折叠成功即 `saw_structured_return = True`
- `agent_run/support.py`：`_RENDER_DELEGATE_NUDGE` 只说「请出图」，**不提供**「无可画材料则只输出 `<SILENCE>`」
- `agent_run/settle.py`：纠正结果 `result_msg = _rc`，若模型回抱怨句，**原样成为对用户出口**

这不是「早柚人设太懒」单因，而是 **流程把内部校验暴露成了用户可见对话**。

---

## 4. 问题清单（含归因）

### P0 — 框架注入轮 / 收尾轮对 C 端说话（含用户点名句）

**现象**

- 出图 nudge 后：`zzz…没啥好画的…数据也没刷出来…别折腾我…`
- 发图工具成功后仍输出：`zzz…`
- 异步 render ack 轮曾正确 `<SILENCE>`（说明模型**会**遵守硬门，但门文案不一致）

**归因**

1. **通道语义缺失**：`create_subagent` 异步回执把「必须 SILENCE」写进 tool_return；而 settle 的纠正注入 / 任务完成注入后的「多余一轮模型输出」没有同等硬门。
2. **人设优先级冲突**：system「懒/困」与「框架校验请做事」冲突时，懒人格选择**抱怨式对白**，而不是协议标签。
3. **发送路径**：TextPart 只要不在 `SILENCE_MARKERS` 就 `send_chat_result`；没有「本轮 origin=framework_corrective → 仅允许 SILENCE 或 tool」的状态。

**非解法**：匹配「别折腾我」关键词删句。

---

### P1 — `saw_structured_return` 过粗 → 假出图 nudge

**现象**：天气查询失败、结果无可用表数据，仍触发 render nudge。

**归因**

- FileOS 折叠 = 体积策略，不等于「可渲染事实包」。
- search 结果摘要卡也带 `long_structured=true` 提示文案，强化模型/闸门「这是长结构」。
- nudge 条件不检查：是否有 `res_` 事实包、是否主任务已 `delegated_render` 在途、结果是否失败/空。

**结构应改为**：「可出图候选」= **已完成的、带可消费 payload 的交付**（事实包句柄 / 多行结构化成功体），而不是「任意大 tool_return」。

---

### P2 — 墙钟软预算与多段委派管线冲突

**现象**：research 刚返回，thinking 读到「超时、不要再调工具」→ 先念摘要；随后仍调 render（契约分裂）。

**归因**

- `_WALL_CLOCK_NUDGE` 语义是**单轮收敛**，未区分「已拿到子代理事实包、下一步必须 render」的管线态。
- 墙钟按墙钟秒数一刀切，**不按 pipeline stage**（gather → package → render → deliver）。

**结果**：要么假完成口头表，要么违约再调工具，history 两边不干净。

---

### P3 — 中间台词假完成（「图已经出了」）

**现象**：R3 在 `create_subagent(render_agent)` **之前**输出「详情让 render 出了个图」。

**归因**

- 主人格被要求「只丢图+一句」；在只有 research 摘要、render 未完成时，模型用**完成态台词**填空。
- `suppress_intermediate_text` 只压制「本 turn 已见 tool_call 后的碎碎念」，**不压制 tool 前的虚假终局台词**。
- 假完成闸 `_claims_fake_done` 偏「提醒/任务设置」动词，**不覆盖「出图/画好了」类交付完成**（且用户要求勿用词表特判——应走**结构：是否已有 image 句柄/是否已 send**）。

---

### P4 — `find_tools` 召回错域

**现象**：天气 need → NTE 游戏工具族。

**归因**

- domain 聚合检索：语义噪声/索引质量/域标签不准时，「整族纳入」会放大错误。
- 返回「已加载」给了模型错误自信；无「相关度阈值以下视为未命中」。
- clear type 场景（模拟盘、东山、深渊）同源风险。

---

### P5 — 零编造破口（无工具支撑的数值）

**现象**：工具失败后仍报「三十三四度」。

**归因**

- system 有「零编造」与「卷轴翻不到」，但同条允许角色常识；模型把**气候常识**包装成**当前读数**。
- POST_TOOL 失败契约存在，但多轮 thrash 后最终回复未强制「仅缺口、无数字」。

---

### P6 — 搜索 thrash + 不读 handle

**现象**：多次 `web_search`/`web_fetch`，返回已是 persisted handle，模型较少 `read_handle` 深读，继续换 query。

**归因**

- 折叠卡提示了 `read_handle`，但主路径 POST_TOOL 契约更强调出图，不强调「先读卡再决定」。
- thrash 阈值对 web_search 偏松（并行多 query 只计 1 轮 × 阈值 4），天气轮可连打多次。

---

### P7 — 工具池膨胀与 cache 前缀不稳

**现象**：工具数 22→40；前两轮 cache ≈0。

**归因**

- Kanban 进行中注入大量 plan/artifact 工具（合理需求与 token 成本未分层）。
- 每轮 tools schema 进入请求前缀 → **破坏 prompt cache**（比「system 永不改」更致命：system 不变但前缀仍变）。
- 动态 user 侧记忆块每轮变，次要。

---

### P8 — 能力代理工具污染

**现象**：research_agent tools_list 含游戏发送类工具。

**归因**：能力节点工具装配边界过宽（检索域/插件挂载），与「专职 research」人设矛盾。

---

### P9 — 用户插话 vs 后台 Kanban 交付竞态

**现象**：render 后台跑时用户问广州气温；完成注入夹在两轮之间；体验上「图晚到 + 中间一堆抱怨」。

**归因**：交互模型是单 session 串行 `run`，但 Kanban 完成是 **proactive injection**；缺少「交付挂起时：新用户消息优先，完成包排队且默认 SILENCE 直至可发图」的调度语义（或合并为一次收尾）。

---

### P10 — 出图质量仍无闭环

**现象**：render 一次 HTML 成型，无「丑图重做」评审；clear type「像记事本」风险仍在（本 log 的 HTML 看起来更结构化，但仍是单次碰运气）。

**归因**：只有 skill 文案，没有 render 质量自检（对比度/分区/信息密度）或二次 refine 预算。

---

## 5. 解决方案（流程 + 提示词工程；框架通用）

> 下列方案**刻意不写**「若含某某词则…」。一律用：**run 阶段、交付形态、通道、是否有可消费 artifact** 等结构信号。

### 5.1 引入 Run Phase 状态机（核心）

在 `RunOnceState`（或 session 级 pipeline）显式阶段，而不是靠模型自觉：

```text
IDLE
  → GATHER        # 检索/专域/子代理取数
  → PACKAGE       # 事实包/artifact 就绪（有 res_ 或结构化成功体）
  → RENDER        # 已 create_subagent(render) 或等价
  → DELIVER       # send_message_by_ai(image) / 短台词
  → QUIET         # 只允许 <SILENCE>
```

**规则（框架执行，不靠词表）**：

1. `GATHER` 未到 `PACKAGE`：禁止终局完成态台词（见 5.3）。
2. 已 `PACKAGE` 且「多项结构」：必须进入 `RENDER` 或明确 `PACKAGE_LIGHT`（单点结论）后 `DELIVER`。
3. 收到异步 ack（后台执行中）：强制下一模型输出 ∈ `{SILENCE}` ∪ 空；**拦截**其它 TextPart 发送。
4. 框架注入（nudge / 任务完成 / 墙钟）：带 `origin=framework`；模型若无新 tool，默认只接受 `SILENCE` 或极短 DELIVER 模板。

这比在 prompt 里反复写「不要说还在写」更稳，因为 **发送闸按 phase 丢弃对白**。

---

### 5.2 重写「可出图」判据（修 P1）

**删除**：`if _folded is not None: saw_structured_return = True`。

**改为结构评分**（示例逻辑，域无关）：

```text
mark_render_candidate only if:
  (A) tool_return 含可消费句柄 res_* 且 kind ∈ {text/markdown, application/json, table-like}
  OR (B) create_subagent 完成体（非 ⏳、非 仍在执行）且正文通过「事实包形态」：
        多段落/表/列表密度 + 长度，且非错误前缀
  OR (C) 主人格主动 artifact_get 得到长文
never mark if:
  tool_return 失败 / 空 / 仅导航壳 / 仅「已加载工具列表」
  OR 已存在 in-flight render task（delegated_render 或 kanban 运行中）
```

`_RENDER_DELEGATE_NUDGE` 改为**双分支契约**（提示词工程）：

```text
（系统校验·内部轮：不对用户聊天）
若本轮已有可消费的长事实包且未出图 → 只调 create_subagent(render_agent, task=句柄或包)
若无可消费事实包 / 工具失败 / 仅单点结论 → 只输出 <SILENCE>
禁止向用户解释本校验、禁止抱怨、禁止重述数据
```

纠正跑 `fake_done_retry` 时：`suppress_intermediate_text=True`，且 **TextPart 非 SILENCE 则 pre_send 丢弃**（框架）。

---

### 5.3 终局台词闸（修 P3，结构而非词表）

在 `pre_send_gate` 旁增加 **delivery_state_gate**（通用）：

| 条件 | 动作 |
|------|------|
| 本 run 已 `create_subagent` 且最近 return 为「后台执行/回灌中」 | 只允许 SILENCE |
| 本 run 有 image `res_` 尚未 `send_message_by_ai` | 禁止纯文本终局（可允许「极短」仅在 send 同响应） |
| 本 run 宣称式结束且 `tool_call_list` 空且 intent 非寒暄 | 既有假完成路径 |
| 本 run 已成功 send 图/文 | 后续 TextPart → SILENCE 化（丢弃或改写为不发送） |

「宣称式结束」用**结构**：同 response 内是否仍有未决 ToolCall、是否存在 in-flight task，而不是扫「画好了」词。

---

### 5.4 墙钟与 pipeline 对齐（修 P2）

修改 `_WALL_CLOCK_NUDGE` 为**分阶段文案**（仍是提示词，但由框架选模板）：

| 当前 phase | 墙钟文案要点 |
|------------|--------------|
| GATHER 且已有部分结果 | 停止新检索；整理已有缺口；必要时一次委派 render |
| PACKAGE 已有 res_ 未 render | **必须** render 或交付缺口；禁止「不要再调工具」一刀切 |
| RENDER in-flight | 只 SILENCE |
| DELIVER 后 | 只 SILENCE |

墙钟触发时设置 `st.pipeline_force = "close_or_render"`，loop 层允许白名单工具：`create_subagent(render_*)` / `send_message_by_ai` / `artifact_get`，剥离其它 tool_call。

---

### 5.5 框架注入统一「内部轮协议」（修 P0）

所有下列入口共用同一协议对象 `FrameworkTurnPolicy`：

- 假完成 nudge / 结构零工具 nudge / render nudge
- Kanban 完成回灌
- 墙钟
- thrash fuse

协议字段：

```text
allow_user_visible_text: bool
allow_tools: frozenset[str] | "all" | "none"
default_if_empty: SILENCE
strip_non_silence_text: bool
```

**任务完成回灌**：`allow_user_visible_text=True` 但仅在成功调用发送工具后的**一句**角色台词；发送完成后 phase→QUIET，再多说的 `zzz…` 直接不发。

异步 subagent 的 SILENCE 硬门已在 tool_return 验证有效——把同等强度提升为 **policy 对象**，避免只写在某一个字符串里。

---

### 5.6 提示词工程：人设 vs 履约 vs 沉默（修 G10/G11/P0）

在 **system 稳定区**（符合 clear type 规则 1）增加一小节「协议通道」，所有人格共享，不进角色卡口癖：

```text
## 协议通道（非台词）
- <SILENCE>：本轮不对用户产生任何可见输出；懒/困/烦 cooldowns 也必须用它，
  不得用抱怨句代替沉默。
- 框架注入/校验/回灌文案：不是群友发言；能做事就只调工具，不能做事就 <SILENCE>，
  禁止评价框架、禁止「别折腾」。
- 人设只约束「说话时的口气」；不约束「该不该 SILENCE、该不该调工具」。
```

角色卡（早柚）**删除或改写**易诱发「用抱怨代替沉默」的示例方向：

- 保留：履约后的懒尾巴（「查到了…好困」）——**仅当本轮确有交付**
- 禁止示例：无交付时的「别烦我/没啥好画的」→ 改为「无话可说 → `<SILENCE>`」

**Presence 表**写清：

| 情境 | 行为 |
|------|------|
| 未被点名/内部轮/已交付后多余输出 | `<SILENCE>` |
| 被点名且正当请求 | 履约；口气可懒 |
| 工具失败 | 一句角色化缺口 **或** 私聊可一句；群聊可 SILENCE（按产品选默认） |
| 后台任务等待 | `<SILENCE>` |

---

### 5.7 工具暴露与 cache（修 P7/G8/G13）

**分层工具前缀（框架）**：

1. **L0 常驻（进 cache 前缀）**：极少 meta + 委派 + 记忆查询 + 搜索入口（个位数～十以内）
2. **L1 情境**：仅当 `has_active_task` / 省略跟进 / 明确 intent 才挂 Kanban/artifact 全家桶
3. **L2 动态**：`find_tools` 命中后**本 run 有效**，不写进下一轮静态 schema 除非仍需要

目标：让「system + L0 tools」成为稳定 cache 前缀；L1/L2 放在 messages 尾部或 toolset 动态层（视 pydantic-ai 能力），避免每轮重写全部 function schema。

**度量**：session log 增加 `cache_ratio`、`tool_count`、`phase` 字段，验收 cache≥70% 看**稳态多轮**，不只看单次高峰。

---

### 5.8 `find_tools` 召回治理（修 P4，非关键词特判）

1. **相关度阈值**：embedding/rerank 低于阈值 → 返回「未找到」，禁止乱加载。
2. **负反馈**：同一 need 家族加载后连续 Unknown/失败 → 域降权。
3. **返回形态**：区分 `loaded` vs `suggested_agents`；错域时优先给 `create_subagent` 节点而不是插件碎片。
4. **索引侧**：工具 description 必须含能力动词与对象类型（「实时气象读数」），避免与游戏「体力/探索」向量黏连——这是**元数据质量**，不是 runtime 特判。
5. research/capability **白名单 tool packs**：节点 profile 只挂检索/知识/artifact，不挂聊天发送类游戏工具（修 P8）。

---

### 5.9 检索 thrash 与 handle 读（修 P6）

POST_TOOL 契约（主人格）改为阶段式：

```text
若返回为 persisted handle 卡：
  1) 需要细节 → read_handle
  2) 已是多项结构且任务要展示 → render_agent(task=handle)
  3) 失败/空 → 停止同工具连打，角色化缺口或 SILENCE
禁止在未 read 的情况下再次 web_search 同意图
```

框架：`same_tool_streak` 对「仅 searchish 且无 read_handle/无新 agent」更早 fuse；fuse 后 strip 该工具。

---

### 5.10 零编造闭环（修 P5）

失败契约加强为：

```text
无本轮成功工具字段支撑的具体数值 → 不得出现在用户可见文本
可：承认没翻到；可：邀请用户稍后再问
不可：用训练常识冒充「今天/最近」读数
```

框架辅助（结构）：若本 run 全部 searchish 失败/空，且 TextPart 匹配「数字+单位」密度高 → **REWRITE 要求去数** 或走 lightweight 重写（已有 OOC rewrite 管道可复用），依据是**数字密度+失败态**，不是天气词表。

---

### 5.11 交付调度（修 P9）

- 用户新消息到达时：若仅有 in-flight render，**先答新消息**；完成包进入 `pending_delivery` 队列。
- 合并策略：若 pending 含 image 句柄，在**下一空闲** proactive 轮只做 DELIVER+一句，然后 QUIET。
- 禁止在用户新问题的 run 中途把旧任务的「抱怨 nudge」插进同一可见时间线（本 log R4b 就是污染）。

---

### 5.12 Render 质量环（修 G4/P10）

流程（仍通用）：

1. render_agent 出图后自检清单进 system（对比度、分区、关键数字是否进 KPI、是否整页纯 `<p>`）。
2. 可选第二步：`refine` 仅当自检失败且预算允许。
3. 主人格只收句柄，不读 HTML。
4. 评测：固定「多维事实包」金标，看图 OCR/人工表，不测域名词。

---

## 6. 建议实施顺序（完整但可迭代）

| 优先级 | 项 | 预期收益 | 依赖 |
|--------|----|----------|------|
| **P0** | FrameworkTurnPolicy + 内部轮强制 SILENCE/只工具 | 立刻消灭「别折腾我」类 C 端 OOC | settle/loop/handle 注入点 |
| **P0** | 修 `saw_structured_return` 判据 + nudge 双分支 | 消灭假出图纠正轮 | loop/settle/support |
| **P1** | 墙钟分 phase + 白名单工具 | research→render 一次说清 | prepare/loop + state.phase |
| **P1** | 终局台词闸 / 发图后 QUIET | 假「图出了」、多余 zzz | loop pre_send |
| **P2** | 工具分层 + cache 前缀稳定 | token/cache | tools.py 装配 |
| **P2** | find_tools 阈值 + capability 白名单 | 召回与 research 纯净 | discovery + profiles packs |
| **P3** | 检索 thrash/handle 契约 | 少 16 万 token 空转 | POST_TOOL + thrash |
| **P3** | 失败零数字 rewrite | 防瞎报气温 | output_gate |
| **P4** | pending_delivery 调度 | 插话不搅交付 | handle_ai / kanban |
| **P4** | render 自检环 | 图观感 | render profile |

每步验收：**禁**加业务特判测试；用结构 fixture（假长 tool_return、异步 ack、无 res_ 的 nudge、发图后多余 TextPart）。

---

## 7. 与「本批改动」的关系说明

- **不要回滚 `agent_run` 拆分**：它让上述 phase/闸门改动终于有落点；问题多在**迁入的旧语义**，不在分包本身。
- **prompts 加厚 DELEGATION_FIRST 有效**（R3 确实委派了 research/render），但**没有**解决内部轮沉默与结构误判。
- **research 质量**在本 log 中相对最好；短板在主人格编排与框架纠正。
- **clear type 仍未完成的核心**：像群友、会沉默、工具准、token 省、图好看且稳定——需要第 5 节的状态机与装配，而不是继续堆「再写长一点的 system」。

---

## 8. 附录：关键代码锚点

| 主题 | 位置 |
|------|------|
| FileOS 折叠误标 structured | `agent_run/loop.py`（`saw_structured_return = True` on fold） |
| render nudge | `agent_run/settle.py` + `support._RENDER_DELEGATE_NUDGE` |
| 墙钟文案 | `support._WALL_CLOCK_NUDGE` |
| SILENCE 发送跳过 | `loop.py` TextPart + `SILENCE_MARKERS` |
| 异步 SILENCE 硬门 | `buildin_tools/subagent.py` 超时回执字符串 |
| 工具装配 | `agent_run/tools.py` |
| find_tools | `buildin_tools/dynamic_tool_discovery.py` |
| 人设/宪法 | `persona/prompts.py` |
| 身份 user 锚 | `context_assembly.py` |

---

*本报告基于 2026-08-08 工作区与指定 session log；若随后改 phase/闸门，请同步更新生命周期文档 §10 与本报告状态。*
