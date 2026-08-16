# §13 Agent 环 Hook 总线与一等套件

> 落地日期：**2026-08-15** · 源码：`gsuid_core/ai_core/hooks/`、`gsuid_core/ai_core/kits/`
> 交接文档：[`AI_CORE_THREE_LINE_REFACTOR_20260815.md`](../../../AI_CORE_THREE_LINE_REFACTOR_20260815.md)
> 生命周期锚点：[`AI_AGENT_LIFECYCLE_SEQUENCE.md`](../../../AI_AGENT_LIFECYCLE_SEQUENCE.md) §1.1

---

## 13.1 一句话

> hook 是插座，套件是插头。**默认能力也是插头**，不再焊在插线板上。

`handle_ai` / `agent_run` 现在是**薄编排器**：只做闸门、Session、`Agent.iter`、输出闸入口、
history 红线，以及在固定锚点 `fire_hooks`。记忆、关系温度、情绪、意图分类、软门、脚手架
注入、工具装配等**产品能力**全是套件，挂在同一张总线上。

换掉整套记忆不必改 `handle_ai.py`：声明 `slot="memory"`，把 `kit_slots.memory` 指向你的
`kit_id` 即可。

---

## 13.2 31 个点位

编号 `Hxx` 是**稳定 ID**：删点只标 deprecated，**不复用编号**。
每个点位在 `hooks/points.py::HOOK_POINT_SPECS` 里声明内核锚点、能力票、默认超时。

| ID | 枚举 | 时机（内核锚点） | 默认套件 |
|----|------|------------------|----------|
| H00 | `ON_INBOUND` | `handler.handle_event`，Event 建完、**不挡**命令/AI 分流 | `gscore.memory` 观察、`gscore.meme` |
| H01 | `BEFORE_AI_CHAT` | 预算通过之后、session 之前 | `gscore.session_mute` |
| H02 | `AFTER_SESSION` | `get_ai_session` 之后 | `gscore.favorability`（读 View）、`gscore.memory`（主动会话 observe） |
| H03 | `CLASSIFY` | 内核只给 prior；**intent 由占用者写入** | `gscore.classifier`、`gscore.statistics` |
| H04 | `REACTIVE_GATE` | 仅 `soft_triggered` | `gscore.reactive_gate` |
| H05 | `RETRIEVE_CONTEXT` | compose 之前的「贵检索」窗 | `gscore.memory`（**唯一 15s 长超时**） |
| H06 | `COMPOSE_CONTEXT` | 合成器开火，各套件 `set_context_block` | mood / favorability / memory / planning / scaffold / self_cognition / identity / reactive_gate |
| H07 | `AFTER_CONTEXT` | 合成完毕、`session.run` 前 | 第三方 `append_user_hint` → `plugin_hints` |
| H08 | `AFTER_RUN` | 结算之后 | `gscore.mood` 更新、`gscore.favorability` 上报 |
| H09 | `ON_AI_ERROR` | 外层 except | 统计 / 告警 |
| H10 | `BEFORE_RUN` | `RunOnceState` 构造后、预算前 | — |
| H11 | `AFTER_BUDGET` | 预算放行后 | — |
| H12 | `AFTER_INIT` | `ToolContext` 已建 | 套件写 `plugin.{kit}.` extra |
| H13 | `AFTER_PREPARE_USER` | user 外壳拼完 | `gscore.scaffold` hints |
| H14 | `ASSEMBLE_TOOLS` | `addr_gated` 已判定 | **仅** `slot=tool_assembly` 占用者 |
| H15 | `AFTER_ASSEMBLE_TOOLS` | 默认套件跑完、exclusive **之前** | 第三方 `ensure_tools` / `drop_tools` |
| H15b | `AFTER_BUILD_AGENT` | `Agent(...)` 之后 | — |
| H16 | `BEFORE_MODEL_REQUEST` | 墙钟/闸门 feedback/thrash 注入之后、stream 前 | `gscore.post_tool` |
| H17 | `ON_TOOL_RETURN` | 单条 ToolReturn | `gscore.fileos` |
| H18 | `ON_TOOL_CALL` | 尚未执行工具体 | `gscore.memory` 工具轨迹；第三方 veto |
| H19 | `ON_THINKING` | ThinkingPart | — |
| H20 | `BEFORE_TEXT_GATE` | `speech_policy` **之后**、`pre_send_gate` **之前** | 第三方改写 |
| H21 | `AFTER_TEXT_GATE` | gate 返回后 | `gscore.speech`（密封，只观测） |
| H22 | `AFTER_SEND` | `send_chat_result` 成功后 | 统计 |
| H23 | `BEFORE_SETTLE` | history + `_relean` + 闸门收尾之后、纠正之前 | `gscore.quality` |
| H24 | `BEFORE_CORRECTION` | 即将嵌套 `_execute_run_once` | `gscore.quality` |
| H25 | `ON_USAGE_LIMIT` | 强制总结前 | — |
| H26 | `ON_CANCEL` | supersede | — |
| H27 | `ON_RUN_ERROR` | 重试耗尽 | — |
| H28 | `AFTER_CLEANUP` | finally 之后 | 套件释放 extra |
| H29 | `ON_STABLE_CONTEXT` | `build_session_system_prompt` 内，**仅新建 / persona 热更** | `gscore.self_cognition`、`gscore.group_profile` |

