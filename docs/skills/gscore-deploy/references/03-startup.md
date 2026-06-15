# 三、启动 Core

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[二、源码部署安装](./02-install.md) · **下一章**：[四、配置体系总览](./04-config-overview.md)

启动命令的**四种写法**完全等价，按你安装时用的包管理器选对应一条即可。
所有命令都需要在**仓库根目录**（含 `pyproject.toml`）执行。

## 3.0 启动命令速查

```sh
# 1) uv（推荐）
uv run core

# 2) poetry
poetry run core

# 3) pdm
pdm run core

# 4) 直接 python（兜底，不推荐）
python -m gsuid_core.core
```

第一次启动会：

1. 创建 `data/` 目录与 `data/config.json`（含随机 `REGISTER_CODE`）
2. 初始化 SQLite 数据库 `data/GsData.db`
3. 加载 `gsuid_core/plugins/` 下所有插件
4. 启动 WebSocket 服务 + WebConsole
5. 注册 APScheduler 定时任务（含自动更新 / 自动清理日志 / AI 定时任务）

## 3.1 命令行参数

源码 [`gsuid_core/core.py`](../../../gsuid_core/core.py) 的 `main()` 支持：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--dev` | flag | `False` | 启用开发模式（影响插件加载行为、日志详细度） |
| `--port PORT` | str | `None` | 临时覆盖 `config.json` 的 `PORT`（**不写回文件**） |
| `--host HOST` | str | `None` | 临时覆盖 `config.json` 的 `HOST`（**不写回文件**） |

```sh
# 监听所有网卡、端口 9527
uv run core --host 0.0.0.0 --port 9527

# 开发模式
uv run core --dev
```

> 命令行覆盖优先级：CLI > `config.json` > 默认值。
>
> `HOST` 特殊值映射（见 `core.py:108-110`）：
>
> | 写入值 | 实际行为 |
> |--------|----------|
> | `localhost` / `127.0.0.1` / `::1` | 仅本机可连 |
> | `0.0.0.0` | 监听全部网卡（公网部署必须） |
> | `all` / `none` / `dual` / 空字符串 | 等价于 `0.0.0.0` |

## 3.2 启动日志识别

正常启动会看到类似：

```
🧠 [GsCore] 切换HF地址，地址: https://hf-mirror.com
📀 [数据库] 开始初始化...
[GsCore] 启动WS服务中...
.------..------..------..------..------..------..------.
|G.--. ||S.--. ||-.--. ||C.--. ||O.--. ||R.--. ||E.--. |
... (ASCII 艺术)
          🌱 [早柚核心] 已启动! 版本 0.10.5 ！
🚀 [GsCore] 启动完成, 耗时: 1.23s, 版本: 0.10.5
📦 插件: 5 | 🛠️ 服务: 18 | ⚡ 触发器: 42
```

> AI 启用后会多一行：
> `🧠 AI工具: 12 | 🔗 Trigger工具: 5 | 🎭 人格: 3 | 📋 配置文件: 2`

## 3.3 守护进程 / 后台运行

### 3.3.1 Linux / macOS（systemd）

`/etc/systemd/system/gsuid_core.service`：

```ini
[Unit]
Description=GsCore
After=network.target

[Service]
Type=simple
User=gsbot
WorkingDirectory=/home/gsbot/gsuid_core
ExecStart=/home/gsbot/.local/bin/uv run core
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now gsuid_core
sudo systemctl status gsuid_core
sudo journalctl -u gsuid_core -f    # 实时日志
```

### 3.3.2 supervisor

`/etc/supervisor/conf.d/gsuid_core.conf`：

```ini
[program:gsuid_core]
command=/home/gsbot/.local/bin/uv run core
directory=/home/gsbot/gsuid_core
user=gsbot
autostart=true
autorestart=true
stdout_logfile=/var/log/gsuid_core.out.log
stderr_logfile=/var/log/gsuid_core.err.log
environment=PYTHONUNBUFFERED=1
```

### 3.3.3 Windows（计划任务 / NSSM）

- **临时后台**：`start /B uv run core`（但终端关掉会被杀）。
- **推荐 NSSM**（Service Wrapper）：
  ```bat
  nssm install GsCore "C:\Users\Wuyi\AppData\Roaming\uv\uv.exe" "run core"
  nssm set GsCore AppDirectory "E:\MyPyProject\gsuid_core"
  nssm set GsCore AppStdout "E:\MyPyProject\gsuid_core\data\logs\service.out.log"
  nssm set GsCore AppStderr "E:\MyPyProject\gsuid_core\data\logs\service.err.log"
  nssm start GsCore
  ```

### 3.3.4 Docker

见 [十二、Docker 部署 §12.4](./12-docker.md#124-容器生命周期管理)。

## 3.4 关闭 / 重启

启动后可以用 Bot 命令关 Core（master 权限）：

| 命令 | 行为 |
|------|------|
| `core重启` | 先保存 `global_val` → 触发 `restart_command`（默认 `uv run python`，可改） → 拉起新进程 |
| `gs关闭Core` | 保存状态后 `os._exit(0)`（依赖 systemd / Docker `--restart always` 自动拉起） |

> `core重启` 默认行为：
>
> - 不依赖 Docker / systemd；
> - 当前进程退出后由 Core 内置 `_restart` 协程拉起新进程；
> - **Windows 下需确保父进程不死**（推荐 NSSM / Docker）。

源码位置：
- `gsuid_core/buildin_plugins/core_command/core_restart/__init__.py`
- `gsuid_core/buildin_plugins/core_command/core_restart/restart.py`

## 3.5 启动失败常见原因（速查）

| 现象 | 原因 |
|------|------|
| `ModuleNotFoundError: gsuid_core` | 你不在仓库根目录 |
| `Address already in use` | 8765 被占用，改 `PORT` 或 `core --port` |
| `Permission denied: 8765` | Linux 1024 以下端口需 root |
| 启动后立刻退出 | 看 `data/logs/` 最近日志；通常是依赖缺失 |
| 启动成功但 WS 连不上 | `HOST` 没改 / 防火墙 / `WS_TOKEN` 不一致 |
| 数据库初始化报 `greenlet DLL load failed` | 见 [故障排查 §16.5](./16-troubleshooting.md#165-greenlet--sqlalchemy-报错) |

完整排查见 [十六、故障排查清单](./16-troubleshooting.md)。
