# 二、源码部署安装

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[一、环境与依赖](./01-environment.md) · **下一章**：[三、启动 Core](./03-startup.md)

本章讲解**源码裸跑**方式的安装流程。Docker 用户请直接看 [十二、Docker 部署](./12-docker.md)。

## 2.1 目录建议

```
~/
├── Bots/                    # 上游 Bot 目录（NoneBot2 / AstrBot / ...）
│   └── nb2/
└── gsuid_core/              # Core 目录（与 Bots/ 同级或任意位置都行）
```

> 之所以建议 Core 与 Bot **同级**（如 `Bots/nb2/` 与 `gsuid_core/`），是因为两个端
> 之间用 WS 通信，目录位置无强制关系；但「Core 与 Bot 各自的 `data` 目录」要分清，
> 互不影响。

## 2.2 克隆仓库

在 Core 目录的上级执行：

```sh
# GitHub
git clone https://github.com/Genshin-bots/gsuid_core.git --depth=1 --single-branch

# 或国内镜像（更快）
git clone https://cnb.cool/gscore-mirror/gsuid_core.git --depth=1 --single-branch

cd gsuid_core
```

> `--depth=1 --single-branch` 只拉默认分支最近一次提交，省时省空间；后续用
> `core更新` / `git pull` 拉新版本即可。
>
> 网络抖动 / 拉不动时设代理：
> ```sh
> # 全局
> git config --global http.proxy http://127.0.0.1:7890
> # 仅本次
> git -c http.proxy=http://127.0.0.1:7890 clone ...
> ```

## 2.3 安装依赖（四种包管理器任选其一）

> ⚠️ 无论选哪种，都建议**在仓库根目录**（含 `pyproject.toml`）执行。

### 2.3.1 【🥳 推荐】uv

```sh
uv python install 3.12          # 下载 Python 3.12（如已装可跳）
uv sync --python 3.12          # 创建 .venv 并安装依赖
uv run python -m ensurepip     # 兜底补 pip（部分插件命令会用到）
```

`uv sync` 默认产物：

- 虚拟环境：`.venv/`
- 锁文件：`uv.lock`

### 2.3.2 poetry

```sh
poetry install
```

- 虚拟环境：`.venv/`（或 Poetry 自定义路径）
- 锁文件：`poetry.lock`

> 若报 `DBusErrorResponse`：`export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring`
> 后重试。

### 2.3.3 pdm

```sh
pdm install
pdm run python -m ensurepip   # 兜底补 pip
```

- 虚拟环境：`.venv/`
- 锁文件：`pdm.lock`

### 2.3.4 【😡 不推荐】直接 pip

```sh
python -m pip install -r requirements.txt
```

> 该方式不会读 `pyproject.toml` 的可选依赖组，且容易和系统包冲突，仅作为兜底。
> 缺包时 Core 启动直接报错，再补。

## 2.4 安装第一颗插件（可选但建议）

Core 本身只是个壳，**没有任何业务功能**；必须装至少一颗业务插件（如 GenshinUID
v4）才有命令可用。

```sh
cd gsuid_core
cd plugins

# v4 GenshinUID（推荐先装这个验证链路）
git clone -b v4 https://github.com/KimigaiiWuyi/GenshinUID.git --depth=1 --single-branch

# 或 StarRailUID
git clone https://github.com/baiqwerdvd/StarRailUID.git --depth=1 --single-branch

cd ../
```

也可以启动后用 master 账号在 Bot 里发 `core安装插件GenshinUID`（详见
[九、插件管理体系](./09-plugins.md)）。

## 2.5 验证安装

```sh
# uv
uv run core --help

# poetry
poetry run core --help

# pdm
pdm run core --help

# pip
python -m gsuid_core.core --help
```

正常输出：

```
usage: core [-h] [--dev] [--port PORT] [--host HOST]
```

## 2.6 安装位置速查

| 路径 | 含义 |
|------|------|
| 仓库根 | 你 `git clone` 的目录，含 `pyproject.toml` |
| `gsuid_core/` | Core 自身的 Python 包（不是数据目录！） |
| `gsuid_core/plugins/` | 业务插件目录，每颗插件一个子目录 |
| `data/` | **运行时生成**，存放数据库 / 配置 / 日志 / 资源 / 主题 |
| `data/GsData.db` | SQLite 数据库（默认） |
| `data/config.json` | Core 全局配置 |
| `data/core_config.json` | Core 行为配置（自动更新 / 风控 / 代理...） |
| `data/plugins_configs/` | 每颗插件的独立配置文件 |
| `data/ai_core/` | AI 核心配置目录 |
| `data/logs/` | 日志（保留 8 天） |
| `data/GenshinUID/` | 业务插件数据示例（含 resource / players / bg...） |

> 完整目录结构：[十五、数据目录与路径速查](./15-data-layout.md)

## 2.7 安装失败常见原因

| 现象 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError: aiomysql` | 切 MySQL 时忘装驱动 | `uv pip install aiomysql` 或 `asyncmy` |
| `greenlet DLL load failed` | Windows 缺 msvc-runtime | `uv pip install greenlet msvc-runtime` |
| `playwright install` 超时 | 下载 Chromium 受限 | 设 `PLAYWRIGHT_DOWNLOAD_HOST` 或挂代理 |
| `DBusErrorResponse` | poetry keyring 抢锁 | `export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` |
| `pip` 找不到 `pyproject.toml` | 你没在仓库根目录 | `cd gsuid_core` 后再装 |

更多：[十六、故障排查清单](./16-troubleshooting.md)
