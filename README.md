# ⚙️[GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) Core 0.6.1

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Imports: isort](https://img.shields.io/badge/%20imports-isort-%231674b1?&labelColor=ef8336)](https://pycqa.github.io/isort/)
[![Lint: flake8](https://img.shields.io/badge/lint-flake8-&labelColor=4C9C39)](https://flake8.pycqa.org/)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Genshin-bots/gsuid-core/master.svg)](https://results.pre-commit.ci/latest/github/Genshin-bots/gsuid-core/master)

[KimigaiiWuyi/GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) 的核心部分，平台无关，支持 HTTP/WS 形式调用，便于移植到其他平台以及框架。

本Readme的部分内容**可能已经失效**，请前往最新的详细文档查阅：

**🎉[详细文档](https://docs.sayu-bot.com)**

 👉[插件编写指南](https://docs.sayu-bot.com/CodePlugins/CookBook.html)

## 安装Core

1. git clone gsuid-core本体

```shell
git clone https://github.com/Genshin-bots/gsuid_core.git --depth=1 --single-branch
```

2. 安装poetry

```shell
pip install poetry
```

3. 安装所需依赖

```shell
# cd进入clone好的文件夹内
cd gsuid_core
# 安装依赖
poetry install
```

4. 安装所需插件（可选）

```shell
# cd进入插件文件夹内
cd plugins
# 安装v4 GenshinUID
git clone -b v4 https://github.com/KimigaiiWuyi/GenshinUID.git --depth=1 --single-branch
```

5. 启动gsuid_core（早柚核心）

```shell
# 在gsuid_core/genshin_core文件夹内
poetry run python core.py
# 或者（二选一即可）
poetry run core
```

6. 链接其他适配端

+ 默认core将运行在`localhost:8765`端口上，如有需要可至`config.json`修改。
+ 在支持的Bot上（例如NoneBot2、HoshinoBot、ZeroBot、YunZaiBot等），安装相应适配插件，启动Bot（如果有修改端口，则需要在启动Bot前修改适配插件相应端口），即可自动连接Core端。

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
