# ⚙️[GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) Core 0.7.1

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Imports: isort](https://img.shields.io/badge/%20imports-isort-%231674b1?&labelColor=ef8336)](https://pycqa.github.io/isort/)
[![Lint: flake8](https://img.shields.io/badge/lint-flake8-&labelColor=4C9C39)](https://flake8.pycqa.org/)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Genshin-bots/gsuid-core/master.svg)](https://results.pre-commit.ci/latest/github/Genshin-bots/gsuid-core/master)

[KimigaiiWuyi/GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) 的核心部分，平台无关，支持 HTTP/WS 形式调用，便于移植到其他平台以及框架。

本Readme的部分内容**已经失效**，请前往最新的详细文档查阅：

**🎉 [详细文档](https://docs.sayu-bot.com)** ( [快速开始(安装)](https://docs.sayu-bot.com/Started/InstallCore.html) | [链接Bot](https://docs.sayu-bot.com/LinkBots/AdapterList.html) | [插件市场](https://docs.sayu-bot.com/InstallPlugins/PluginsList.html) )

👉 [插件编写指南](https://docs.sayu-bot.com/CodePlugins/CookBook.html)

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

3. Docker Compose启动

```shell
# 进入项目根目录
docker-compose up -d
```

- 默认core将运行在`localhost:8765`端口上，Docker部署必须修改`config.json`，如`0.0.0.0:8765`
- 如果Bot（例如NoneBot2、HoshinoBot）也是Docker部署的，Core或其插件更新后，可能需要将Core和Bot的容器都重启才生效
