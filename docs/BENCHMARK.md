# GsCore 评测账本（BENCHMARK）

## 铁律

**本文件每个评测只写一个分数：当前最好成绩。禁止写入任何优化历史、对照轮次、旧口径、A/B、vN 序列或「不要和某某混报」类叙述。**

被刷新的那一行直接覆盖；旧分只留在 `eval/**/results/` 原始报告里，不进本文件。怎么跑评测、怎么启 Core → 见文末**附录**。

---

写给人和编码 Agent。源码仍是唯一事实源。本文件记录**已完成的实测最好分**，不是发布门槛。

- 高级模型：`openai++MiniMAX`（次选 `openai++商汤科技`）。嵌入：`jinaai/jina-embeddings-v2-base-zh`。
- 人格：Agent 套件走生产人格；RAG / LongMem / BEAM / CorpusQA / LOFT 作答走 **评测助手**（必须作答、禁止静音）。

---

## 一、当前分数

| 套件 | 测什么 | 口径 | 分数 |
|------|--------|------|------|
| **Agent 硬核套件** | 群聊 Agent：工具、寻址、人格、跨轮、安全、出图… | 独立 `user_id`；**pass^k=1**；通道核 12 工具 | **443/496 = 89.3%**（2026-09-04） |
| **LongMemEval-S（生产 Chat）** | 目录卡 + 模型自己 `search_cognition` | `--enable-tools --no-memory-eval --inject-date` | **462/500 = 92.4%**（2026-09-03） |
| **BEAM 100k（官方 128K）** | 多会话记忆探针（20 conv × 20 题） | 评测助手 + tools；`batch_observe` 灌对话；`clock_at` | **226/400 = 56.5%**（2026-09） |
| **CorpusQA 128k** | 跨文档统计 / 比较（分层 40 题，每域 10） | 评测助手 + tools；语料 `batch_observe`；不塞全文进当前轮 | **36/40 = 90.0%**（2026-09-28） |
| **CorpusQA 1m** | 同上，1M 语料分层 40 题 | 同上 | **10/40 = 25.0%**（2026-09-27） |
| **LOFT RAG · nq · 128k** | GlobalQA 语料级问答（100 题） | 评测助手 + tools；语料灌记忆 | **94/100 = 94.0%**（2026-09） |
| **LOFT retrieval · scifact · 128k** | BEIR 文本检索 recall@1（100 题） | 同上 | **87/100 = 87.0%**（2026-09） |

### 1.0 BEAM 100k（官方 128K）

报告：`eval/BEAM_official/results/100k/_runs/v2_reprobe_m3_c11/report.md`。

| 类别 | 过线 |
|------|------|
| abstention | 23/40 |
| contradiction_resolution | 32/40 |
| event_ordering | 1/40 |
| information_extraction | 25/40 |
| instruction_following | 31/40 |
| knowledge_update | 21/40 |
| multi_session_reasoning | 16/40 |
| preference_following | 33/40 |
| summarization | 15/40 |
| temporal_reasoning | 29/40 |
| **合计** | **226/400 = 56.5%** |

### 1.1 CorpusQA 128k（分层 40 题）

报告：`eval/corpusqa/results/128k/report_n40_v15.md`。

| 域 | 通过 |
|----|------|
| education_en | 10/10 (100%) |
| financial_en | 9/10 (90%) |
| financial_zh | 10/10 (100%) |
| real_estate_en | 7/10 (70%) |
| **合计** | **36/40 = 90.0%** |

### 1.2 CorpusQA 1m（分层 40 题）

报告：`eval/corpusqa/results/1m/report_n40_v10.md`。

| 域 | 通过 |
|----|------|
| education_en | 1/10 (10%) |
| financial_en | 0/10 (0%) |
| financial_zh | 5/10 (50%) |
| real_estate_en | 4/10 (40%) |
| **合计** | **10/40 = 25.0%** |

### 1.3 LOFT RAG · nq · 128k

报告：`eval/loft/results/rag/nq/128k/report_full_v7.md`。**94/100 = 94.0%**。

### 1.4 LOFT retrieval · scifact · 128k

