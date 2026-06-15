# 十六、故障排查清单

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十五、数据目录与路径速查](./15-data-layout.md) · **下一章**：[十七、常用内置命令速查](./17-commands.md)

本章按"症状 → 排查 → 解决"组织，覆盖部署 GsCore 时最常见的报错。

> 用户 FAQ：https://docs.sayu-bot.com/Extra/FAQ.html

## 16.0 通用排查流程

```sh
# 1. 看日志（最近 200 行）
tail -200 data/logs/<最新日志文件>

# 或实时跟
tail -f data/logs/<最新日志文件>

# 2. 把日志级别调成 DEBUG，看更详细的信息
#    改 data/config.json：
#    "log": { "level": "DEBUG", ... }
#    重启 Core

# 3. 看启动日志中的关键行
#    📀 [数据库] 开始初始化...
#    [GsCore] 启动WS服务中...
#    🚀 [GsCore] 启动完成, 耗时: ..., 版本: ...
#    📦 插件: ... | 🛠️ 服务: ... | ⚡ 触发器: ...
#    🧠 AI工具: ... | 🔗 Trigger工具: ... | 🎭 人格: ... | 📋 配置文件: ...   ← AI 启用时
```

## 16.1 Core 启动失败

### 16.1.1 `ModuleNotFoundError: gsuid_core`

**原因**：没在仓库根目录运行。

**解决**：

```sh
cd gsuid_core
uv run core
```

### 16.1.2 `JSONDecodeError` 启动失败

**原因**：`data/config.json` 或 `data/core_config.json` JSON 损坏。

**解决**：

```sh
# 验证
python -c "import json; json.load(open('data/config.json'))"

# 恢复：从备份或 git 还原
git checkout data/config.json  # 如果有备份

# Core 启动时会用 update_config() 给缺失字段补默认，
# 但坏 JSON 不会自动恢复
```

### 16.1.3 `Address already in use`

**原因**：8765 端口被占用。

**解决**：

```sh
# 找占用进程
# Linux/macOS
lsof -i:8765
# Windows
netstat -ano | findstr 8765

# 杀掉占用进程，或换端口
# config.json 改 PORT，或命令行 core --port 9527
```

### 16.1.4 `Permission denied: 8765`

**原因**：Linux 1024 以下端口需 root。

**解决**：用 ≥ 1024 的端口（如 8765 / 9527 / 28000）。

## 16.2 数据库报错

### 16.2.1 找不到 SQLite 数据库 / 表

**症状**：`no such table: xxx`

**原因**：数据库没初始化或插件版本太旧。

**解决**：

```sh
# 删 db 重启，Core 会按当前插件的 BaseModel 自动建
systemctl stop gsuid_core  # 或 core关闭Core
rm data/GsData.db
systemctl start gsuid_core
```

### 16.2.2 切 MySQL 后连不上

| 现象 | 原因 / 解决 |
|------|-------------|
| `Can't connect to MySQL server` | `db_host` / `db_port` 错 / 防火墙 / MySQL 没启 |
| `Access denied for user` | `db_user` / `db_password` 错 |
| `Unknown database 'GsData'` | 没建库，先 `CREATE DATABASE GsData CHARACTER SET utf8mb4` |
| `No module named 'aiomysql'` | 没装驱动，`uv pip install aiomysql` 或 `asyncmy` |
| 中文乱码 | 库用 `utf8mb4` 编码；连接串加 `?charset=utf8mb4` |

