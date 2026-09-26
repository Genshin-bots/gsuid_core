# CorpusQA（语料级分析）

[CorpusQA](https://github.com/Tongyi-Zhiwen/CorpusQA)（arXiv:2601.14952）测的是 **corpus-level analysis**：证据打散在上百页文档里，答案要靠过滤、跨文档比较和统计聚合，不能靠「检索出两三块就拼出来」。

官方题库在 Hugging Face [`Tongyi-Zhiwen/CorpusQA`](https://huggingface.co/datasets/Tongyi-Zhiwen/CorpusQA)：`128k_4domains.jsonl`（329 题，约 100MB）和 `1m_4domains.jsonl`（329 题，约 2GB）。四域：`education_en` / `financial_en` / `financial_zh` / `real_estate_en`。金标由 SQL 在结构化表上算出，含合法空列表 `[]`。

GsCore 不把 128k token 塞进单轮 prompt。评测协议：

1. **ingest**：每个 domain 一份共享语料 → `batch_observe` 写入 `user_global:eval_cqa_{scale}_{domain}`
2. **probe**：`/api/chat_with_history`，人格 **评测助手**，`enable_tools=True`，`memory_eval=False`
3. **judge**：数字 / 列表先规则匹配；规则失败再走 LLM（官方 ORM 口径加 `--llm-fallback-on-rule-fail`）

需要已启动、且 `GSUID_LOCAL_TEST_MODE=1` 的 core（不要 `--dev`）。

```powershell
$env:GSUID_LOCAL_TEST_MODE="1"
$env:GSUID_LOCAL_TEST_TOKEN="<token>"
$env:PYTHONUTF8="1"
$env:NO_PROXY="localhost,127.0.0.1"
uv run core --port 8765
```

```sh
uv run python eval/corpusqa/run_corpusqa.py download --scale 128k
uv run python eval/corpusqa/run_corpusqa.py ping
uv run python eval/corpusqa/run_corpusqa.py smoke --scale 128k
uv run python eval/corpusqa/run_corpusqa.py all --scale 128k --limit 40
uv run python eval/run_eval.py corpusqa all --scale 128k --limit 40

# 1M 档（同一套脚本，语料更大，灌入更久）
uv run python eval/corpusqa/run_corpusqa.py download --scale 1m
uv run python eval/corpusqa/run_corpusqa.py all --scale 1m --limit 20
```

`--limit N` 按四域轮询抽样。去掉 `--limit` 即跑该档全部题目（128k 为 329 题）。国内下载可设 `HF_ENDPOINT=https://hf-mirror.com`。

结果在 `eval/corpusqa/results/{scale}/`（gitignored）：`answers_*.json` / `judge_*.json` / `report_*.md`。原始 jsonl 与抽好的文档缓存也不进 git。

## 实测（2026-09-26）

128k 分层 40 题（每域 10），生产 Chat，语料已按域灌入。HTTP 40/40。

| 口径 | 分数 |
|------|------|
| 规则过则过、失败再 LLM（对齐官方 ORM） | **16/40 = 40.0%**（`report_n40_orm.md`） |
| 规则-only | 14/40 = 35.0%（`report_n40.md`） |

education_en 6/10，real_estate_en 4/10，financial_en 4/10，financial_zh 2/10。全量 329 题用同一脚本去掉 `--limit`。
