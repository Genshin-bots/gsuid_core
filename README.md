# ⚙️[GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) Core 0.8.7

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

### 重要说明

镜像仅提供运行环境，**不包含**：

- ❌ 核心代码文件
- ❌ 插件文件
- ❌ 配置文件

用户需要通过以下方式提供代码：

✅ **挂载本地代码目录**

### 镜像特性

- **镜像地址**：`docker.cnb.cool/gscore-mirror/gscore-docker:latest`
- **基础镜像**：基于 `astral/uv:python3.12-bookworm-slim`
- **架构支持**：支持 `linux/amd64` 和 `linux/arm64`
- **注意**：镜像**仅包含运行环境**，不包含核心代码和插件

#### Playwright 版本（SayuStock 专用）

- **镜像地址**：`docker.cnb.cool/gscore-mirror/gscore-docker/playwright:latest`
- **参考 Dockerfile**：[Dockerfile](https://cnb.cool/gscore-mirror/gscore-docker/-/blob/main/Dockerfile.playwright)
- **使用方式**：

  ```shell
  # 方法一：直接使用环境变量
  GSCORE_IMAGE=docker.cnb.cool/gscore-mirror/gscore-docker/playwright:latest docker-compose up -d

  # 方法二：修改 .env 文件
  cp .env.example .env
  # 编辑 .env 文件，修改 GSCORE_IMAGE 的值
  # GSCORE_IMAGE=docker.cnb.cool/gscore-mirror/gscore-docker/playwright:latest
  docker-compose up -d
  ```

### 部署方式

**共同步骤**：

1. 拉取核心代码

```shell
# 方法一：从 GitHub 拉取
git clone https://github.com/Genshin-bots/gsuid_core.git

# 方法二：从 cnb.cool 拉取（国内镜像更快）
git clone https://cnb.cool/gscore-mirror/gsuid_core.git
```

2. 创建配置文件（可选）

```shell
cp .env.example .env
```

> 💡 如需自定义配置，请编辑 .env 文件并取消注释相应配置

3. 启动服务

**方式一：Docker Compose（推荐）**

```shell
docker-compose up -d
```

**方式二：Docker Run 命令**

```shell
docker run -d \
  --name gsuid_core \
  -p ${PORT:-8765}:8765 \
  -v ${MOUNT_PATH:-.}:/gsuid_core \
  -v venv-data:/venv \
  docker.cnb.cool/gscore-mirror/gscore-docker:latest
```

4. 访问控制台

启动后可通过 `localhost:8765/genshinuid` 进入核心的后台管理界面

### 插件安装方式

插件可以安装在任何一种位置：

**方式一：在宿主机安装（推荐）**

```shell
# 在宿主机上直接操作，无需进入容器
cd gsuid_core/plugins
git clone https://github.com/KimigaiiWuyi/GenshinUID.git -b v4
```

**方式二：在容器内安装**

```shell
docker exec -it gsuid_core sh
cd /gsuid_core/gsuid_core/plugins
git clone https://github.com/KimigaiiWuyi/GenshinUID.git -b v4
```

### 容器部署说明

- **挂载目录**：容器内的 `/gsuid_core` 目录对应项目根目录
- **虚拟环境**：持久化存储在 `venv-data` 卷中
- **网络**：支持通过 `host.docker.internal` 访问宿主机服务

### Git 代理配置

如果在容器内需要使用 git 代理，请在容器启动后手动配置：

```shell
docker exec -it gsuid_core git config --global http.proxy http://host.docker.internal:7890
```

> 💡 如果使用代理，请开启 lan 模式