报告：`eval/loft/results/retrieval/scifact/128k/report_full_v7.md`。**87/100 = 87.0%**（recall@1 均值 0.870）。

### 1.5 LongMemEval-S 生产 Chat

HTTP **500/500**，`<SILENCE>` **0**。

| 域 | 通过 |
|----|------|
| SSP | 27/30 (90.0%) |
| SSU | 69/70 (98.6%) |
| SSA | 54/56 (96.4%) |
| KU | 76/78 (97.4%) |
| MS | 118/133 (88.7%) |
| TR | 118/133 (88.7%) |
| **合计** | **462/500 = 92.4%** |

### 1.6 Agent 硬核套件（pass^k=1）

报告：`eval/agent/results/_kernel_unify_k1.json`。

| 指标 | 值 |
|------|----|
| **通过率** | **443/496 = 89.3%** |
| 均延迟 / P50 / P95 | 22.3s / 12.9s / 75.7s |
| input tokens | 6,988,121（例均 14,089） |
| output / cache_read | 193,312 / 4,141,152 |
| cache_rate | 59.3% |

---

## 附录 A · 怎么跑这些评测

一律在仓库根目录（含 `pyproject.toml`）。Windows 用 PowerShell。实机必须打**已启动且加载全部插件**的 Core，**不要** `--dev`。

刷新本文件时：**只改对应评测那一行分数与分域表**，删掉被超越的旧分，不要追加对照段。

### A.0 启动 Core（所有实机评测共用）

```powershell
$env:GSUID_LOCAL_TEST_MODE = "1"
$env:GSUID_LOCAL_TEST_TOKEN = "<token>"   # 与 Core 进程环境一致
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:NO_PROXY = "localhost,127.0.0.1"
$env:HTTP_PROXY = ""; $env:HTTPS_PROXY = ""; $env:ALL_PROXY = ""
Set-Location F:\gsuid_core
uv run core --port 8765
```

要点：

- 等日志出现启动完成 / `Uvicorn running` 再开 probe。
- 长跑不要被包装层 10 小时杀掉（Cursor 里用 `background=true` 或本机独立终端）。
- Core 死后**不要清库**，用 resume / `--no-resume` 按脚本约定续跑。
- 改了 `ai_core` 记忆 / 装配 / 闸门后必须**重启 Core** 再测。

统一入口也可：`uv run python eval/run_eval.py <benchmark> <stage> ...`（`longmem` / `beam` / `corpusqa` / `loft`）。

### A.1 Agent 硬核套件

入口：`python -m eval.agent.run`。发布线用 **pass^k=1**。

```powershell
uv run python -m eval.agent.selftest
uv run python -m eval.agent.run --dry-run
uv run python -m eval.agent.run `
  --base-url http://127.0.0.1:8765 `
  --token $env:GSUID_LOCAL_TEST_TOKEN `
  --k 1 --judge bot --concurrency 2 `
  --timeout 360 --delivery-wait 90 `
  --out eval/agent/results/_agent_k1.json
```

分块续跑：`eval.agent._chunked_run`。只把连接失败 / `session_log_not_found` 当传输故障重试；`max_latency`、静音、未完成是产品失败，留下。

### A.2 LongMemEval-S

数据：`eval/longmemeval/longmemeval_s_cleaned.json`（500 题）。协议：每题独立 `user_id=eval_{qid}`；`batch_observe` 摄入（**不** `--extract`）；作答 `history=[]`；`--inject-date` 把 `question_date` 放进 HTTP `clock_at`（禁止拼进问句进 system，§1.7）。

```powershell
# 库里已有 ingest 时
uv run python eval/run_eval.py longmem run-domains --tag prod7

# 只重跑一个域
uv run python eval/run_eval.py longmem run-domains --question-type multi-session --tag prod7

# 标记传输/空答失败后 resume（内容 FAIL 不改写）
uv run python eval/run_eval.py longmem mark-fails --tag prod7
uv run python eval/run_eval.py longmem run-domains --tag prod7
```

不要开 `GSUID_EVAL_MEMORY_FULL_SCOPE`。不要 `--clear-first` 除非有意毁掉该 scope。

