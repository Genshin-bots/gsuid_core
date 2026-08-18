# GsCore Agent 单次 run 拆分：`agent_run` 包（2026-08-08）

> **状态**：已合入工作区源码；行为与拆分前 `_execute_run_once` 单函数版等价。
> **2026-08-10 增补**：在本包上新增 DELIVERED 交付终局态 / 交付状态汇报防火墙 /
> 出图候选时效判据等（见 §8），拆分结构不变。
> **读者**：改 `ai_core` Agent 环 / 工具装配 / 出站闸 / 假完成 的开发者与 Agent
> **红线**：[AGENTS.md](../AGENTS.md)
> **生命周期总览**：[AI_AGENT_LIFECYCLE_SEQUENCE.md](AI_AGENT_LIFECYCLE_SEQUENCE.md) §10
> **关联**：OOC/委派 [AI_CORE_OOC_DELEGATION_UPDATE_20260724.md](AI_CORE_OOC_DELEGATION_UPDATE_20260724.md)；
> OOC 根治归因 [AI_SESSION_OOC_ROOTCAUSE_20260810.md](AI_SESSION_OOC_ROOTCAUSE_20260810.md)

---

## 0. 一句话

把原先堆在 `gs_agent.GsCoreAIAgent._execute_run_once` 里的 **~1500 行** 主路径，拆成
`gsuid_core/ai_core/agent_run/` 包：按 **准备 → 工具 → iter 环 → 收尾** 分模块，
`gs_agent.py` 只保留 Session 类、重试、闸门收尾辅助与工厂；**运行语义不变**。

---

## 1. 为什么改

| 问题 | 后果 |
|------|------|
| `_execute_run_once` 过长 | 读不懂、难 review、改假完成/工具层易误伤闸门 |
| 逻辑与类型耦合在同一巨型方法 | 局部变量爆炸；拆阶段后需要 `RunOnceState` |
| 循环依赖风险（阶段模块 ↔ `gs_agent`） | 拆包时 import 炸；必须把共享纯函数/预算 ctx 下沉 |

**非目标（本批不做）**：改工具五层策略、改 `pre_send_gate` 顺序、改假完成/出图 nudge 语义、改 subagent 契约文案。

---

## 2. 目标结构

```text
gsuid_core/ai_core/
  gs_agent.py              # GsCoreAIAgent + 重试/历史裁剪/闸门收尾 helper + create_agent
  agent_run/               # 单次 run 阶段包
    __init__.py            # 文档 + 惰性导出 RunOnceMixin / RunOnceState
    state.py               # RunOnceState、BUDGET_GATE_PASS
    host.py                # RunOnceHost：宿主字段 + 跨阶段方法槽（类型）
    support.py             # 纯函数/常量：假完成、thrash、委派 exclusive…
    budget_ctx.py          # 预算 scope contextvar（无 gs_agent 依赖）
    prepare.py             # A：预算闸门 → init → user 消息 / 脚手架
    tools.py               # B：工具五层 + 构建 pydantic-ai Agent
    loop.py                # C：Agent.iter（ModelRequest / CallTools / End）
    settle.py              # D：history / 闸门收尾 / 假完成 / UsageLimit / cleanup
    orchestrator.py        # _execute_run_once 编排入口
    mixin.py               # RunOnceMixin = Prepare+Tools+Loop+Settle+Orchestrator
```

`GsCoreAIAgent(RunOnceMixin)`：运行时能力来自 mixin 组合；对外 API（`run` / `create_agent`）不变。

### 2.1 编排入口（读代码从这里开始）

源码：`agent_run/orchestrator.py` → `_execute_run_once`

```text
st = RunOnceState(...)
early = await budget_gate(st)
if early is not BUDGET_GATE_PASS: return early
try:
    init_state(st)
    prepare_user_message(st)     # RAG / DS / 无工具提醒 / 脚手架
    assemble_tools(st)           # 五层 + exclusive + find_tools
    agent = build_agent_meta(st)
    return await iter_and_settle(st, agent, stats)
except UsageLimitExceeded:
    return await usage_limit_fallback(...)
finally:
    cleanup(st)                  # budget scope / 墙钟 / 单轮节流
```

瞬时故障（超时/网络/5xx/529）**仍不在 once 内捕获**，由 `_execute_run` 重试包装处理。

### 2.2 阶段 ↔ 生命周期 §10

