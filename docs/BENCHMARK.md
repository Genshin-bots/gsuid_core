# GsCore 评测账本（BENCHMARK）

> 写给人和编码 Agent。源码仍是唯一事实源。本文件记录**已完成的实测**，不是发布门槛。
>
> - **Agent 硬核套件**：最近一次全量是 **2026-09-04**（通道核统一，pass^k=1，**443/496 = 89.3%**）。k=3 对照仍是 2026-08-28 的 383/496。
> - **LongMemEval-S**：最近一次生产 Chat 是 **2026-09-03 prod7 = 462/500**。不要把 prod1–prod6 或 dump/对齐口径混报成现行分。
> - 高级模型：`openai++MiniMAX`（次选 `openai++商汤科技`）。嵌入：`jinaai/jina-embeddings-v2-base-zh`。
> - 人格：Agent 套件走生产人格；LongMem 作答走 **评测助手**（必须作答、禁止静音）。群聊生产默认静音，评测不得把「不要 SILENCE」写进工具回执。

---

## 一、当前分数

两套评测测的不是同一件事：

| 套件 | 测什么 | 口径 | 分数 |
|------|--------|------|------|
| **Agent 硬核套件** | 群聊 Agent：工具、寻址、人格、跨轮、安全、出图… | 独立 `user_id`；**pass^k=1**；通道核 12 工具 | **443/496 = 89.3%**（2026-09-04） |
| 同上，旧 k=3 对照 | 同一 496 例，三次合取 | pass^k=3 | 383/496 = 77.2%（2026-08-28） |
| 同上，更小例集 k=1 | 447 例 | pass^k=1 | 404/447 = 90.4%（历史，例集不同） |
| **LongMemEval-S（生产 Chat）** | 目录卡 + 模型自己 `search_cognition` | `--enable-tools --no-memory-eval --inject-date` | **462/500 = 92.4%**（2026-09-03 prod7） |

462 是 **评测助手 + 目录卡 + 工具回想 + 词面跨会话补条**，不是「通用闲聊记忆」。群聊生产仍应大部分时候静音。

历史对照（不要当现行分）：召回对齐 378/500；dump 战役 458/500。Chat 高于 dump，因为作答策略（工具、问答 hint）补了检索缺口，不是检索天花板。

### 1.1 LongMemEval-S 生产 Chat（2026-09-03 prod7）

skip-ingest，`--enable-tools --no-memory-eval --inject-date`，答卷 `*_prod7.json`。HTTP **500/500**，`<SILENCE>` **0**，judge-error **0**。

| 域 | prod7 |
|----|-------|
| SSP | 27/30 (90.0%) |
| SSU | 69/70 (98.6%) |
| SSA | 54/56 (96.4%) |
| KU | 76/78 (97.4%) |
| MS | 118/133 (88.7%) |
| TR | 118/133 (88.7%) |
| **合计** | **462/500 = 92.4%** |

### 1.2 Agent 硬核套件（2026-09-04，通道核统一，pass^k=1）

报告：`eval/agent/results/_kernel_unify_k1.json`。召回金标：`eval/agent/results/_recall_probe.json`（**10/10**，`core_pool_size=12`）。

群/私同一份 `MAIN_AGENT_CORE_TOOLS`（12）：发现 / 回想 / 委派 / 发送 + `add_once_task` / `add_interval_task`。列出/改/删/暂停不进核，靠本句检索或 `find_tools`。

| 指标 | 08-28 k=3 | **09-04 k=1（本轮）** |
|------|-----------|----------------------|
| 通过率 | 383/496 = 77.2% | **443/496 = 89.3%** |
| 均延迟 / P50 / P95 | 15.0s / 6.1s / 77.5s | 22.3s / 12.9s / 75.7s |
| input tokens | 未记账 | **6,988,121**（例均 14,089） |
| output / cache_read | 未记账 | 193,312 / 4,141,152 |
| cache_rate | 49.8% | **59.3%** |
| 通道核 schema | 私聊约 24、群聊更瘦 | **12**（群私同一份） |
| 闲聊 input（`rel_greeting`） | 调试会话 ~22k、24 工具 | **18,364** |

k=3 与 k=1 不能直接比准确度。旧 k=1 **404/447 = 90.4%** 例集更小，只能当量级对照。

工具域：`tool_selection_args` **17/19**；`tool_disambiguation` **4/4**；召回探针 **10/10**。两例工作日打卡（`args_grp_workday_only` / `hard_args_workday`）核内已有 `add_interval_task`，模型走了 `find_tools` / `create_subagent`。`tool_relevance` 6/12：闲聊仍调用核内 `search_cognition`（`no_tool_calls` 失败）。`speaker_slot_recall` 44/49。

首轮连跑曾被 Core 卡死 + judge 404 污染成 370/496；污染卷在 `_kernel_unify_k1_contaminated.json`，**不以那次为准**。

### 1.2.1 对照：2026-08-28 pass^k=3

报告：`eval/agent/results/_benchmark_k3_20260828.json`。单 run 通过 1288/1488 = 86.6%。k=3 合取会把「2/3 过」的开放题整例判死。

