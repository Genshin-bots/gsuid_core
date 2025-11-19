# ⚙️[GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) Core 0.8.6

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

## 使用 docker 镜像部署

> 此镜像使用下文的 Github Actions 自动构建，每 12 小时更新

`请先安装好 Docker`


- 镜像地址（docker hub）：https://hub.docker.com/repository/docker/lilixxs666/gsuid-core

  docker hub 网站需要注册账号，并至少选择 Free Plan 才能访问

- 镜像名称：`lilixxs666/gsuid-core:dev`

- 免费用户有 1 小时 10 次拉取的限制😂

---

可通过如下指令拉取镜像并运行：
```bash
docker run -d \
--name gsuidcore \
-e TZ=Asia/Shanghai \
-e GSCORE_HOST=0.0.0.0 \
-p 18765:8765 \
-v /opt/gscore_data:/gsuid_core/data \
-v /opt/gscore_plugins:/gsuid_core/gsuid_core/plugins \
lilixxs666/gsuid-core:dev
```
在本地按照以上指令容器运行后，可直接进入`localhost:18765/genshinuid`进入核心的后台管理界面
相关文档见：

**镜像参数说明：**

| **参数**  | **功能**  |
|--------------------------------------|----------------------------------------------------------|
| `-d`                                                    | 服务后台运行                                                   |
| `--name gsuidcore`                                      | 生成的容器名称，可选，这里指定为 gsuidcore，若不写则系统给随机名称         |
| `-p 18765:8765`                                         | 端口映射，可选，默认不做映射，只有映射的端口才能被外部访问<br/>这里设置为容器内 8765 端口 → 外部 18765 端口                       |
| `-e TZ=Asia/Shanghai`                                   | 时区，可选，默认值=Asia/Shanghai                                  |
| `-e GSCORE_HOST=0.0.0.0`                                | 服务监听地址：可选，默认值=localhost                                  |
| `-v /opt/gscore_data:/gsuid_core/data `                 | 文件映射，可选，只有映射的路径内的文件才能直接从外部读写：原软件的 data 文件夹<br/>可从外部的  /opt/gscore_data 位置访问      |
| `-v /opt/gscore_plugins:/gsuid_core/gsuid_core/plugins` | 文件映射，可选，只有映射的路径内的文件才能直接从外部读写：原软件的 plugins 文件夹<br/>可从外部的 /opt/gscore_plugins 位置访问 &emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp;&emsp; |

**一些容器部署的常见问题**
详见下文【5. 容器部署的相关使用说明】


**镜像构建整体流程：**

- 主分支同步（Github Actions 每 12 小时执行一次）：项目主分支（gsuid-core:master） --> fork 的分支 (gsuid-core-prci:master)

- 镜像构建（Github Actions 每 12 小时执行一次）：fork 的分支 (gsuid-core-prci:master) --> 编译 --> 发布到 dockerhub


**用于实现以上构建流程的 fork 库地址：**

- 同步分支（master）：https://github.com/lilixxs/gsuid_core-prci/tree/master

- Github Action 构建分支（docker-autobuild）：https://github.com/lilixxs/gsuid_core-prci/tree/docker-autobuild


**Github Action 配置文件：**

- master 分支自动同步：./github/workflows/master-autosync.yaml
- docker 镜像自动构建：./github/workflows/docker-autobuild.yaml


## 使用 Docker compose 从代码构建 docker 容器（可选）

`请先安装好 Docker 与Docker Compose`

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

# 在同一机器上多次重复构建，会出现大量无用的镜像，可使用以下指令清空未使用的镜像
docker container prune -f
```

4. Docker Compose 配置文件说明
```yaml
services:
  gsuid-core:
    build:
      context: .
      # 指定要使用的镜像构建文件
      # Dockerfile = 原软件源（连到国外服务器，适用 Github CI/CD 或 Docker Hub 等外网环境）
      # Dockerfile.cn = 国内镜像源（使用国内镜像源，适用于自己或国内服务器构建）
      dockerfile: Dockerfile.cn
    container_name: gsuidcore
    privileged: true
    restart: unless-stopped
    environment:
      # TZ = 时区设置，可选参数，默认为 Asia/Shanghai
      # GSCORE_HOST = 服务监听地址 (0.0.0.0 = 监听全部地址，启动容器可直接进后台) 可选参数，默认 locaohost (只允许容器内本地访问)
      - TZ=Asia/Shanghai
      - GSCORE_HOST=0.0.0.0
    ports:
      # 端口映射，可自由修改，以下为：容器（core 中配置）8765 对应外部的 18765 端口
      - 18765:8765
    volumes:
      # 仅映射需要的文件夹，避免数据冲突
      # 如需访问项目根目录，需要通过 docker exec -it <容器id> bash 进入容器内部
      # 进入后默认的 /gsuid_core 即为插件根目录，路径与文档路径保持一致
      - /opt/gscore_data:/gsuid_core/data
      - /opt/gscore_plugins:/gsuid_core/gsuid_core/plugins
```
5. 容器部署的相关使用说明
- 如需访问容器部署的 core 的其他路径（上面 yaml 文件中没有映射的文件或路径）则需要通过`docker exec -it <容器id> bash`进入，进入后默认的`/gsuid_core`即对应文档中的 core 根目录`gsuid_core`，其他文档路径与官方文档对应

- 若不设定环境变量`GSCORE_HOST`,则 core 默认监听在`localhost`地址，Docker 部署后无法直接连接

  需要修改`config.json`文件中的`HOST`配置，参考 [https://docs.sayu-bot.com/Started/CoreConfig.html](https://docs.sayu-bot.com/Started/CoreConfig.html)

  （以上面的 yaml 文件为例，配置文件在文档中的路径为`gsuid_core/data`，则对应在容器外部的地址为`/gscore_data/config.json`，需将其中的`host`配置修改为`0.0.0.0`

  然后执行`docker restart <容器id>`重启容器


- 关于端口配置，由于 docker 容器本身会对端口做转发（对应 yaml 文件中的`port`部分，因此建议使用 docker compose 的相关配置或 docker 指令来修改端口，而不用 core 本身的配置来修改

  同时每次 docker 修改端口、监听地址后都需要先删除当前容器重新执行`docker compose up -d`指令重新部署

- 如需增加插件，建议使用命令进行安装，也可进入容器项目根目录手动安装

- 如果Bot（例如 NoneBot2、HoshinoBot）也是Docker部署的，Core或其插件更新后，建议将Core和Bot的容器都重启，保证修改生效