| 阶段 | 模块 | 生命周期章节 | 要点方法 |
|------|------|--------------|----------|
| A 准备 | `prepare.py` | §10.1–10.2 | `_run_once_budget_gate` / `_run_once_init_state` / `_run_once_prepare_user_message` |
| B 工具 | `tools.py` | §10.3 | `_run_once_assemble_tools` / `_run_once_build_agent_meta` |
| C 环 | `loop.py` | §10.4–10.5 | `_run_once_on_model_request` / `_run_once_on_call_tools` / `_run_once_iter_and_settle` |
| D 收尾 | `settle.py` | §10.8 / §11.1 | `_run_once_settle_result` / `_run_once_usage_limit_fallback` / `_run_once_cleanup` |
| 编排 | `orchestrator.py` | §10 总入口 | `_execute_run_once` |
| 共享状态 | `state.py` | — | `RunOnceState`（环内可变：工具列表、闸门 pending、假完成暂扣、thrash…） |
| 宿主声明 | `host.py` | — | `history` / `create_by` / 闸门收尾槽…（由 `GsCoreAIAgent` 实现） |
| 纯函数 | `support.py` | — | `_claims_fake_done`、thrash、exclusive 剥离、委派画像… |
| 预算 ctx | `budget_ctx.py` | 预算记账 | `_current_budget_scope`、`set/reset_budget_scope_context` |

### 2.3 仍留在 `gs_agent.py` 的职责

- `GsCoreAIAgent` 构造、history 裁剪、角色锚定提取
- `_execute_run` 瞬时失败重试 / 干净历史重试
- 输出闸收尾：`_resolve_output_gate_after_run` / `_ooc_rewrite_and_send` / `_lightweight_text_rewrite`
- 假完成 history scrub：`_scrub_fake_done_history`
- 工厂 `create_agent` / `build_new_persona`
- **兼容 re-export**：测试与外部仍可 `from gsuid_core.ai_core.gs_agent import _THRASH_SAME_TOOL_LIMIT, _claims_fake_done, …`

### 2.4 依赖方向（禁止循环）

```text
gs_agent ──imports──► agent_run.{mixin, support, budget_ctx}
agent_run.prepare/tools/loop/settle ──imports──► agent_run.{state, host, support, budget_ctx}
agent_run.*  ──✗──►  gs_agent   （阶段模块不得再 import gs_agent）
```

`agent_run/__init__.py` 对 `RunOnceMixin` **惰性** `__getattr__`，避免
`from agent_run.support import …` 时拉起整棵阶段图。

---

## 3. 类型与工程卫生（对齐 AGENTS.md）

| 项 | 做法 |
|----|------|
| 状态袋 | `@dataclass RunOnceState`，`return_mode: Literal[...]`，工具/上下文显式类型 |
| Optional 收窄 | `_require_context` / `_require_limits`（init 后断言） |
| 禁止 cast / type: ignore 糊弄 | 宿主方法槽写在 `RunOnceHost`，跨 Phase 调用可类型检查 |
| 禁止 getattr 兜底（本批触达路径） | `Event.is_tome` / `image_id_list` 等直读；`extra` 用 `in` 再下标 |
| ruff / pyright / basedpyright | 目标：**0 error / 0 warning**（对本批路径） |

---

## 4. 行为等价清单（验收用）

拆分前后应保持：

1. 预算闸 `budget_gate=True` 超额早退（无墙钟 install）；放行后 finally 还原 scope/墙钟
2. 工具五层 + exclusive 剥离 + `create_subagent` / `find_tools` 注入条件
3. `Agent.iter` 内墙钟 / thrash / 输出闸 REWRITE·FUSE 注入
4. TextPart：`pre_send_gate(channel=main)` → 假完成暂扣 → `send_chat_result`
5. ToolReturn：tech dump 屏蔽、主人格 JSON 折叠、POST_TOOL 分通道契约
6. supersede：cancel 后不写 history
7. settle：`_resolve_output_gate_after_run` → 假完成/结构零工具/render nudge 纠正重跑
8. return 路径：Capability/subagent **跳过** roleplay OOC scrub，仅 tech dump 摘要
9. `UsageLimitExceeded` 专属兜底总结（return 模式不 bot 说话）

---

## 5. 怎么改代码（导航）

| 需求 | 改哪里 |
|------|--------|
| 改装配层 / 闲聊是否搜工具 | `agent_run/tools.py` |
| 改脚手架 hints / 无工具强制提醒 | `agent_run/prepare.py` |
| 改 TextPart 发送闸顺序 | `agent_run/loop.py` + `output_gate.py` |
| 改假完成 / render nudge | `agent_run/settle.py` + `support.py` 常量 |
| 改尖括号/OOC **收尾** | `gs_agent._resolve_output_gate_after_run` |
| 改瞬时重试 | `gs_agent._execute_run` |
| 改预算 contextvar API | `agent_run/budget_ctx.py`（`gs_agent` re-export） |

