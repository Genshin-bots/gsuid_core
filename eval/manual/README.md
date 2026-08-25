# 手工 WS / 出图脚本（不是 pytest）

正式实机评测入口是 `eval/agent/`。这里是从 `tests/` 移出的手写客户端：需要先 `uv run core`，通过条件含当前人格口癖或出图，**不要**放进默认 `pytest`。

```powershell
$env:GSUID_LOCAL_TEST_TOKEN = "<token>"
$env:GSUID_LOCAL_TEST_MODE = "1"
uv run python eval/manual/e2e_quick.py
```

`GSUID_LOCAL_TEST_TOKEN` 必填，文件内无密钥 fallback。图片写到仓库根目录 `test_output/`（已 gitignore）。