### 超时预算

| 点位 | 第三方默认 | 说明 |
|------|------------|------|
| H00 | 200ms | memory observe 声明 500ms |
| H01–H04 / H07–H09 | 500ms | classifier / 软门 2s（已有 LLM） |
| **H05** | **15s** | **唯一允许的长超时**（双路 + rerank）。第三方占 `memory` 槽 = 接受这个预算 |
| H06 | 500ms | 应只读缓存/已检索结果 |
| H10–H15 | 200ms | tool_assembly 2s（含向量检索） |
| H16–H22 | **50ms** | 热路径，别在这里做 IO |
| H23–H28 | 200ms | quality 与内核纠正同寿 |

---

## 13.3 能力票（Context 不交出 `RunOnceState`）

`AgentHookContext` 按点位声明可写操作，未授权调用抛 `HookCapabilityError`
（dispatcher fail-open 记 warning）。**第一方套件也走能力票**——否则套件和第三方
变成两套 API，替换时对不齐。

| 方法 | 允许的点位 | 说明 |
|------|-----------|------|
| `set_context_block(name, text)` | H05 H06 H07 H29 | `name` 必须在 `CONTEXT_BLOCK_ORDER`（或 H29 的稳定块名），未知名**拒绝** |
| `stash_retrieved(name, text)` | H05 | 检索窗暂存，H06 再写成正式块 |
| `set_has_actionable(flag)` | H06 | planning 抬档 |
| `set_intent(intent)` | H03 | 仅 `闲聊`/`工具`/`问答` |
| `set_should_speak(flag)` | H04 | 软门结果 |
| `append_user_hint(text)` | H07 H13 H16 | 自动加 `（套件·` / `（插件·` 前缀 |
| `ensure_tools` / `drop_tools` | H14 H15 | 护栏见 §13.6 |
| `replace_text(text)` | H20 | 改台词 |
| `set_tool_return(text)` | H17 | 折叠/摘要 |
| `request_correction(reason)` | H23 H24 | 纠正 |
| `abort(reason)` | H01 H03 H10 | 整轮不跑 |
| `silence(reason)` | H01 H04 H20 | 本轮不出声 |
| `veto_tool(reason)` | H18 | 拦下这次工具调用 |

**只读**：`ctx.group_id`（**私聊恒 `None`**，记忆 scope 防回归的地基）、`ctx.user_id`、
`ctx.bot_id`、`ctx.relationship`、`ctx.intent`、`ctx.prev_turn_used_tools`、
`ctx.recent_report_titles`、`ctx.mood_key`、`ctx.cheap_gate`。

---

## 13.4 写一个第三方 hook（只追加，不换槽）

```python
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook

@on_agent_hook(AgentHookPoint.AFTER_CONTEXT, priority=420)
async def inject_watchlist(ctx: AgentHookContext) -> None:
    if ctx.ev is not None and ctx.group_id:
        ctx.append_user_hint(f"本群自选：{await load(ctx.group_id)}")
```

