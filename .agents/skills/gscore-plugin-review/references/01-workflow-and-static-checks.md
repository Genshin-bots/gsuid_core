# 一、审核流程、静态扫描脚本与报告模板

> 返回 [SKILL 主入口](../SKILL.md)

审核插件时遵循**先客观取证、再逐域深读、最后分级定论**的顺序。静态扫描负责快速锁定可疑点与零漏项盘点，**不能替代人工精读安全、数据库与 AI 关键代码**。

## 1.1 准备与隔离获取

1. **禁止直接把待审插件放进工作区 `gsuid_core/plugins/`**：避免污染当前 Git 工作树或触发自动导入副作用。
2. **浅克隆到会话临时目录（`scratch/`）**，同时记录基准 commit、文件数、总体积与扩展名分布：

```powershell
# PowerShell（Windows）示例
$dest = "<scratch_dir>\TargetPlugin"
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
git clone --depth 5 <repo_url> $dest
git -C $dest log -n 5 --oneline

$files = Get-ChildItem -Recurse -File $dest | Where-Object { $_.FullName -notmatch '\\\.git\\' }
$files | Measure-Object -Property Length -Sum | ForEach-Object {
    "Files: $($_.Count), Total MB: $([math]::Round($_.Sum / 1MB, 2))"
}
$files | Group-Object Extension | Sort-Object Count -Descending | Format-Table Name, Count, @{
    N = 'MB'; E = { [math]::Round(($_.Group | Measure-Object Length -Sum).Sum / 1MB, 2) }
} -AutoSize
```

**体积预警线**：
- 常规代码插件仓库建议 ≤ 10 MB；若超过 30 MB 或含大量无压缩 PNG / 音频 / 模型权重，检查是否应拆分外置资源下载或压缩素材。
- 检查 `.git` 外是否混入 `.env`、`config.json`、`*.db`、`*.sqlite`、`__pycache__/`、临时测试数据或私密密钥。

## 1.2 自动化静态扫描脚本

在 GsCore 仓库根目录下运行以下 4 类检查（可合并进单次命令，减少回合）。

### ① AST 综合审计（依赖、红线、外链、Py3.12 语法、触发器与状态字典）

```python
import ast
import pathlib
import re
import sys

root = pathlib.Path(r"<dest>\<InnerPkg>")
stdlib = sys.stdlib_module_names
imports: set[str] = set()
gscore_imports: set[tuple[str, str | None]] = set()
hosts: set[str] = set()

hits: dict[str, list[str]] = {
    "Any": [],
    "cast": [],
    "type_ignore": [],
    "getattr": [],
    "requests": [],
    "sync_open": [],
    "tablename_def": [],
    "active_bot": [],
    "target_send": [],
    "uvicorn_or_fastapi_new": [],
    "py312_only": [],
    "bare_or_broad_except": [],
    "global_state_dicts": [],
}

for p in sorted(root.rglob("*.py")):
    rel = p.relative_to(root).as_posix()
    src = p.read_text(encoding="utf-8-sig")

    for i, line in enumerate(src.splitlines(), 1):
        if "type: ignore" in line or "pyright: ignore" in line:
            hits["type_ignore"].append(f"{rel}:{i}")
        if re.search(r"^\s*__tablename__\s*=", line):
            hits["tablename_def"].append(f"{rel}:{i}")
        if "active_bot" in line:
            hits["active_bot"].append(f"{rel}:{i}")
        if ".target_send(" in line:
            hits["target_send"].append(f"{rel}:{i}")
        if "uvicorn" in line or "FastAPI(" in line:
            hits["uvicorn_or_fastapi_new"].append(f"{rel}:{i}")
        if "onexc=" in line or "itertools.batched" in line:
            hits["py312_only"].append(f"{rel}:{i}: {line.strip()}")
        if re.search(r"^_[a-z_]*(lock|cache|task|token|session)s?\s*:", line):
            hits["global_state_dicts"].append(f"{rel}:{i}: {line.strip()}")

    for u in re.findall(r"https?://[A-Za-z0-9._:/-]+", src):
        hosts.add("/".join(u.split("/")[:3]))

    tree = ast.parse(src, filename=rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.TypeAlias):
            hits["py312_only"].append(f"{rel}:{node.lineno}: PEP695 type alias")
        elif isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                imports.add(top)
                if top == "gsuid_core":
                    gscore_imports.add((a.name, None))
                if top == "requests":
                    hits["requests"].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            imports.add(top)
            if top == "gsuid_core":
                for a in node.names:
                    gscore_imports.add((node.module, a.name))
            if top == "requests":
                hits["requests"].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.Name):
            if node.id in ("Any", "cast", "getattr"):
                hits[node.id].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                hits["sync_open"].append(f"{rel}:{node.lineno}")
        elif isinstance(node, ast.Try):
            for h in node.handlers:
                etype = ast.unparse(h.type) if h.type else "BARE"
                if etype in ("BARE", "Exception", "BaseException"):
                    hits["bare_or_broad_except"].append(f"{rel}:{h.lineno}: except {etype}")

third_party = sorted(i for i in imports if i not in stdlib and i not in ("gsuid_core", root.name))
print("THIRD_PARTY:", third_party)
print("EXTERNAL_HOSTS:", sorted(hosts))
for k, v in hits.items():
    print(f"{k}: {len(v)}", v[:10])
```