详见 [十一、§11.3](./11-database.md#113-切到-mysql)。

### 16.2.3 `greenlet DLL load failed`

**症状**：

```
ValueError: the greenlet library is required to use this function.
DLL load failed while importing _greenlet: 找不到指定的模块。
```

**解决**：

```sh
uv pip install greenlet
# Windows 还需要
uv pip install msvc-runtime
```

详见用户 FAQ。

## 16.3 WebSocket / Bot 连不上

### 16.3.1 Bot 端一直连不上

| Core 日志 | 原因 | 解决 |
|-----------|------|------|
| `🔒️ [GsCore] 拒绝来自已封禁 IP 的连接` | Bot 之前连错太多次 | 等 900s 或重启 Core 清状态 |
| `🚨 [GsCore] Token 错误!剩余尝试次数: ...` | `WS_TOKEN` 不一致 | 检查 Bot 端和 Core 端 |
| `🔒️ [GsCore] 未配置WS_TOKEN，所有外网连接将被拒绝！` | `WS_TOKEN` 空 + Bot 不在 `TRUSTED_IPS` | 配 Token 或加 IP |
| 没任何日志 | Core 没启 / 端口错 / 防火墙 | `curl ws://HOST:PORT` 测试 |

### 16.3.2 Bot 能连但消息 Core 不响应

| 现象 | 原因 | 解决 |
|------|------|------|
| 私聊能，群聊不能 | `command_start` 不一致 / bot 配置没 `@Bot` | 看两边的命令头 |
| 完全没响应 | master 没配 / 用户 ID 填错 | Core 日志里看 `user_id` |
| 部分命令不响应 | `sv.<name>.enabled=false` / `pm` 不够 | WebConsole 看 sv 配置 |
| 命令报错「找不到资源」 | 资源没下完 | `core重启` 或 `下载全部资源` |

### 16.3.3 Bot 连上但频繁断开

**原因**：反代 `proxy_read_timeout` 太短 / 网络抖动。

**解决**：

```nginx
proxy_read_timeout 600s;
```

## 16.4 WebConsole 拒登 / 异常

### 16.4.1 打开 `/app` 空白 / 404

**原因**：`data/dist/` 缺前端 bundle。

**解决**：

```sh
# 重启会自动拉（CDN）；慢可手动下载
# https://github.com/Genshin-bots/gsuid_core/releases 下载 dist.zip
wget -O data/dist.zip <release_url>
unzip data/dist.zip -d data/
```

### 16.4.2 登录提示「请求无效，请刷新页面后重试」

**原因**：

- 前端 bundle 过旧（reload 一下）
- 本地时钟漂移
- 被 MITM（**不要输入密码**）

**解决**：

1. 浏览器**强制刷新**（Ctrl+Shift+R / Cmd+Shift+R）
2. 检查系统时钟
3. 对比 Core 启动日志里的 `pubkey_fingerprint` 与 WebConsole 上看到的

### 16.4.3 登录提示「操作过于频繁」

**原因**：触发 `AuthRateLimiter` 限流（60s/10 次或 5 连败/900s）。

**解决**：等几百秒后重试，或重启 Core 清状态。

### 16.4.4 忘记注册码 / 密码

```sh
# 注册码
cat data/config.json | jq .REGISTER_CODE

# 密码：删 web_user 表 + 重置 REGISTER_CODE
sqlite3 data/GsData.db "DELETE FROM webuser;"

# 或重建（数据全丢）
rm data/GsData.db
# 重启 Core，自动重建表和新注册码
```

## 16.5 greenlet / SQLAlchemy 报错

详见 [§16.2.3](#1623-greenlet-dll-load-failed)。

## 16.6 Docker 容器问题

### 16.6.1 容器起来又立刻退出

```sh
docker logs gsuid_core
```

| 现象 | 解决 |
|------|------|
| `No module named ...` | 镜像旧，`docker pull` 重拉 |
| `Permission denied: data/` | 改挂载目录权限 |
| `OSError: [Errno 28] No space left` | 宿主机磁盘满，`docker system prune` |

### 16.6.2 容器能起但 WS 连不上

| 现象 | 解决 |
|------|------|
| `host.docker.internal` 解析不到 | docker-compose 已加 `extra_hosts`，单 `docker run` 需加 `--add-host host.docker.internal:host-gateway` |
| 镜像启动后 `HOST` 默认 `localhost` | 镜像默认 `core --host 0.0.0.0`，但若挂载了本地 `config.json` 会被覆盖 |
| 端口映射错 | `-p 8765:8765` |
| 容器内 IP 与宿主机不同 | 容器内网 ≠ 宿主机 IP，Bot 端要用宿主机 IP 或 `host.docker.internal` |

### 16.6.3 升级后报错

```sh
# 重置 venv 卷
docker-compose down -v
docker-compose up -d --build
```

### 16.6.4 git clone 卡死

容器内设 git 代理：

```sh
docker exec -it gsuid_core git config --global http.proxy http://host.docker.internal:7890
```

或在 WebConsole 切镜像源（见 [九、§9.5](./09-plugins.md#95-git-镜像源切换)）。

## 16.7 资源下载过慢或失败

业务插件（`GenshinUID` 等）首次启动会下载图片 / 攻略等资源。

**症状**：Bot 报 `No such file or directory` / 资源图裂开。

**解决**：

1. `core重启` 让 Core 重试下载
2. 或用 master 命令 `下载全部资源`
3. 慢 / 失败：挂代理 / 切镜像源
4. 仍然失败：用户文档 [`Extra/ResourceDownload.md`](../../../../../GenshinUID-docs/docs/Extra/ResourceDownload.md) 下载离线资源包覆盖到 `data/<plugin_name>/resource/`

## 16.8 米游社 / 业务 API 报错

### 16.8.1 `SSLCertVerificationError`

**用户文档 FAQ**：

> 进入[虚空数据库](https://akashadata.feixiaoqiu.com/static/data/abyss_total.js)，
> 点击链接左边的小锁 → 链接是安全的 → 证书有效 → 详细信息 → 导出证书，
> 文件名随便，后缀改 `.crt`，双击安装证书。

### 16.8.2 `Httpx AsyncClient Timeout Error`

涉及功能 `版本深渊`，网络无法连接内鬼网，开代理或忽略（不影响大部分功能）。

### 16.8.3 签到 / 每日 报错 / 错误码 1034

**原因**：米游社全域验证码风控。

**解决**：手动上米游社解除验证码（我的 → 我的角色），目前**暂无公开解决方案**。

### 16.8.4 群友用命令无反馈、仅自己可用

**原因**：sv 配置错乱 / 权限错乱。

**解决**：用 master 命令 `重置core配置` 后重启。

## 16.9 命令不响应 / 帮助图不刷新

### 16.9.1 `gs帮助` 无反应，其他命令有效

**原因**：帮助图缓存过期。

**解决**：`core重启`。

### 16.9.2 装了插件但命令列表里没有

**原因**：插件没加载 / 加载报错。

**解决**：

```sh
# Core 日志搜「Plugins」「SV」相关
grep -i "plugin\|sv" data/logs/<最新日志文件>

# 或用 master 命令
core刷新插件列表
core重启
```

## 16.10 升级 / 迁移失败

### 16.10.1 升级后 ImportError

**原因**：依赖没同步。

**解决**：

```sh
uv sync               # 源码
docker-compose down -v
docker-compose up -d --build    # Docker
```

### 16.10.2 升级后数据库报错

**原因**：表结构不兼容。

**解决**：

```sh
# 备份先
cp data/GsData.db data/GsData.db.bak

# 删表重建（数据全丢，仅作为最后手段）
rm data/GsData.db
core重启
```

### 16.10.3 v3 → v4 数据迁移失败

详见 [十四、§14.2](./14-upgrade.md#142-v3--v4-数据迁移)。

## 16.11 性能问题

### 16.11.1 内存爆

| 组件 | 缓解 |
|------|------|
| AI 嵌入模型 | 切 `embedding_provider=openai`（远程） |
| AI Rerank 模型 | `enable_rerank=false` 或切 `openai` |
| 数据库连接池 | 调小 `db_pool_size` |
| 定时任务堆积 | 看 APScheduler 日志，禁用长时间任务 |

### 16.11.2 启动慢

- 插件多 → 按需禁用不用的
- Playwright 首次启动要下载 Chromium（仅源码裸跑；Docker 已预装）
- AI 嵌入模型下载 → 切 `https://hf-mirror.com`

## 16.12 日志位置 & 级别

```sh
# 目录
data/logs/

# 最新日志
ls -t data/logs/*.log | head -1

# 实时
tail -f data/logs/<最新>

# 改级别
# data/config.json: "log": { "level": "DEBUG", ... }
# 重启 Core
```

## 16.13 提 Issue / 反馈 Bug

如果本章没覆盖到你的问题：

1. 收集信息：
   - Core 版本（`data/core_config.json` 或启动 banner）
   - Python 版本 + OS
   - 部署方式（源码 / Docker / 系统服务）
   - 完整启动日志（`DEBUG` 级别）
   - 复现步骤
2. GitHub Issue：https://github.com/Genshin-bots/gsuid_core/issues
3. 提供上面信息 + 最小复现脚本

> 提供日志时**用代码块包起来**，别裸贴控制台输出（被论坛吞格式）。
