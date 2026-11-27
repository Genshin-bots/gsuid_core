# 二、目录结构、打包依赖与兼容性

> 返回 [SKILL 主入口](../SKILL.md)

本节检查插件能否被 GsCore 正确发现、安装依赖、加载与热重载，以及能否在商店用户常见的不同操作系统与 Python 版本中正常运行。

## 2.1 目录结构与嵌套加载规范

对照 [01-plugin-basics.md](../../gscore-plugin-development/references/01-plugin-basics.md) 核查：

| 检查项 | 级别 | 合规要求 | 常见违规 / 风险 |
|--------|------|---------|----------------|
| 外层入口 | P0 | `外层 __init__.py` + `外层 __nest__.py`（嵌套模式）+ `内层 <同目录名>/__init__.py` + `内层 __full__.py` | 内层包名与仓库目录名不一致导致加载路径错乱；漏放 `__nest__.py` 导致子模块未加载 |
| `Plugins(...)` 声明 | P0 | 内层 `__init__.py` 声明单例 `Plugins(name=..., force_prefix=[...], allow_empty_prefix=False)` | `allow_empty_prefix=True` 且无前缀，导致高频中文命令与其他插件撞车 |
| `version.py` 与 `ICON.png` | P1 | 内层含 `version.py`，根目录含正方形 `ICON.png`（商店封面与 WebConsole 展示用） | 缺失 `ICON.png` 导致 `plugin_list.json` 封面失效或帮助图报错 |
| 子模块划分 | P1 | 业务目录用 `{prefix}_{feature}/`，公共工具放 `utils/`，配置放 `{prefix}_config/` | 顶层散落几十个平铺脚本，难以维护或触发循环导入 |
| 基础设施插件（meta） | P1 | 声明 `[tool.gsuid] kind = "meta"` 时对外接口须收口在 `api/`；软依赖其他 meta 须用 `import_api()` 而非 `try/except ImportError` | 顶层硬 import 可选 meta 插件导致未装依赖时整包加载失败 |
| 独立工程配置 | P1 | 根目录含 `ruff.toml`（**必须排除 `pyproject.toml` 和 `**/*.toml`**）、建议带 `.vscode/` 与 `pyrightconfig.json` | `ruff.toml` 未排除 `pyproject.toml`，在根目录跑 ruff 时误扫 TOML 报错 |
| 开源协议与仓库洁净 | P1 | 含 `LICENSE`、合理的 `.gitignore`；仓库内无 `__pycache__`、`.db`、运行时日志或私钥文件 | 把个人配置/Cookie/测试截图提交进仓库 |

## 2.2 `pyproject.toml` 依赖审计

GsCore 在 [server.py](../../../../gsuid_core/server.py) 的 `check_pyproject()` 中读取 `[project].dependencies` 并合并自动安装。审核时必须做**代码 import ↔ `pyproject.toml` ↔ Core 基础依赖**三方比对：

1. **漏声明第三方依赖（P0）**：代码 `import` 了非标准库且不在 `gsuid_core/pyproject.toml` 中的包（例如 `pypinyin`、`bs4`、`pycryptodome`、`playwright`），但插件 `pyproject.toml` 未声明 → 新用户安装后直接 `ModuleNotFoundError`。
2. **重型依赖警示（P1）**：若声明了 `playwright`、`torch`、`opencv-python` 等重型或需后置安装二进制的依赖：
   - 确认是否确有必要（能用 `PIL` / 内置 `pytakumi` 渲染的卡片不应拉 `playwright`）；
   - 若确需 `playwright`，README 与商店 `installMsg` **必须**写明 `playwright install chromium` 步骤。
3. **版本约束冲突（P0）**：严禁把 Core 基础依赖（如 `pydantic`、`fastapi`、`starlette`、`sqlmodel`、`httpx`、`pillow`）锁死在窄版本或旧版本（如 `pydantic==2.5.0`），否则会破坏 Core 及其他插件环境。
4. **锁文件与 `tool.pdm` / `uv.lock`**：可存在，但不得依赖本地路径依赖（`file://` / 相对路径 wheel）。

## 2.3 Python 3.11 兼容性陷阱（高频 P0）

> **警告**：GsCore 本身声明 `requires-python = ">=3.11,<4.0"`，且 `check_pyproject()` **只解析 `dependencies`，完全不校验 `requires-python`**。商店用户中存在大量 Python 3.11 环境。

审核必须逐项排查以下 Python 3.12+ 独占写法：

| Python 3.12+ 特性 | 在 Python 3.11 下的表现 | 兼容改法 |
|---|---|---|
| `shutil.rmtree(path, onexc=...)` | 运行时抛 `TypeError: rmtree() got an unexpected keyword argument 'onexc'` | 按 `sys.version_info >= (3, 12)` 分支传 `onexc=` / `onerror=` |
| `type Alias = ...`（PEP 695）与 `def f[T](...)` 泛型语法 | import 时直接 `SyntaxError`，整个插件加载失败 | 改用 `TypeAlias` 与 `TypeVar` |
| `itertools.batched` | `ImportError` | 自行切片 `range(0, len(seq), size)` |
| `datetime.UTC`（3.11 已有，安全） / `f"{f'nested'}"` 嵌套同引号 f-string（3.12 新增） | 同引号嵌套 f-string 在 3.11 下直接 `SyntaxError` | 外层双引号、内层单引号或先赋临时变量 |
| `@typing.override` | `ImportError`（3.11 无 `typing.override`） | 从 `typing_extensions` 导入或省略 |

**真实案例（`CalabiyauUID`）**：
- `pyproject.toml` 写了 `requires-python = ">=3.12"`，但全仓仅 [resource_sync.py:133](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/utils/helper/resource_sync.py#L133) 一处用了 3.12 专属参数 `shutil.rmtree(path, onexc=_retry_writable)`。
- 一旦 3.11 用户从商店安装，启动资源同步清理目录时就会直接报错。改为兼容分支并将 `requires-python` 放宽到 `>=3.11` 即可覆盖全部 Core 用户。

## 2.4 Core 版本要求与跨平台兼容

1. **最低 Core 版本核实（P2）**：检查插件用到的较新框架能力（如 `pytakumi` 的 `root_max_width` / `allow_refit`、`AgentNode`、`with_read_session`、`register_backup_path`、`register_plugin_page`、`on_meta`），核对 README 标注的 `gsuid_core` 最低版本是否属实；对可选新能力（如 `register_backup_path`）建议做 `ImportError` 优雅跳过。
2. **Windows / Linux 跨平台兼容（P1）**：
   - 路径拼接一律用 `pathlib.Path`（禁硬编码 `/` 或 `\` 字符串拼接）；
   - 读写文本文件显式指定 `encoding="utf-8"`（Windows 默认 GBK 会崩）；
   - 原子替换用 `os.replace(tmp, dst)`（而非 `Path.rename` 覆盖已存在文件——Windows 上 `rename` 目标已存在会抛 `FileExistsError`）；
   - 删除含只读文件的目录（如 `.git` pack 文件）需先去除只读属性再删。
3. **字体与系统命令依赖（P1）**：PIL 绘图优先用 `core_font(size)`，禁止硬编码 `/usr/share/fonts/...` 或 `C:\Windows\Fonts\...`；若调用 `git` 等外部二进制，不可用时须降级提示，不得阻断 Core 启动。