- `priority` 越小越先；**第一方占 100–399，第三方建议 400+**。
- 同优先级串行按注册序（环内有变异，不能像启动钩子那样并发）。
- 同步函数会被丢到 `asyncio.to_thread`。
- 热重载：`reload_plugin` 按 `__module__` 前缀摘钩，并把该插件占用的槽位回落默认套件。

---

## 13.5 写一个套件（换掉整套能力）

```python
from gsuid_core.ai_core.kits import AgentKit, register_agent_kit
from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, on_agent_hook

class Mem0Kit(AgentKit):
    def register(self) -> None:
        on_agent_hook(AgentHookPoint.ON_INBOUND, priority=420, kit_id=self.kit_id)(self.observe)
        on_agent_hook(AgentHookPoint.RETRIEVE_CONTEXT, priority=420, kit_id=self.kit_id)(self.retrieve)
        on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=420, kit_id=self.kit_id)(self.inject)

    async def retrieve(self, ctx: AgentHookContext) -> None:
        ctx.stash_retrieved("memory", await my_mem0_search(ctx.query, ctx.user_id))

    async def inject(self, ctx: AgentHookContext) -> None:
        text = ctx.retrieved["memory"] if "memory" in ctx.retrieved else ""
        if text:
            ctx.set_context_block("memory", text)

KIT = register_agent_kit(
    Mem0Kit(kit_id="mymem.mem0", slot="memory", display_name="Mem0 记忆",
            owns_tools=("my_mem_search",))
)
```

配置 `kit_slots.memory = "mymem.mem0"` → 框架卸 `gscore.memory`、装你的。

**注意**：
- `kit_id` 必须以插件名开头（热重载靠这个前缀识别归属并回落槽位）。
- `slot` 必须与被配置的槽名一致，否则**拒绝启用并 warning**（不校验会「配错槽却静默启用到
  别的槽」，被配的那个槽反而空着）。
- `owns_tools` 会在关槽 / 替换时被 `unregister_tool` 卸掉——否则「套件没了、模型还看见空壳工具」。
- **换槽 ≠ 在 hook 里重做一套 RAG**。记忆实现内部的事（认知节点、双路检索）见 §09。

---

## 13.6 18 个槽位

| slot | 默认占用者 | 互斥 | 说明 |
|------|------------|------|------|
| `inbound_observe` | `gscore.meme` | 否 | 入站观察扇出（记忆观察随 `memory` 槽走） |
| `memory` | `gscore.memory` | 是 | 检索 + 注入 + 工具轨迹 + 观察 |
| `favorability` | `gscore.favorability` | 是 | 关系温度读 / 注入 / 上报 / 衰减 job |
| `mood` | `gscore.mood` | 是 | 情绪注入 + 收尾更新 |
| `self_cognition` | `gscore.self_cognition` | 是 | 稳定自述（H29）+ 口吻锚点 |
| `group_profile` | `gscore.group_profile` | 是 | 稳定前缀群画像（H29） |
| `planning_context` | `gscore.planning_context` | 是 | 长任务文案 + `has_actionable` |
| `classifier` | `gscore.classifier` | 是 | intent |
| `reactive_gate` | `gscore.reactive_gate` | 是 | 软门 + 软触发提示块 |
| `scaffold` | `gscore.scaffold` | 是 | 风格短句 + C-1/C-2 hints |
| `session_mute` | `gscore.session_mute` | 是 | 静默窗 |
| `statistics` | `gscore.statistics` | 是 | 统计（**只上报，不扣减**） |
| `tool_assembly` | `gscore.tool_assembly` | 是 | 五层装配 + `find_tools` |
| `fileos` | `gscore.fileos` | 是 | 回执折叠 |
| `post_tool` | `gscore.post_tool` | 是 | 工具后契约 |
| `quality` | `gscore.quality` | 是 | 假完成 / 出图纠正 |
| `speech` | `gscore.speech` | **密封** | 出站话术态：可关不可替 |
| `persona_identity` | `gscore.identity` | **密封** | 身份锚：不可关 |

槽名**不含点号**（点号既是槽名一部分又是配置层级分隔符会有解析歧义）。
配置键 `kit_slots.<slot>`，由槽位表**派生**——不要手写 18 个近似条目。

