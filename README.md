# ⚙️[GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) Core 0.7.1

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Imports: isort](https://img.shields.io/badge/%20imports-isort-%231674b1?&labelColor=ef8336)](https://pycqa.github.io/isort/)
[![Lint: flake8](https://img.shields.io/badge/lint-flake8-&labelColor=4C9C39)](https://flake8.pycqa.org/)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Genshin-bots/gsuid-core/master.svg)](https://results.pre-commit.ci/latest/github/Genshin-bots/gsuid-core/master)

[KimigaiiWuyi/GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) 的核心部分，平台无关，支持 HTTP/WS 形式调用，便于移植到其他平台以及框架。

**💖一套业务逻辑，多个平台支持！**

**🎉 [详细文档](https://docs.sayu-bot.com)** ( [快速开始(安装)](https://docs.sayu-bot.com/Started/InstallCore.html) | [链接Bot](https://docs.sayu-bot.com/LinkBots/AdapterList.html) | [插件市场](https://docs.sayu-bot.com/InstallPlugins/PluginsList.html) )

## 优点&特色

- 🔀 **异步优先**：异步处理~~大量~~消息流，不会阻塞任务运行
- 🔧 **易于开发**：即使完全没有接触过Python，也能在一小时内迅速上手 👉 [插件编写指南](https://docs.sayu-bot.com/CodePlugins/CookBook.html)
- ♻ **热重载**：修改插件配置&安装插件&更新插件，无需重启也能直接应用
- **🌎 [网页控制台](https://docs.sayu-bot.com/Advance/WebConsole.html)**：集成网页控制台，可以通过WEB直接操作**插件数据库/配置文件/检索日志/权限控制/数据统计/批量发送** 等超多操作
- 📄 **高度统一**：统一**所有插件**的[插件前缀](https://docs.sayu-bot.com/CodePlugins/PluginsPrefix.html)/[配置管理](https://docs.sayu-bot.com/CodePlugins/PluginsConfig.html)/[帮助图生成](https://docs.sayu-bot.com/CodePlugins/PluginsHelp.html)/权限控制/[数据库写入](https://docs.sayu-bot.com/CodePlugins/PluginsDataBase.html)/[订阅消息](https://docs.sayu-bot.com/CodePlugins/Subscribe.html)，所有插件编写常见方法一应俱全，插件作者可通过简单的**继承重写**实现**高度统一**的逻辑
- 💻 **多元适配**：借助上游Bot (NoneBot2 / Koishi / YunzaiBot) 适配，支持QQ/QQ频道/微信/Tg/Discord/飞书/KOOK/DODO/OneBot v11(v12)等多个平台，做到**一套业务逻辑，多个平台支持**！
- 🚀 **作为插件**：该项目**不能独立使用**，作为**上游Bot (NoneBot2 / Koishi / YunzaiBot)** 的插件使用，无需迁移原本Bot，保留之前全部的功能，便于充分扩展
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

## Docker部署Core（可选）

`请先安装好Docker与Docker Compose`

1. git clone gsuid-core本体

```shell
git clone https://github.com/Genshin-bots/gsuid_core.git --depth=1 --single-branch
```

2. 安装所需插件（可选）

```shell
# cd进入插件文件夹内
cd plugins
# 安装v4 GenshinUID
git clone -b v4 https://github.com/KimigaiiWuyi/GenshinUID.git --depth=1 --single-branch
```

3. Docker Compose 启动

> 注意：根据 docker 版本及插件安装情况，composer 命令可能为`docker-composer`或`docker composer`，若下面的语句执行提示指令出错，请换一个语句尝试（若两个语句都提示指令出错，则是`Docker Composer`没有安装

```shell
# 进入项目根目录（此目录下包含 Dockerfile 和 docker-compose.yaml 文件）

# 初次安装，需要编译、安装依赖，需执行以下指令：
docker-compose up -d --build

# 之后的部署配置，仅修改 docker-compose.yaml 文件，需执行以下指令：
# 先停止相关容器
docker-compose down
# 再按照新的配置部署新的容器
docker-compose up -d
```

4. Docker Compose 配置文件说明
```yaml
services:
  gsuid-core:
    build:
      context: .
    container_name: gsuidcore # 生成的容器名称（若名称被占用则会变为随机名称）
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai # 时区设置
    ports:
      - 18765:8765 # 端口映射：原插件的 8765 对应容器外部的 18765 端口
    volumes:
    # 仅映射需要外部访问的文件夹，如 data、plugins 文件夹
	# 以下例子：将容器内 core 程序的 data、plugins 文件夹，映射到 /opt/gscore_data 和 /opt/gscore_plugins 中
      - /opt/gscore_data:/app/gsuid_core/data
      - /opt/gscore_plugins:/app/gsuid_core/plugins
```
5. 容器部署的相关使用说明
- 如需访问容器部署的 core 的其他路径（上面 yaml 文件中没有映射的文件或路径）则需要通过`docker exec -it <容器id> bash`进入，进入后默认的`/app/gsuid_core`对应文档中的 core 根目录`gsuid_core`

- 默认 core 运行在`localhost:8765`端口上，Docker 部署后无法连接

  必须修改`config.json`文件中的`HOST`配置，参考 [https://docs.sayu-bot.com/Started/CoreConfig.html](https://docs.sayu-bot.com/Started/CoreConfig.html)

  （以上面的 yaml 文件为例，配置文件在文档中的路径为`gsuid_core/data`，则对应在容器外部的地址为`/opt/gscore_data/config.json`，需将其中的`port`配置修改为`0.0.0.0:8765`

  然后执行`docker restart <容器id>`重启容器


- 关于端口配置，由于 docker 容器本身会对端口做转发（对应 yaml 文件中的`port`部分，因此建议使用 docker compose 的相关配置或 docker 指令来修改端口，而不用 core 本身的配置来修改

	同时每次 docker 修改端口后都需要先删除当前容器重新执行`docker compose up -d`指令重新部署

- 如需增加插件，则需要进入容器项目根目录，按照官方教程使用命令安装或手动安装

- 如果Bot（例如 NoneBot2、HoshinoBot）也是Docker部署的，Core或其插件更新后，可能需要将Core和Bot的容器都重启才生效
