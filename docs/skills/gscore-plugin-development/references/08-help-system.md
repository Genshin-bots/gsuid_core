# 八、帮助系统注册

GsCore 提供统一的"插件帮助一览"页面，每个插件应注册一项条目，并提供发送图片帮助的命令。

## 8.1 `register_help` —— 把插件挂到一览页

在帮助子模块 `__init__.py` 中调用 `register_help(name, help, icon)`，模块加载时即注册：

```python
# MyPlugin/myplugin_help/__init__.py
from PIL import Image
from gsuid_core.sv import SV, get_plugin_available_prefix
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.help.utils import register_help

from .get_help import ICON, get_help

sv_help = SV("MyPlugin 帮助")

@sv_help.on_fullmatch("帮助", block=True)
async def send_help_img(bot: Bot, ev: Event):
    await bot.send(await get_help())

# 注册到全局帮助一览（命令在框架接收到 "core 帮助" 时会列出所有插件）
register_help(
    "MyPlugin",
    f"{get_plugin_available_prefix('MyPlugin')}帮助",   # 自动取插件的可用前缀
    Image.open(ICON),
)
```

## 8.2 `get_new_help` —— 渲染插件自身的帮助图

帮助数据写在 `help.json`：键是分组名，值含 `desc` 描述和 `data`（每条命令的展示信息）。
然后用 `get_new_help` 一行渲染：

```python
# MyPlugin/myplugin_help/get_help.py
import json
from typing import Dict
from pathlib import Path

import aiofiles
from PIL import Image

from gsuid_core.sv import get_plugin_available_prefix
from gsuid_core.help.model import PluginHelp
from gsuid_core.help.draw_new_plugin_help import get_new_help

from ..version import MyPlugin_version
from ..utils.image import get_footer

ICON = Path(__file__).parent.parent.parent / "ICON.png"
HELP_DATA = Path(__file__).parent / "help.json"
ICON_PATH = Path(__file__).parent / "icon_path"
TEXT_PATH = Path(__file__).parent / "texture2d"


async def get_help_data() -> Dict[str, PluginHelp]:
    async with aiofiles.open(HELP_DATA, "rb") as file:
        return json.loads(await file.read())


async def get_help():
    return await get_new_help(
        plugin_name="MyPlugin",
        plugin_info={f"v{MyPlugin_version}": ""},
        plugin_icon=Image.open(ICON),
        plugin_help=await get_help_data(),
        plugin_prefix=get_plugin_available_prefix("MyPlugin"),
        help_mode="dark",                                   # or "light"
        banner_bg=Image.open(TEXT_PATH / "banner_bg.jpg"),
        banner_sub_text="为你服务！",
        help_bg=Image.open(TEXT_PATH / "bg.jpg"),
        cag_bg=Image.open(TEXT_PATH / "cag_bg.png"),
        item_bg=Image.open(TEXT_PATH / "item.png"),
        icon_path=ICON_PATH,
        footer=get_footer(),
        enable_cache=True,                                  # 推荐：缓存生成结果加速
    )
```

`help.json` 示例：

```json
{
  "绑定相关": {
    "desc": "把游戏 UID 绑定到机器人账号",
    "data": [
      {
        "name": "绑定",
        "eg": "mp 绑定 12345678",
        "need_ck": false,
        "need_sk": false,
        "need_admin": false
      },
      {
        "name": "解绑",
        "eg": "mp 解绑 12345678",
        "need_ck": false,
        "need_sk": false,
        "need_admin": false
      }
    ]
  },
  "查询相关": {
    "desc": "查询角色 / 装备 / 抽卡记录",
    "data": [
      {
        "name": "查角色",
        "eg": "mp 查角色 雷电将军",
        "need_ck": true,
        "need_sk": false,
        "need_admin": false
      }
    ]
  }
}
```

- `name` 命令名（也是 `icon_path/<name>.png` 图标查找键，没图标可省略）。
- `eg` 用户实际要发的示例（含前缀）。
- `need_ck`/`need_sk`/`need_admin` 控制图标右上角的标签。

## 8.3 注册到 "core 状态"（`register_status`）

GsCore 内置一个 **"core 状态"** 命令——任意用户发 `core状态` 都会得到一张汇总插件运行状态的
图片（订阅数、绑定数、激活用户数 …）。每个插件都应该在加载时调用 `register_status` 把
自己**最关键的运行指标**挂上去，让管理员一眼看到健康状况。

参照 SayuStock / GenshinUID 的写法：

```python
# MyPlugin/myplugin_status/__init__.py
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.status.plugin_status import register_status

from ..utils.image import get_ICON
from ..utils.database.models import MyGameBind


async def get_bind_num() -> int:
    """已绑定 UID 的用户数"""
    datas = await MyGameBind.get_all_data()
    return len(datas) if datas else 0


async def get_sub_num() -> int:
    """开启每日早报订阅数"""
    datas = await gs_subscribe.get_subscribe("[MyPlugin] 每日早报")
    return len(datas) if datas else 0


# 模块导入时立即注册。register_status 是同步函数，参数：
#   icon:        PIL.Image，插件图标（一般直接 get_ICON()）
#   plugin_name: 在状态图上显示的插件标题
#   plugin_status: {显示名: 异步无参函数 -> str/int/float}
register_status(
    get_ICON(),
    "MyPlugin",
    {
        "绑定 UID": get_bind_num,
        "订阅早报": get_sub_num,
    },
)
```

**关键点**：

- `register_status` 的第三个参数是 `Dict[显示名, async () -> Union[str, int, float]]`，
  框架会在用户发 `core状态` 时**并发调用**所有指标函数取实时值，**所以指标函数必须 async + 快**。
- 状态函数里**严禁**调外部 API / 跑长任务——只查本地数据库或读内存计数。慢函数会阻塞整张
  状态图的渲染。
- 指标值数量 1–4 个最合适，太多挤不下；命名 4 个汉字以内最佳。
- 多个指标可以放在 `myplugin_status/__init__.py` 中通过 `gs_subscribe` 和 ORM 直接算。
