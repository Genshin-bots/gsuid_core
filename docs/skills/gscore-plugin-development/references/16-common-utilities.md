# 十六、常用工具模块速查

这一节汇总插件开发最常用的"小工具"——多读两遍能省下大量重新发明轮子的功夫。

## 16.1 资源存储：`get_res_path`

**所有插件运行时数据 / 配置 / 缓存 / 用户数据**都应该放在 `data/<PluginName>/` 下，
**严禁**在插件代码目录下写盘（卸载升级时会被删掉），**严禁**写到 `Path.cwd()` 或系统临时目录。

```python
from gsuid_core.data_store import get_res_path

# 几种调用方式：
get_res_path()                              # data/（数据根）
get_res_path("MyPlugin")                    # data/MyPlugin/
get_res_path(["MyPlugin", "players"])       # data/MyPlugin/players/
get_res_path(Path("/abs/path"))             # 绝对路径直接用

# 不存在的目录会被自动 mkdir(parents=True)
```

**强烈推荐**：在 `utils/resource/RESOURCE_PATH.py` 集中定义所有路径常量（参照 §1.5），
其他模块都从这里 import，避免散落到处的 `get_res_path("XXX")`。

框架自身也用 `get_res_path` 维护了几个常用路径，需要时直接复用：

| 常量 | 路径 | 用途 |
|------|------|------|
| `gsuid_core.data_store.RES` | `data/` | 数据根 |
| `image_res` | `data/IMAGE_TEMP/` | 临时图片缓存 |
| `data_cache_path` | `data/DATA_CACHE_PATH/` | 数据缓存 |
| `CONFIGS_PATH` | `data/configs/` | 框架级配置 |
| `PLUGINS_CONFIGS_PATH` | `data/plugins_configs/` | 插件级配置（多个插件共享） |
| `AI_CORE_PATH` | `data/ai_core/` | AI 模块（记忆 / artifact / RAG） |

## 16.2 推送主人消息：`send_msg_to_master`

任何需要"通知机器人主人 / 运维"的场景（异常告警、签到失败汇总、被频控警报、新版本提示），
**首选**：

```python
from gsuid_core.utils.message import send_msg_to_master

await send_msg_to_master("⚠️ API 异常 5 次，请检查 Cookie 配置。")
await send_msg_to_master(MessageSegment.image(error_screenshot))
await send_msg_to_master(["第一行", "第二行", MessageSegment.image(b"...")])
```

**前置条件**：主人需要先发送 `core订阅主人` 类命令（buildin_plugins 中已注册），
框架会在 `subscribe` 表里以 `task_name="主人用户"` 持久化一条订阅。
`send_msg_to_master` 内部就是 `gs_subscribe.get_subscribe("主人用户")` 后强制
`force_direct=True` 推私聊。

> 配置项 `core_config.get_config("masters")` 也维护一份主人 ID 列表，没有订阅记录时
> `send_msg_to_master` 会打 warning 但不会崩溃。

## 16.3 错误码与提示：`error_reply`

`gsuid_core.utils.error_reply` 已封装了通用的米游社风格错误提示：

```python
from gsuid_core.utils.error_reply import (
    UID_HINT,      # "你还没绑定过 uid 哦！请使用 [{前缀}绑定uid xxx] 命令绑定！"
    MYS_HINT,      # 米游社 ID 未绑定
    CK_HINT,       # Cookie 未绑定
    SK_HINT,       # Stoken 未绑定
    VERIFY_HINT,   # 出现验证码
    CHAR_HINT,     # 角色缓存未生成
    UPDATE_HINT,   # 插件更新失败
    get_error,     # int retcode -> 中文提示
    get_error_img, # int retcode -> bytes 错误图（可选）
)

# 用法（业务里直接复用，不要自己写"请先绑定 UID"）
bind = await MyBind.get_bind(ev.user_id, ev.bot_id)
if not bind:
    return await bot.send(UID_HINT)

# API 返回非 0 retcode 时
data = await fetch_data()
if data["retcode"] != 0:
    return await bot.send(get_error(data["retcode"], data))
```

新增自定义错误码：从外部 patch `error_reply.error_dict`（在插件启动钩子里）即可。

## 16.4 限流：`CooldownTracker`

防止用户狂刷某个高成本命令：

```python
from gsuid_core.utils.cooldown import cooldown_tracker

@sv.on_command("查角色")
async def query_char(bot: Bot, ev: Event):
    # 同一用户 30 秒内只能查一次
    if cooldown_tracker.is_on_cooldown(ev.user_id, cooldown=30):
        return await bot.send("⏰ 30 秒内请勿重复查询")
    ...
```

`cooldown_tracker` 是一个**全局单例**，所有插件共享同一份命中表——key 用
`f"{plugin}:{user_id}:{action}"` 类似格式做命名空间隔离。

## 16.5 函数级图片缓存：`@gs_cache`

```python
from gsuid_core.utils.cache import gs_cache

@gs_cache(expire_time=300)   # 300 秒内同参数命中
async def render_dashboard(uid: str, mode: str) -> bytes:
    data = await fetch(uid, mode)
    return await build_image(data)
```