### 工具装配槽的三条内核收口（换套件也逃不掉）

1. `addr_gated` 为真时**不打** `ASSEMBLE_TOOLS`（C-3 零工具硬约束）；
2. H14 之后与 H15 之后**各剥离一次** exclusive（前者防套件直接装 `render_*`，
   后者防第三方 `ensure` 回来）；
3. `self` / `buildin` / `meta` 是特权分类，`ensure_tools` **拒绝**；也不许 drop
   `create_subagent`。

> **在途短轮**：内核 `in_flight_short` 会在 H14 默认装配里跳过语境池/向量检索。换套件时
> 应继续尊重 `ctx` / extra 里的短轮信号，不要在催进度的短句上灌满 40+ 工具（毁前缀缓存）。

> 本槽 `off` 时 `find_tools` 一并消失（它是 `meta` 分类、由装配层注入，用户套件无权
> `ensure`）——这是正确行为，验收文案要写明。

---

## 13.7 红线（破一条就是生产事故）

| 红线 | 要求 |
|------|------|
| **前缀缓存** | `system_prompt` 会话内绝不改串。**H29 是唯一**允许贡献 system 稳定块的点位，且只在建 session / persona 热更时触发，dispatcher 硬拒其余时机 |
| **zone 只进 user 侧** | 关系是 per-user，群 session 共享前缀。**绝不**把 zone 写进人设卡 / system |
| **保头裁中段** | 禁止套件改 `history[0]`、禁止 `history[-n:]` 式 compact |
| **出站双闸顺序** | `speech_policy.should_block` → H20 → `pre_send_gate`，顺序不可换、不可跳、不可被套件替换。`speech_policy=delivered` 挡住后**不打 H20**。出图在途 / 多点读数密度同样走 `should_block`，套件不得把念白改写后放行 |
| **fail-open 但必须告警** | 单个 hook 异常吞掉 + `logger.warning`。**不得**升级成 `ABORT_RUN`、**不得**变成人格台词 |
| **不写 Agent 实例态** | 禁止往 `GsCoreAIAgent` 实例写状态（同 session `_run_lock` 下仍有并发）。跨 step 用 `extra_set("plugin.{kit}.…")`，H28 清理 |
| **统计 / 预算不可套件化** | 预算闸与 token 记账留内核；套件挂 H08/H11/H22 只**上报** |
| **闸门应过滤，不该整轮跳过** | 槽位 `off` 的正确做法是**不注册**（自然跳过），**不是**在内核写 `if enable_x` |
| **私聊 `group_id=None`** | `ctx.group_id` 私聊恒 None；回退成 `user_id` 会去查空的幻影 `group:{user_id}` |
| **AI 总开关** | `ai_config.enable` 关 → 总线与套件初始化整条不跑 |

---

## 13.8 可观测

```text
[AgentHook] fire point=COMPOSE_CONTEXT hooks=8
[AgentKit]  load slot=memory kit=gscore.memory
[AgentKit]  replace slot=memory old=gscore.memory new=mymem.mem0
[AgentHook] fail point=RETRIEVE_CONTEXT hook=gscore.memory.retrieve e=...
[AgentHook] 超时 point=AFTER_SEND hook=... 预算=50ms
```

WebConsole（需登录）：

- `GET /api/agent_kits/slots` —— 槽位健康（`occupants` 为空 = 该槽 off；
  `sealed=true 且 occupants=[]` 应打红字）
- `GET /api/agent_kits/hooks` —— 31 点位契约 + 当前挂载者 + 总闸状态
- `GET /api/relationship/view?user_id=…` —— zone / `last_reason` / 日预算余量
- `GET /api/cognition/nodes?keyword=…` —— 认知节点索引

---

## 13.9 明确不做

- 让用户替换 `pre_send_gate` 编排顺序 / exclusive / 前缀裁剪 / 预算记账。
- 在 H16 里允许任意套件再开一轮 LLM（LLM 只在 H03/H04 已有的地方）。
- 把 RAG 知识库真值源并进 `memory` 槽（认知层是索引层，见 §09/§10）。
- 跨进程套件（见 `plans/plugin_subprocess_and_sdk_split_20260814.md`，仍冻结）。
