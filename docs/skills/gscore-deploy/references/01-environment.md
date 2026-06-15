# 一、环境与依赖

部署 GsCore 前先确认运行环境。本章给出所有依赖的硬性下限与推荐版本，并列出常见的
平台差异（Windows / Linux / macOS）。所有命令同时给出裸跑与三种包管理器（uv /
poetry / pdm）的检查方式，**任选其一**。

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **下一章**：[二、源码部署安装](./02-install.md)

## 1.1 Python 版本

- **硬性下限**：`>=3.11,<4.0`（来自 [`pyproject.toml`](../../../pyproject.toml) 的 `requires-python`）
- **推荐**：`3.12`（Docker 基础镜像 `gscore-uv-3.12`）
- **不建议**：`>=3.14`（部分依赖如 `greenlet` / `pydantic-ai-slim` 在新版 Python 上
  可能有 wheels 缺失问题）

**检查**：

```sh
python -V
# Python 3.12.x
```

**Windows 安装**：[python.org](https://www.python.org/downloads/windows/) 下载安装包，
安装时**务必勾选「Add Python to PATH」**。

**Linux（Debian/Ubuntu）**：

```sh
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip
```

**macOS**：

```sh
brew install python@3.12
```

> 关于 `greenlet`：在 Windows 上跑 MySQL 驱动（aiomysql / asyncmy）时常见错误
> `the greenlet library is required to use this function. DLL load failed while
> importing _greenlet: 找不到指定的模块`。解决：
>
> ```sh
> uv pip install greenlet
> uv pip install msvc-runtime   # Windows 必需
> ```
>
> 见 [十六、故障排查 §16.5](./16-troubleshooting.md#165-greenlet--sqlalchemy-报错)。

## 1.2 Git

**硬性下限**：`>=2.0`。

**检查**：

```sh
git -v
# git version 2.42.x
```

**安装**：

- Windows：[git-scm.com](https://git-scm.com/) 下载安装包
- Linux：`sudo apt install -y git`
- macOS：`brew install git`（或 `xcode-select --install`）

## 1.3 包管理器（三选一即可，推荐 uv）

GsCore 的依赖由 [`pyproject.toml`](../../../pyproject.toml) 描述，安装方式取决于你
用的包管理器。**三种完全等价，按喜好选**：

| 包管理器 | 推荐场景 | 检查命令 | 安装 |
|----------|----------|----------|------|
| **uv** 🥳 推荐 | 速度最快、依赖解析稳 | `uv -V` | `pip install uv` 或 [astral.sh/uv](https://docs.astral.sh/uv/) |
| **poetry** | 历史项目多、生态熟 | `poetry -V`（需 >=1.4.0） | `pip install poetry` |
| **pdm** | PEP 582 友好 | `pdm -V` | `pipx install pdm` |
| **裸 pip** 😡 不推荐 | 仅作为兜底 | `pip -V` | 自带 |

**注意**：

- **poetry** 在某些 Linux 桌面环境（keyring 后端）下 install 会报 `DBusErrorResponse`，
  解决：`export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` 后重试。
- **pdm** 必须用 `pipx` 或独立 venv 装，避免与系统 pip 打架。
- **uv** 自带 PEP 723 / PEP 751 / Python 版本管理，无需预装 Python。

## 1.4 Playwright 与 Chromium（可选）

Core 内置 HTML 渲染管线（`pyhtmlrender` / `playwright`）。源码裸跑首次启动会**自动
下载 Chromium**（约 130MB），慢的话可手动加速：

```sh
# 单独触发浏览器下载
uv run playwright install chromium

# 国内加速（仅首次下载有效）
uv run playwright install chromium --with-deps
# 或者设置环境变量
export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/
```

Docker 镜像 **已预装**，无需处理。

## 1.5 系统资源

| 资源 | 最低 | 推荐 | 说明 |
|------|------|------|------|
| CPU | 1 核 | 2 核+ | AI 启用 Rerank 时吃 CPU |
| 内存 | 1 GB | 2 GB+ | AI 嵌入模型（local）加载约 300MB；Rerank 模型约 1GB |
| 磁盘 | 2 GB | 10 GB+ | 资源 / 数据库 / 日志 / 备份 |
| 网络 | 出站 HTTPS | — | 米游社 / 插件上游 API / GitHub / HuggingFace |

## 1.6 端口与防火墙

- **默认监听**：`8765/tcp`（FastAPI + WebSocket），可通过 `config.json` 的 `PORT` 改。
- **HTTP 模式（可选）**：`ENABLE_HTTP=true` 后额外暴露 `/api/send_msg` POST 接口。
- **公网部署**：放行对应端口；建议套反向代理（Nginx / Caddy）。

## 1.7 文件系统编码

- Windows 默认 GBK 用户把 Bot 跑成控制台中文乱码时，设：
  ```bat
  set PYTHONIOENCODING=utf-8
  set PYTHONUTF8=1
  ```
- Linux / macOS 默认 UTF-8，无问题。

## 1.8 常见前置库

部分 OS 的精简镜像需要补包：

```sh
# Debian/Ubuntu 跑 Playwright 时
sudo apt install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
                    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
                    libgbm1 libpango-1.0-0 libcairo2 libasound2t64

# CentOS / RHEL
sudo yum install -y nss atk cups-libs libXcomposite libXdamage libXrandr \
                    mesa-libgbm pango cairo alsa-lib
```

Docker 镜像已包含，可忽略。

## 1.9 检查清单（部署前最后一遍过）

- [ ] `python -V` 显示 `3.11` ~ `3.13`
- [ ] `git -v` 显示 `>=2.0`
- [ ] 已选 uv / poetry / pdm 中的一个并能正常调用
- [ ] 8765（或自定义）端口未被占用：`netstat -ano | findstr 8765` / `lsof -i:8765`
- [ ] 磁盘剩余 ≥ 2 GB
- [ ] （可选）能访问 GitHub / HuggingFace；不能就准备好代理或镜像源

> 满足以上条件即可进入 [二、源码部署安装](./02-install.md) 或 [十二、Docker 部署](./12-docker.md)。