- 自动按函数名 + 参数 hash 作 key，缓存到 `data/IMAGE_CACHE/<ts>_<key>.jpg`。
- 返回值是 `bytes` / `PIL.Image` / `"base64://..."` / `Path` 都能缓存。
- 缓存过期自动清理。
- **副作用函数（写库 / 发消息）不要加** ——只有"纯输入 → 输出"的渲染 / 计算函数才适合。

## 16.6 字体：`core_font`

```python
from gsuid_core.utils.fonts.fonts import core_font

font = core_font(48)        # 拿一个 size=48 的中英文兜底字体
draw.text((10, 10), "雷电将军", font=font, fill="white")
```

**不要 hardcode 字体路径**，`core_font` 内部用框架自带的 `MiSans-Bold.ttf`。

## 16.7 同步代码异步桥接：`to_thread`

GsCore 是全异步项目，但偶尔需要跑 CPU 密集型同步函数：

```python
from gsuid_core.pool import to_thread

@to_thread
def heavy_calc(data: list[int]) -> int:
    return sum(x * x for x in data)

# 调用时不要再 await——@to_thread 已经把它包成 awaitable
result = await heavy_calc(my_list)
```

适合：图片合成、数据分析、`requests` / `PIL.Image.thumbnail` 等无法异步化的库。
**不适合**：网络 I/O（应该用 `httpx.AsyncClient`）、数据库 I/O（应该用 `@with_session`）。

## 16.8 第三方 API 缓存：`@cache_data`

```python
from gsuid_core.utils.api.utils import cache_data

@cache_data
async def fetch_role_info(uid: str, role_id: int) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"https://api.example.com/role/{uid}/{role_id}")
        return r.json()
```

按"函数名 + 参数 hash"在内存中缓存结果（默认 TTL 由实现给定）。适合频繁查询、变更少的
外部接口（角色基础信息、武器图标 URL 等）。

## 16.9 群组 / 私聊批量播报：`send_board_cast_msg`

如果你已经手动构造好"哪些群 / 哪些私聊收哪条消息"的字典，可以一把推：

```python
from gsuid_core.utils.boardcast.send_msg import send_board_cast_msg
from gsuid_core.utils.boardcast.models import BoardCastMsgDict

msgs: BoardCastMsgDict = {
    "private_msg_dict": {
        "10001": [{"messages": "你的签到完成", "bot_id": "onebot"}],
    },
    "group_msg_dict": {
        "999888": {"messages": "群签到完成", "bot_id": "onebot"},
    },
}
await send_board_cast_msg(msgs)
```

> 一般场景下 **优先用 `gs_subscribe`**，[`send_board_cast_msg`](../references/16-common-utilities.md) 仅在你已经有外部数据源
> （比如从某 API 拉到的目标列表）时使用。

## 16.10 速查表：常用 import

```python
# —— 框架核心 ——
from gsuid_core.sv import SV, Plugins, get_plugin_available_prefix
from gsuid_core.bot import Bot
from gsuid_core.models import Event, Message
from gsuid_core.segment import MessageSegment
from gsuid_core.message_models import Button
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_start_before, on_core_shutdown
from gsuid_core.aps import scheduler
from gsuid_core.gss import gss
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.config import core_config

# —— 数据 / 资源 / 配置 ——
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database.base_models import (
    BaseIDModel, BaseBotIDModel, BaseModel, Bind, Push, with_session, async_maker,
)
from gsuid_core.utils.plugins_config.models import (
    GSC, GsStrConfig, GsBoolConfig, GsIntConfig, GsFloatConfig,
    GsListStrConfig, GsListConfig, GsDictConfig, GsImageConfig,
    GsTimeRConfig, GsDivider, GsFileUploadConfig, GsFilesUploadConfig,
    GsDateConfig, GsTimeRangeConfig, GsColorConfig, GsRepeatGroupConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

# —— Web 控制台 ——
from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site

# —— 帮助 / 状态 ——
from gsuid_core.help.utils import register_help
from gsuid_core.help.draw_new_plugin_help import get_new_help
from gsuid_core.help.model import PluginHelp
from gsuid_core.status.plugin_status import register_status

# —— 消息 / 推送 ——
from gsuid_core.utils.message import send_msg_to_master, send_diff_msg
from gsuid_core.utils.boardcast.send_msg import send_board_cast_msg

# —— 图片 / 渲染 ——
from gsuid_core.utils.image.image_tools import (
    get_color_bg, crop_center_img, easy_paste, easy_alpha_composite,
    draw_pic_with_ring, CustomizeImage,
)
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.html_render import (
    render_html_to_bytes, render_md_to_bytes, render_text_to_bytes,
)

# —— 通用工具 ——
from gsuid_core.utils.error_reply import (
    UID_HINT, CK_HINT, SK_HINT, VERIFY_HINT, get_error, get_error_img,
)
from gsuid_core.utils.cooldown import cooldown_tracker
from gsuid_core.utils.cache import gs_cache
from gsuid_core.utils.api.utils import cache_data
from gsuid_core.pool import to_thread

# —— AI 集成 ——
from gsuid_core.ai_core.trigger_bridge import ai_return
from gsuid_core.ai_core.register import ai_tools, ai_alias, ai_entity
from gsuid_core.ai_core.models import KnowledgePoint, ToolContext
from gsuid_core.ai_core.gs_agent import create_agent
```
