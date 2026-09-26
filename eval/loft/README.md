# LOFT（BEIR 检索 + RAG / GlobalQA）

[LOFT](https://github.com/google-deepmind/loft)（arXiv:2406.13121）测超长上下文上的检索与 RAG。GsCore 不把 32k/128k/1M 语料塞进单轮 prompt，而是灌入记忆再作答。

| 任务 | 对应 | 金标 | 主指标 |
|------|------|------|--------|
| `--task retrieval` | LOFT 文本检索 = **BEIR 子集**（SciFact / NQ / FiQA / …） | passage id | recall@1 |
| `--task rag` | LOFT RAG = **GlobalQA**（语料级问答：NQ / HotPotQA / …） | 短答案 | 子串 EM，可选 LLM |

数据来自 `https://storage.googleapis.com/loft-bench/{retrieval\|rag}/{dataset}.zip`，每套含 `32k` / `128k` / `1m` 语料与 100 道 test。

需要已启动、且 `GSUID_LOCAL_TEST_MODE=1` 的 core。

```sh
uv run python eval/loft/run_loft.py download --task retrieval --dataset scifact --length 128k
uv run python eval/loft/run_loft.py all --task retrieval --dataset scifact --length 128k --limit 20
uv run python eval/loft/run_loft.py all --task rag --dataset nq --length 128k --limit 20
uv run python eval/run_eval.py loft all --task rag --dataset nq --length 128k --limit 20
```

`--length 1m` 换 LOFT 百万档语料。结果在 `eval/loft/results/`（gitignored）。原始 zip / jsonl 在 `eval/loft/data/`，也不进 git。
