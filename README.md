# ⚙️ GenshinUID Core

[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-7C3AED.svg)](https://github.com/astral-sh/ruff)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Genshin-bots/gsuid-core/master.svg)](https://results.pre-commit.ci/latest/github/Genshin-bots/gsuid-core/master)

GsCore（早柚核心 / `gsuid-core`）是 [GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) 的跨平台服务框架。服务支持通过 WebSocket 与 HTTP 协议接入 NoneBot2、Koishi、YunzaiBot 等上游聊天机器人框架，实现业务逻辑与聊天平台的解耦。

**[官方文档](https://docs.sayu-bot.com)**（[安装指南](https://docs.sayu-bot.com/Started/InstallCore.html) | [适配器列表](https://docs.sayu-bot.com/LinkBots/AdapterList.html) | [插件市场](https://docs.sayu-bot.com/InstallPlugins/PluginsList.html)）

## 功能特性

- **异步架构**：基于异步事件循环处理消息流与网络请求，不阻塞后台任务。
- **配置热重载**：修改插件配置、安装或更新插件无需重启服务。
- **Web 控制台**：内置 [WebConsole](https://docs.sayu-bot.com/Started/WebConsole.html) 管理界面，支持管理插件配置、数据表、日志审计、权限与运行统计。
- **统一插件规范**：为插件提供统一的[命令前缀](https://docs.sayu-bot.com/CodePlugins/PluginsPrefix.html)、[配置项管理](https://docs.sayu-bot.com/CodePlugins/PluginsConfig.html)、[帮助菜单](https://docs.sayu-bot.com/CodePlugins/PluginsHelp.html)、[数据库访问](https://docs.sayu-bot.com/CodePlugins/PluginsDataBase.html)与[消息订阅](https://docs.sayu-bot.com/CodePlugins/Subscribe.html)。
- **多平台适配**：通过适配器对接 NoneBot2、Koishi、YunzaiBot、AstrBot 等框架，支持 QQ、微信、Telegram、Discord、飞书、KOOK 等平台。
- **插件宿主定位**：本项目不直连聊天平台，作为上游 Bot 的后端服务运行。
- **内置运维命令**：内置服务重启、运行状态查询、插件管理与依赖更新等运维命令。
- **权限分级帮助**：支持按权限输出对应的帮助信息，并支持将插件二级菜单注册至主目录。

<details><summary>主菜单帮助示例</summary><p>
<img src="https://s2.loli.net/2025/02/07/glxaJyS6325zvbG.jpg" alt="帮助菜单示例">
</p></details>

## 声明与致谢

- 本项目仅供学习使用，请勿用于商业用途。
- [爱发电赞助](https://afdian.com/a/KimigaiiWuyi)
- 开源协议：[GPL-3.0 License](https://github.com/Genshin-bots/gsuid_core/blob/master/LICENSE) © [@KimigaiiWuyi](https://github.com/KimigaiiWuyi)

---

## 使用 Docker 部署

提供两种 Docker 部署模式：

### 模式一：挂载模式（推荐）

挂载本地代码目录到容器，本地修改即时生效。

1. **拉取代码**

   从 GitHub 拉取：

   ```shell
   git clone https://github.com/Genshin-bots/gsuid_core.git
   cd gsuid_core
   ```

   或从国内镜像拉取：

   ```shell
   git clone https://cnb.cool/gscore-mirror/gsuid_core.git
   cd gsuid_core
   ```

2. **创建配置文件（可选）**

   若需自定义配置，可复制环境模板：

   ```shell
   cp .env.example .env
   ```

3. **启动服务**

   ```shell
   docker compose up -d --build
   ```

4. **访问服务**

   服务默认运行在端口 `8765`。启动后访问 `http://localhost:8765/app` 进入后台管理界面。

---

### 模式二：全量镜像模式

直接运行包含运行环境、代码与依赖的镜像，无需下载源码。

1. **获取配置文件**

   下载 [docker-compose.bundle.yml](./docker-compose.bundle.yml)。

2. **创建配置文件（可选）**

   若需自定义配置，可复制环境模板：

   ```shell
   cp .env.example .env
   ```

3. **启动服务**

   **方式 A：Docker Compose**

   ```shell
   docker compose -f docker-compose.bundle.yml up -d
   ```

   **方式 B：Docker Run**

   ```shell
   docker run -d \
     --name gsuid_core \
     --restart always \
     -p 8765:8765 \
     -v /opt/gscore_data:/gsuid_core/data \
     -v /opt/gscore_plugins:/gsuid_core/gsuid_core/plugins \
     -v gsuid_core_venv:/venv \
     docker.cnb.cool/gscore-mirror/gsuid_core:latest
   ```

4. **数据持久化**

   - 业务数据保存在 `/opt/gscore_data` 目录。
   - 自定义插件保存在 `/opt/gscore_plugins` 目录。

5. **访问服务**

   服务默认运行在端口 `8765`。启动后访问 `http://localhost:8765/app` 进入后台管理界面。

---

### Playwright 环境

所有 Docker 镜像均预装 Playwright 与 Chromium 运行环境，无需额外配置。

---

### 高级操作

#### 1. 网络代理配置

配置代理前，须确保代理软件已开启“允许局域网连接”功能。

**容器内全局代理（不含 Git 代理）**

在 `.env` 中添加：

```yaml
GSCORE_HTTP_PROXY=http://host.docker.internal:7890
GSCORE_HTTPS_PROXY=http://host.docker.internal:7890
```

**容器内设置 Git 代理**

```shell
docker exec -it gsuid_core git config --global http.proxy http://host.docker.internal:7890
```

#### 2. 安装 Python 依赖

安装第三方插件所需依赖：

```shell
docker exec -it gsuid_core uv pip install <包名>
```

#### 3. 环境重置

> [!WARNING]
> 执行重置操作将删除 `venv-data` 数据卷，所有手动安装的 Python 包均须重新安装。`data` 目录中的业务数据不会丢失。

更新镜像后若发生依赖冲突，可按如下步骤清理旧环境：

**挂载模式：**

```shell
docker compose down -v
docker compose up -d --build
```

**全量模式：**

```shell
# Docker Compose 模式
docker compose -f docker-compose.bundle.yml down -v
docker compose -f docker-compose.bundle.yml up -d

# Docker Run 模式
docker volume rm gsuid_core_venv
```
