# ⚙️[GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) Core 0.1.0

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Imports: isort](https://img.shields.io/badge/%20imports-isort-%231674b1?&labelColor=ef8336)](https://pycqa.github.io/isort/)
[![Lint: flake8](https://img.shields.io/badge/lint-flake8-&labelColor=4C9C39)](https://flake8.pycqa.org/)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Genshin-bots/gsuid-core/master.svg)](https://results.pre-commit.ci/latest/github/Genshin-bots/gsuid-core/master)

[KimigaiiWuyi/GenshinUID](https://github.com/KimigaiiWuyi/GenshinUID) 的核心部分，平台无关，支持 HTTP/WS 形式调用，便于移植到其他平台以及框架。

**🎉[详细文档](https://docs.gsuid.gbots.work/#/)**

## 安装Core

1. git clone gsuid-core本体

```shell
git clone https://ghproxy.com/https://github.com/Genshin-bots/gsuid_core.git --depth=1 --single-branch
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
git clone -b v4 https://ghproxy.com/https://github.com/KimigaiiWuyi/GenshinUID.git --depth=1 --single-branch
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
git clone https://ghproxy.com/https://github.com/Genshin-bots/gsuid_core.git --depth=1 --single-branch
```

2. 安装所需插件（可选）

```shell
# cd进入插件文件夹内
cd plugins
# 安装v4 GenshinUID
git clone -b v4 https://ghproxy.com/https://github.com/KimigaiiWuyi/GenshinUID.git --depth=1 --single-branch
```

3. Docker Compose启动

```shell
# 进入项目根目录
docker-compose up -d
```

- 默认core将运行在`localhost:8765`端口上，Docker部署必须修改`config.json`，如`0.0.0.0:8765`
- 如果Bot（例如NoneBot2、HoshinoBot）也是Docker部署的，Core或其插件更新后，可能需要将Core和Bot的容器都重启才生效

## 配置文件

修改`gsuid_core/gsuid_core/config.json`，参考如下

**（注意json不支持`#`，所以不要复制下面的配置到自己的文件中）**

```json
{
 "HOST": "localhost", # 如需挂载公网修改为`0.0.0.0`
 "PORT": "8765", # core端口
 "masters": ["444835641", "111"], # Bot主人，pm为0
 "superusers": ["123456789"], # 超管，pm为1
 "sv": {
     "Core管理": {
         "priority": 5, # 某个服务的优先级
         "enabled": true, # 某个服务是否启动
         "pm": 1, # 某个服务要求的权限等级
         "black_list": [], # 某个服务的黑名单
         "area": "ALL",  # 某个服务的触发范围
         "white_list": [] # 某个服务的白名单
     },
 },
 "log": {
     "level": "DEBUG" # log等级
 },
 "command_start": ["/", "*"], # core内所有插件的要求前缀
 "misfire_grace_time": 90
}
```

> 黑名单一旦设置，黑名单中的用户ID将无法访问该服务
>
> 白名单一旦设置，只有白名单的用户ID能访问该服务
>
> 服务配置可以通过[网页控制台](https://docs.gsuid.gbots.work/#/WebConsole)实时修改, 如果手动修改`config.json`需要**重启**

## 编写插件


```python
import asyncio

from gsuid_core.sv import SL, SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event


@SV('开关').on_prefix(('关闭', '开启')) # 定义一组服务`开关`，服务内有两个前缀触发器
async def get_switch_msg(bot: Bot, ev: Event):
    name = ev.text         # 获取消息除了命令之外的文字
    command = ev.command   # 获取消息中的命令部分
    im = await process(name)  # 自己的业务逻辑
    await bot.logger.info('正在进行[关闭/开启开关]')  # 发送loger
    await bot.send(im)   # 发送消息

sv=SV(
    name='复杂的服务',  # 定义一组服务`开关`,
    pm=2, # 权限 0为master，1为superuser，2为群的群主&管理员，3为普通
    priority=5, # 整组服务的优先级
    enabled=True, # 是否启用
    area= 'ALL', # 群聊和私聊均可触发
    black_list=[], # 黑名单
    white_list=[], # 白名单
)

@sv.on_prefix('测试')
async def get_msg(bot: Bot, ev: Event):
    ...
```