**生命周期文档**改行为后必须同步 [AI_AGENT_LIFECYCLE_SEQUENCE.md](AI_AGENT_LIFECYCLE_SEQUENCE.md) §10 / §S.6。

---

## 6. 验证（本批基线）

```bash
uv run ruff check gsuid_core/ai_core/agent_run gsuid_core/ai_core/gs_agent.py
uv run ruff format --check gsuid_core/ai_core/agent_run gsuid_core/ai_core/gs_agent.py
uv run pyright gsuid_core/ai_core/agent_run gsuid_core/ai_core/gs_agent.py
uv run basedpyright gsuid_core/ai_core/agent_run gsuid_core/ai_core/gs_agent.py

uv run pytest \
  tests/test_behavior_scaffold_fixes.py::test_no_tool_reminder_exempts_chitchat \
  tests/test_review_fixes_20260717.py::test_compact_runs_after_history_surgery \
  tests/test_thrash_and_subagent_delivery.py \
  tests/test_capability_delegation_flow.py \
  tests/test_benign_fp.py -q
```

期望：ruff/pyright 全绿；上述 pytest 通过。
`inspect.getsource` 类测试已改为指向 `agent_run` 阶段方法（如 `_run_once_settle_result`）。

---

## 7. 迁移说明

| 旧路径（概念） | 新路径 |
|----------------|--------|
| `gs_agent._execute_run_once` 整函数 | `agent_run.orchestrator.OrchestratorPhase._execute_run_once`（经 `RunOnceMixin` 挂在 `GsCoreAIAgent`） |
| once 内局部变量 | `RunOnceState` 字段 |
| once 内工具装配块 | `agent_run.tools` |
| once 内 `Agent.iter` 循环 | `agent_run.loop` |
| once 内假完成/UsageLimit/finally | `agent_run.settle` |
| `_current_budget_scope` 定义点 | `agent_run.budget_ctx`（`gs_agent` 仍 re-export） |

**对外** `create_agent` / `GsCoreAIAgent.run` **签名与默认语义不变**。

---

## 8. 2026-08-10 在 `agent_run` 上的增补（OOC 根治批次）

拆分结构不变，仅在既有阶段上叠加以下能力（归因见
[AI_SESSION_OOC_ROOTCAUSE_20260810.md](AI_SESSION_OOC_ROOTCAUSE_20260810.md)）：

| 位置 | 新增 | 说明 |
|------|------|------|
| `state.py` | `delivered_terminal` / `delivered_nudged` / `saw_timeless_aggregate` / `main_channel_sends` | DELIVERED 终局、终局指令是否已注入、低时效聚合标记、主通道出站计数 |
| `speech_policy.py` | `SpeechPolicy` 加 `"delivered"`；`MAIN_CHANNEL_VISIBLE_LIMIT`；`looks_like_delivery_status_narration`；出图候选时效+多点判据 | 交付后只许 SILENCE；交付状态汇报结构检测 |
| `loop.py` | ToolReturn 侧读 `extra["delivered_with_speech"]` 置 DELIVERED、注入终局 SILENCE 指令、低时效提醒；TextPart 侧 speech_policy 闸 + 出站配额 | 见生命周期 §10.4 |
| `settle.py` | 结构零工具纠正加 **SILENCE 自洽出口** | 概念题已答全则不刷屏、不削原答 |
| `support.py` | `_STRUCTURAL_ZERO_TOOL_NUDGE` 文案增"已答全→SILENCE"分支 | 同上 |
| `prepare.py` | supersede 交接语注入（`_pending_delegation_handoff`） | 抢答打断在途委派时，后到 run 感知 |
| `tools.py` | 召回阈值 `tool_recall_threshold` 传入检索 | 低于阈值不装配 |
| `host.py` / `gs_agent.py` | `_pending_delegation_handoff` 宿主字段 | 交接语暂存 |

**验收**：`tests/test_delivery_terminal_and_firewall_20260810.py`（12 例结构 fixture）+
既有 `test_speech_policy_20260808.py` / `test_output_gate.py` / `test_benign_fp.py` 全绿。

---

*文档与源码不一致时以源码为准；改 agent 环后请同步本文与生命周期 §10。*
