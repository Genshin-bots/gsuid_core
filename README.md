# ⚙️[GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) Core 0.9.1

[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-7C3AED.svg)](https://github.com/astral-sh/ruff)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Genshin-bots/gsuid-core/master.svg)](https://results.pre-commit.ci/latest/github/Genshin-bots/gsuid-core/master)

[KimigaiiWuyi/GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) 的核心部分，平台无关，支持 HTTP/WS 形式调用，便于移植到其他平台以及框架。

**💖 一套业务逻辑，多个平台支持！**

**🎉 [详细文档](https://docs.sayu-bot.com)** ( [快速开始(安装)](https://docs.sayu-bot.com/Started/InstallCore.html) | [链接 Bot](https://docs.sayu-bot.com/LinkBots/AdapterList.html) | [插件市场](https://docs.sayu-bot.com/InstallPlugins/PluginsList.html) )

## 优点&特色

- 🔀 **异步优先**：异步处理~~大量~~消息流，不会阻塞任务运行
- 🔧 **易于开发**：即使完全没有接触过 Python，也能在一小时内迅速上手 👉 [插件编写指南](https://docs.sayu-bot.com/CodePlugins/CookBook.html)
- ♻ **热重载**：修改插件配置&安装插件&更新插件，无需重启也能直接应用
- **🌎 [网页控制台](https://docs.sayu-bot.com/Advance/WebConsole.html)**：集成网页控制台，可以通过 WEB 直接操作**插件数据库/配置文件/检索日志/权限控制/数据统计/批量发送** 等超多操作
- 📄 **高度统一**：统一**所有插件**的[插件前缀](https://docs.sayu-bot.com/CodePlugins/PluginsPrefix.html)/[配置管理](https://docs.sayu-bot.com/CodePlugins/PluginsConfig.html)/[帮助图生成](https://docs.sayu-bot.com/CodePlugins/PluginsHelp.html)/权限控制/[数据库写入](https://docs.sayu-bot.com/CodePlugins/PluginsDataBase.html)/[订阅消息](https://docs.sayu-bot.com/CodePlugins/Subscribe.html)，所有插件编写常见方法一应俱全，插件作者可通过简单的**继承重写**实现**高度统一**的逻辑
- 💻 **多元适配**：借助上游 Bot (NoneBot2 / Koishi / YunzaiBot) 适配，支持 QQ/QQ 频道/微信/Tg/Discord/飞书/KOOK/DODO/OneBot v11(v12)等多个平台，做到**一套业务逻辑，多个平台支持**！
- 🚀 **作为插件**：该项目**不能独立使用**，作为**上游 Bot (NoneBot2 / Koishi / YunzaiBot)** 的插件使用，无需迁移原本 Bot，保留之前全部的功能，便于充分扩展
- 🛠 **内置命令**：借助内置命令，轻松完成**重启/状态/安装插件/更新插件/更新依赖**等操作
- 📝 **帮助系统**：通过统一适配，可按照不同**权限输出**不同帮助，并支持插件的**二级菜单注册**至主帮助目录，并支持在帮助界面使用不同的**自定义前缀**

<details><summary>主菜单帮助示例</summary><p>
<a><img src="https://s2.loli.net/2025/02/07/glxaJyS6325zvbG.jpg"></a>
</p></details>

## 感谢

- 本项目仅供学习使用，请勿用于商业用途
- [爱发电](https://afdian.com/a/KimigaiiWuyi)
- [GPL-3.0 License](https://github.com/Genshin-bots/gsuid_core/blob/master/LICENSE) ©[@KimigaiiWuyi](https://github.com/KimigaiiWuyi)

---

> [!IMPORTANT]
>
> 以下内容未经验证。

## 使用 Docker 部署

目前提供两种 Docker 部署模式

### 模式一：挂载模式 (Mount Mode) - 推荐

**特点**：挂载本地代码到容器，修改即生效。

1. **拉取代码**

```shell
# 方法一：从 GitHub 拉取
git clone https://github.com/Genshin-bots/gsuid_core.git

# 方法二：从 cnb.cool 拉取（国内镜像更快）
git clone https://cnb.cool/gscore-mirror/gsuid_core.git

cd gsuid_core
```

2. 创建配置文件（可选）

```shell
cp .env.example .env
```

> 💡 如需自定义配置，请编辑 .env 文件并取消注释相应配置

3. **启动服务**

```shell
docker-compose up -d --build
```

4. **管理**
   - 服务运行在端口 `8765`。
   - 启动后可通过 `localhost:8765/genshinuid` 进入核心的后台管理界面

---

### 模式二：全量模式 (Bundle Mode)

**特点**：无需下载源码，直接运行全量镜像（包含环境+代码+依赖）。

1. **获取配置文件**
   只需下载 [docker-compose.bundle.yml](./docker-compose.bundle.yml) 文件。

2. 创建配置文件（可选）

```shell
cp .env.example .env
```

2. **启动服务**

   **方式 A：Docker Compose (推荐)**

   ```shell
   docker-compose -f docker-compose.bundle.yml up -d
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

   _(会自动拉取全量镜像)_

3. **数据管理**

   - 数据持久化在 `/opt/gscore_data` 目录。
   - 自定义插件可放在 `/opt/gscore_plugins` 目录。

4. **管理**
   - 服务运行在端口 `8765`。
   - 启动后可通过 `localhost:8765/genshinuid` 进入核心的后台管理界面

---

### Playwright 支持 (截图功能)

目前所有 Docker 镜像 **默认均已包含 Playwright 及 Chromium 浏览器环境**，无需额外配置，开箱即用。

---

### 高级操作指南

#### 1. 网络代理配置

_(注意：请确保代理软件开启了 "允许局域网连接/LAN" 模式)_

**容器内的全局代理（不包括 Git 代理）**
在 `.env` 中添加：

```yaml
GSCORE_HTTP_PROXY=http://host.docker.internal:7890
GSCORE_HTTPS_PROXY=http://host.docker.internal:7890
```

**容器内设置 Git 代理**

```shell
docker exec -it gsuid_core git config --global http.proxy http://host.docker.internal:7890
```

#### 2. 安装额外的 Python 包

如果你安装了第三方插件需要额外依赖：

```shell
docker exec -it gsuid_core uv pip install <包名>
```

#### 3. 环境重置 (解决依赖冲突)

如果更新镜像后报错（如缺少依赖），请执行以下命令**彻底清理**旧环境：

**挂载模式：**

```shell
docker-compose down -v
docker-compose up -d --build
```

**全量模式：**

```shell
# docker-compose 模式
docker-compose -f docker-compose.bundle.yml down -v
docker-compose -f docker-compose.bundle.yml up -d

# docker run 模式
docker volume rm gsuid_core_venv
```

_(警告：这将删除 `venv-data` 卷，所有手动安装的包需要重新安装，但 `data` 数据不会丢失)_
