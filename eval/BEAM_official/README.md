# 官方 BEAM ladder（100k / 500k / 1M / 10M）

数据集在 `data/data/`。共用 ingest/probe/judge 在 `eval.common.beam_runner`。

```sh
# 官方 ladder（默认 100k → 500k → 1M → 10M）
uv run python eval/BEAM_official/run_official.py ladder

# 单档
uv run python eval/BEAM_official/run_official.py all --scale 100k
uv run python eval/BEAM_official/run_official.py reprobe --scale 100k

# 10M CLI（plans 1–10）
uv run python eval/BEAM_official/run_beam_eval.py all --conv 0 --plans 1
```

需要 `GSUID_LOCAL_TEST_MODE=1` 的已启动 core。结果写在 `results/{100k,500k,1m,10m}/`。