### A.3 BEAM（官方 ladder：100k / 500k / 1m / 10m）

数据集在 `data/data/`（见 `eval/BEAM_official/README.md`）。每档用配套对话和探针；结果写 `eval/BEAM_official/results/{100k,500k,1m,10m}/`。

```powershell
# 单规模全流程（ingest + probe + judge）
uv run python eval/BEAM_official/run_official.py all --scale 100k

# 已灌库只重答
uv run python eval/BEAM_official/run_official.py reprobe --scale 100k

# 官方 ladder 顺序（100k → 500k → 1M → 10M）
uv run python eval/BEAM_official/run_official.py ladder
```

口径：评测助手 + `enable_tools` + `memory_eval=False` + `clock_at`。`user_id=beam_off_<scale>_<conv>`。

### A.4 CorpusQA

官方题库 Hugging Face `Tongyi-Zhiwen/CorpusQA`。按 domain 清库 → `batch_observe` 灌文档 → `chat_with_history` 作答 → 规则 + LLM 判分。不把 128k/1m 正文塞进当前轮。

```powershell
uv run python eval/corpusqa/run_corpusqa.py download --scale 128k
uv run python eval/corpusqa/run_corpusqa.py all --scale 128k --limit 40 --tag n40
uv run python eval/corpusqa/run_corpusqa.py all --scale 1m --limit 40 --tag n40

# 或统一入口
uv run python eval/run_eval.py corpusqa all --scale 128k --limit 40
```

常用：`--concurrency 4`；`--no-resume` 强制重跑；judge 可加 `--llm-fallback-on-rule-fail`。报告在 `eval/corpusqa/results/{128k,1m}/report_*.md`。

### A.5 LOFT（BEIR 检索 + RAG / GlobalQA）

数据：`eval/loft/run_loft.py download ...`。协议同 CorpusQA：灌记忆再作答。

```powershell
uv run python eval/loft/run_loft.py download --task retrieval --dataset scifact --length 128k
uv run python eval/loft/run_loft.py download --task rag --dataset nq --length 128k

uv run python eval/loft/run_loft.py all --task rag --dataset nq --length 128k
uv run python eval/loft/run_loft.py all --task retrieval --dataset scifact --length 128k

uv run python eval/run_eval.py loft all --task rag --dataset nq --length 128k
```

结果：`eval/loft/results/{rag,retrieval}/<dataset>/<length>/`。

### A.6 协议红线（各套件共用）

| 点 | 要求 |
|----|------|
| 前缀缓存 | 禁止中途改 `system_prompt`；动态内容进 user 侧（AGENTS.md §1.7） |
| 时钟 | `clock_at` 走 HTTP 字段；禁止从用户原文解析写进 system |
| 人格 / 能力 | 框架不写死口癖、不内置业务词表（§1.9） |
| 群聊 | 未点名走寻址门 / SILENCE；评测助手禁静音；工具回执不得写「不要 SILENCE」 |
| Agent vs RAG | Agent 考办事；LongMem / BEAM / CorpusQA / LOFT 考记忆与语料，分数不可横向比 |

### A.7 改代码后的回归面

| 你改了 | 先跑 |
|--------|------|
| 记忆检索 / 词面补条 | `tests/test_memory_set_recall.py` + `tests/test_memory_injection_quality.py` |
| `turn_pipeline.py` 时钟 | `tests/test_agent_kits_slots.py` |
| 双入口装配 / `clock_at` | `tests/test_context_assembly.py` |
| `interaction_scaffold.py` | `tests/test_interaction_scaffold.py` |
| 装配 / 闸门 / 每轮注入 | 单测绿不够，还要对照 `eval/agent` 群聊基准 |

```powershell
uv run pytest tests/test_memory_injection_quality.py tests/test_memory_set_recall.py tests/test_context_assembly.py tests/test_interaction_scaffold.py tests/test_eval_judge_parse.py tests/test_agent_kits_slots.py -q
```

单测全绿不能代替实机评测。交付闸仍按 `AGENTS.md`：`ruff` / `format` / `pytest tests` / `basedpyright`。