### ② `gsuid_core.*` 导入有效性实测

对照当前仓库的真实 `gsuid_core` 逐项验证模块与属性是否存在，防止插件引用已删除/改名的内部 API：

```python
import importlib

bad: list[str] = []
for mod, attr in sorted(gscore_imports):
    try:
        m = importlib.import_module(mod)
        if attr and not hasattr(m, attr):
            bad.append(f"MISSING ATTR: {mod}.{attr}")
    except Exception as exc:
        bad.append(f"IMPORT ERR: {mod}.{attr} -> {type(exc).__name__}: {exc}")
print(f"Checked {len(gscore_imports)}, failures={len(bad)}", bad)
```

### ③ Ruff 与 BasedPyright 检查

- 用插件自带 `ruff.toml`（若缺失则用 Core 根配置）跑 `uv run --dev ruff check`。
- 生成临时 `pyright_check.json`（放 `scratch/`，勿写入仓库），将 `extraPaths` 指向 Core 根目录后执行 `uv run --dev basedpyright -p <tmp_cfg>`：

```json
{
  "include": ["TargetPlugin/TargetPlugin"],
  "extraPaths": ["f:/AIProject/gsuid_core"],
  "typeCheckingMode": "basic",
  "pythonVersion": "3.11"
}
```

> **提示**：检查 `ruff format --check` 时，在 Windows 上需留意 Git `core.autocrlf` 导致的 CRLF 假差异，先用 `git ls-files --eol` 确认仓库实际换行符，避免误报。

### ④ 危险调用与敏感信息专项排查

重点 grep 检查以下模式（命中即必须人工精读上下文）：
- 命令/代码执行：`subprocess`、`os.system`、`eval(`、`exec(`、`__import__`、`pickle.loads`、`yaml.load(`（未用 `safe_load`）
- 文件/路径：`shutil.rmtree`、`unlink`、`Path(` 拼接用户输入、`file://`
- 网络与模板：`verify=False`（关闭 TLS 校验）、`ipify` / `httpbin` / 外网 IP 探测、`| safe`、`<script`、`innerHTML`
- 凭据与隐私：`AppSecret` / `api_key` / `token` / `password` / `cookie` / `sign`、写死个人 QQ 号或群号

## 1.3 避免误报的核验原则

审核结论直接决定作者返工成本，必须避免三类常见误报：

| 疑似现象 | 必须核验的事实 |
|----------|---------------|
| 代码里出现 `__tablename__` | 区分「定义 `__tablename__ = "..."`」（违规）与「在迁移脚本中只读 `Model.__tablename__`」（合法） |
| `bot.send(image_bytes)` 未显式调用 `convert_img` | 查 `gsuid_core/bot.py` 与 `segment.py`：框架 `convert_message` / `MessageSegment.image` 已原生支持 `bytes` 与图床上传，不算缺陷 |
| `@scheduler.scheduled_job(..., id=...)` 未写 `replace_existing=True` | 查 `reload_plugin.py`：新版 Core 热重载已先摘除该插件 job；建议补充以兼容旧 Core，定级为 P1/小项而非 P0 |
| 模块中存在 `try/except` | 区分「掩盖内部类型/逻辑 bug 的兜底」（违规）与「捕获网络 `httpx.HTTPError` / 解析不可信 JSON / `_ai_return_xxx` / 定时任务顶层兜底」（合法） |

## 1.4 审核报告输出模板

向维护者或插件作者交付结论时，统一使用以下结构：

```markdown
### 一、总体结论
- 审核基准：`<repo_url>` @ `<commit_short>`（文件数 / 体积 / 静态检查结果）
- 商店准入结论：**适合直接上架** / **修复 P0 后适合上架** / **暂不建议上架**
- 分维度评估表（目录与兼容 / 代码规范 / 数据与资源 / 数据库与配置 / Web与隐私 / AI集成 / 帮助与文档）

### 二、亮点与合规项（简述）
- 客观肯定已达标的工程实践，避免只挑刺。

### 三、P0 阻断项（上架前必须修复）
每条必须包含：
1. 问题标题与归类标签（如 `【安全/隐私】`、`【Python 3.11 兼容】`）
2. 精确文件与行号链接
3. 触发条件与实际危害（可附框架源码依据）
4. 具体、可落地的修改方案

### 四、P1 强烈建议修复项
（潜在运行时 bug、性能/内存泄漏、异步/类型规范、文档与代码不一致）

### 五、P2 运营合规与上架提示
（第三方依赖风险、`installMsg` 建议文案、最低 Core 版本说明、测试覆盖建议）
```