### 1.3 离线单测

改记忆 / 脚手架 / 装配 / 时钟后至少：

```
uv run pytest tests/test_memory_injection_quality.py tests/test_memory_set_recall.py tests/test_context_assembly.py tests/test_interaction_scaffold.py tests/test_eval_judge_parse.py tests/test_agent_kits_slots.py -q
```

单测全绿不能代替实机 500 题。

---

## 二、为什么是这个分

Agent 套件考办事：开口、工具、省略跟进、群聊 @、出图、人格。和 LongMem 90%+ 不可比。

LongMem 协议：每题独立 `user_id=eval_{qid}`；`batch_observe` 摄入（**不** extract）；作答 `history=[]`。生产 Chat 走目录卡 + `search_cognition`。`clock_at` 是 HTTP 显式字段，**禁止**从用户原文解析写进 system（§1.7）。问句类型不再用英文正则分流；相对日窗口只在有显式时钟时做日期换算。

群聊：未点名走寻址门 / SILENCE。评测助手禁静音；工具回执不得写「不要 SILENCE」。

---

## 三、下次怎么评测

一律仓库根目录。Windows 用 PowerShell。实机必须全插件 Core，**不要** `--dev`。

### 3.0 启动 Core

```powershell
$env:GSUID_LOCAL_TEST_MODE = "1"
$env:GSUID_LOCAL_TEST_TOKEN = "<token>"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:NO_PROXY = "localhost,127.0.0.1"
$env:HTTP_PROXY = ""; $env:HTTPS_PROXY = ""; $env:ALL_PROXY = ""
Set-Location F:\gsuid_core
uv run core --port 8765
```

Core 不要被包装层 10 小时杀掉（`background=true` 或等价）。死后**不要清库**，resume 续跑。

### 3.1 Agent 硬核套件

入口：`python -m eval.agent.run`。

```powershell
uv run python -m eval.agent.selftest
uv run python -m eval.agent.run --dry-run
uv run python -m eval.agent.run --base-url http://127.0.0.1:8765 --token $env:GSUID_LOCAL_TEST_TOKEN --k 1 --judge bot --concurrency 2 --timeout 360 --delivery-wait 90 --out eval/agent/results/_agent_k1.json
```

看发布线用 k=1。现行账是 09-04 的 **443/496**；不要拿 08-28 的 k=3 77.2% 和 90% 比。分块续跑用 `eval.agent._chunked_run`，只把连接失败 / `session_log_not_found` 当传输故障重试；`max_latency`、静音、未完成是产品失败，留下。

### 3.2 LongMemEval-S

数据：`eval/longmemeval/longmemeval_s_cleaned.json`（500 题）。

统一入口：`uv run python eval/run_eval.py longmem <stage>`。

| stage | 作用 |
|-------|------|
| `probe` | 摄入（除非 `--skip-ingest`）+ 作答 |
| `judge` | LLM 判 PASS/FAIL |
| `report` | 打印已有 judge |
| `diagnose` | 对照 answers 诊断 |
| `run-domains` | 按域顺序 probe+judge（生产 Chat：tools + 目录卡 + `clock_at` + skip-ingest） |
| `mark-fails` | 仅把传输/空答失败改成 `[ERROR] rerun` 并丢掉对应 judge；内容 FAIL 不重跑 |

```powershell
# 现行生产 Chat 口径（库里已有 ingest）
uv run python eval/run_eval.py longmem run-domains --tag prod7

# 只重跑一个域
uv run python eval/run_eval.py longmem run-domains --question-type multi-session --tag prod7

# 标记传输失败题后 resume（内容 FAIL 不会被改写）
uv run python eval/run_eval.py longmem mark-fails --tag prod7
uv run python eval/run_eval.py longmem run-domains --tag prod7
```

`--inject-date` 把 `question_date` 放进 HTTP `clock_at`，不再把「当前时间：」拼进问句。

不要开 `GSUID_EVAL_MEMORY_FULL_SCOPE`（那是 dump 战役）。不要 `--extract`。不要 `--clear-first` 除非有意毁掉该 scope。

改了 `eval_protocol.py` / `chat_with_history_api.py` / `turn_pipeline.py` 后必须重启 Core。

### 3.3 BEAM-10M

```powershell
uv run python eval/run_eval.py beam probe --conv 0
uv run python eval/run_eval.py beam judge --conv 0
```

不要和 LongMem 500 混在同一 answers 文件里。

### 3.4 改代码时的回归面

| 你改了 | 先跑 |
|--------|------|
| 记忆检索 / 词面补条 | `tests/test_memory_set_recall.py` + `tests/test_memory_injection_quality.py` |
| `turn_pipeline.py` 时钟 | `tests/test_agent_kits_slots.py` |
| 双入口装配 / `clock_at` | `tests/test_context_assembly.py` |
| `interaction_scaffold.py` | `tests/test_interaction_scaffold.py` |
| 装配 / 闸门 / 每轮注入 | 单测绿不够，还要对照 `eval/agent` 群聊基准 |
