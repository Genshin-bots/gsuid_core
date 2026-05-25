---
name: gscore-plugin-development
description: >
  为 GsCore 机器人框架编写插件的完整指南。涵盖项目级目录规范（参照 ZZZeroUID / SayuStock）、
  Plugins/SV 双层架构、各类触发器的语义差异（on_command vs on_prefix vs on_fullmatch vs
  on_keyword vs on_regex vs on_message vs on_file）、消息收发、数据库操作并注册到网页控制台
  （site.register_admin / GsAdminModel）、订阅系统（gs_subscribe）、定时任务、配置管理、
  帮助系统（register_help + get_new_help）、推荐的渲染范式（优先 PIL → htmlkit
  → playwright 兜底）、AI 工具集成（@ai_tools、to_ai、ai_return、create_agent）、
  知识库 / 别名注册、启动钩子。

  当用户要求"帮我写一个 GsCore 插件"、"给这个插件加功能"、"改造触发器支持 AI"、
  "怎么用 to_ai"、"注册 ai_tools"、"写一个游戏查询插件"、"插件帮助怎么注册"、
  "如何把数据库表挂到网页控制台"、"PIL/htmlkit/playwright 哪个用哪个"时触发此 SKILL。
  对所有 GsCore 插件开发任务都应优先读取此 SKILL。
---

# GsCore 插件开发完整指南

> 本指南与 [`docs/LLM.md`](../../LLM.md)（代码红线）、[`docs/AI_AGENT_ARCHITECTURE.md`](../../AI_AGENT_ARCHITECTURE.md)（AI 代理层）
> 配套使用。插件代码要同时满足 LLM.md 中的类型 / 异步 / 数据库红线，AI 相关章节遵循 AI_AGENT_ARCHITECTURE.md
> 描述的 `to_ai`/`ai_tools`/`ai_return`/能力代理画像语义。范例参照 `gsuid_core/plugins/` 下的
> ZZZeroUID（多子模块嵌套加载）、SayuStock（含 playwright 渲染 + 股票 agent 注册）。

## 目录
- [一、插件基础结构](#一插件基础结构)
- [二、SV 与触发器](#二sv-与触发器)
- [三、消息收发](#三消息收发)
- [四、配置管理](#四配置管理)
- [五、数据库操作](#五数据库操作)
- [六、定时任务与订阅](#六定时任务与订阅)
- [七、启动 / 关闭 / Bot 上线钩子](#七启动--关闭--bot-上线钩子)
- [八、帮助系统注册](#八帮助系统注册)
- [九、图片渲染范式](#九图片渲染范式)
- [十、AI 集成：to_ai 与 ai_return](#十ai-集成to_ai-与-ai_return)
- [十一、AI 集成：@ai_tools 装饰器](#十一ai-集成ai_tools-装饰器)
- [十二、AI 集成：知识库与别名注册](#十二ai-集成知识库与别名注册)
- [十三、AI 集成：create_agent](#十三ai-集成create_agent)
- [十四、完整插件示例](#十四完整插件示例)
- [十五、常用工具模块速查](#十五常用工具模块速查)
- [十六、代码规范红线](#十六代码规范红线)

---

## 一、插件基础结构

### 1.1 推荐目录结构（嵌套加载模式）

参照 **ZZZeroUID** / **SayuStock** 等成熟插件，**推荐使用嵌套加载模式**——外层是
"插件包"（含 `pyproject.toml` / `README.md` / `LICENSE` / `ICON.png` / `__nest__.py`），
内层是与插件同名的 Python 包，所有业务子模块写在里面：

```
gsuid_core/plugins/MyPlugin/                 ← 插件根目录（仓库名）
├── __init__.py                              ← 外层入口（一般留空）
├── __nest__.py                              ← 空文件，标记"嵌套加载"
├── pyproject.toml                           ← 插件元数据 + 依赖
├── README.md
├── LICENSE
├── ICON.png                                 ← 插件图标（帮助 / webconsole 展示）
└── MyPlugin/                                ← 内层包（与插件目录同名）
    ├── __init__.py                          ← 定义 Plugins(...)，import 子模块触发注册
    ├── __full__.py                          ← 空文件，向框架标记"扫描子目录全部导入"
    ├── version.py                           ← MyPlugin_version = "1.0.0"
    ├── myplugin_bind/                       ← 业务子模块（前缀 = 插件名小写缩写）
    │   └── __init__.py
    ├── myplugin_roleinfo/                   ← 业务子模块
    │   ├── __init__.py
    │   ├── draw_roleinfo.py                 ← 图片渲染层（PIL / htmlkit）
    │   └── texture2d/                       ← 子模块专属静态资源
    ├── myplugin_help/                       ← 帮助子模块
    │   ├── __init__.py
    │   ├── get_help.py
    │   ├── help.json                        ← 帮助数据
    │   ├── icon_path/                       ← 各分组图标
    │   └── texture2d/                       ← 帮助底图
    ├── myplugin_config/                     ← 配置子模块
    │   ├── __init__.py                      ← 命令：开启/关闭/设置阈值 等
    │   ├── config_default.py                ← CONFIG_DEFAULT: Dict[str, GSC]
    │   └── myplugin_config.py               ← MYPLUGIN_CONFIG = StringConfig(...)
    └── utils/                               ← 共享工具（**不加前缀**）
        ├── database/
        │   ├── __init__.py
        │   └── models.py                    ← 数据模型 + @site.register_admin
        ├── resource/
        │   └── RESOURCE_PATH.py             ← 资源 / 配置 / 数据路径常量
        ├── image.py                         ← get_footer / get_ICON / 颜色相关
        ├── api.py                           ← 第三方 API 客户端
        └── hint.py                          ← 公共提示文案
```

**命名规范**（参照 GenshinUID / ZZZeroUID / SayuStock）：

| 规则 | 示例 |
|------|------|
| 插件目录名 | `MyPlugin/`（用户插件）；buildin 插件用 `_PluginName/` 前缀下划线 |
| 内部包名 | 与插件目录名同名（不含下划线前缀） |
| 入口文件 | 外层 `__init__.py` + 外层 `__nest__.py` + 内层 `__init__.py` + 内层 `__full__.py` |
| 子模块目录 | `{prefix}_{feature}/`，`prefix` 为插件名小写 |
| 共享工具 | `utils/`、`tools/`（不加前缀） |
| 配置模块 | `{prefix}_config/` |
| 静态资源 | 子模块内的 `texture2d/`（仅本模块用）或外层 `ICON.png`（插件级） |

### 1.2 入口三件套

**外层 `MyPlugin/__init__.py`**（可留空，仅作 Python 包标记）：
```python
"""init"""
```

**外层 `MyPlugin/__nest__.py`**（空文件，向框架声明启用嵌套加载）：
```python
```

> 关键作用：框架在加载插件目录时若发现 `__nest__.py`，会改为遍历内层包下所有子模块自动 import，
> 不需要在内层 `__init__.py` 手动一一 `from . import xxx`。配合 `__full__.py` 让 import 顺序稳定。

**内层 `MyPlugin/MyPlugin/__init__.py`**（声明 `Plugins`，触发子模块注册）：
```python
"""init"""
from gsuid_core.sv import Plugins

# Plugins 是单例：定义这一整个"插件"的前缀、权限、别名
# 同名 plugin_name 在框架内只创建一次，所有 SV 自动归属到它。
Plugins(
    name="MyPlugin",
    force_prefix=["mp", "我的插件"],   # 强制前缀，用户必须以此开头
    allow_empty_prefix=False,         # 不允许无前缀触发
    alias=["my_plugin"],              # 插件别名
)

# 若用了 __full__.py 嵌套加载，则不需要手动 import 子模块；
# 否则要在这里依次 from . import xxx，触发子模块的 @sv.on_xxx 注册。
```

**内层 `MyPlugin/MyPlugin/__full__.py`**（空文件）：
```python
```

### 1.3 `Plugins` vs `SV` 的层级关系

| | `Plugins` | `SV` |
|---|---|---|
| 层级 | **插件级**（整个项目） | **服务模块级**（一组功能） |
| 数量 | 一个插件目录一个 | 一个插件下可有多个（按业务分组） |
| 配置项 | `name` / `force_prefix` / `prefix` / `allow_empty_prefix` / `disable_force_prefix` / `alias` / `pm` / `area` | `name` / `pm` / `priority` / `enabled` / `area` / `black_list` / `white_list` |
| 持久化 | `plugin_config_store`（webconsole 可改） | `config_sv` 中（webconsole 可改） |
| 文件位置 | 内层 `__init__.py` 顶部 | 各子模块 `__init__.py` 顶部 |
| 创建语义 | 单例，重复声明返回同一实例 | 同 `name` 的 SV 是单例（跨文件共享） |

`SV("xxx")` 在内部会自动从调用栈推断"我属于哪个 plugin"（按 `plugins/` 或 `buildin_plugins/`
下一级目录名），不需要显式声明归属。

### 1.4 pyproject.toml（声明插件依赖）

```toml
[project]
name = "my-plugin"
version = "0.1.0"
dependencies = [
    "httpx>=0.24.0",
    "pillow>=9.0.0",
    "aiofiles>=23.0.0",
    # 仅当需要 HTML 渲染时（详见第九章）
    # "pyrenderhtml>=0.0.5",
    # 仅当 PIL / htmlkit 都不够时
    # "playwright>=1.49.0",
]
```

启动时自动安装 `dependencies` 中声明的依赖。`python`、`fastapi`、`pydantic`、`gsuid-core`、
`sqlmodel`、`apscheduler`、`pydantic-ai` 等框架基础依赖**无需重复声明**。

### 1.5 资源路径约定（推荐 `utils/resource/RESOURCE_PATH.py`）

把所有运行时数据 / 配置 / 缓存 / 用户数据的绝对路径集中在一个文件，业务代码统一引用，
避免到处散落 `get_res_path()`：

```python
# MyPlugin/MyPlugin/utils/resource/RESOURCE_PATH.py
import sys
from pathlib import Path
from gsuid_core.data_store import get_res_path

# 插件运行时根目录：data/MyPlugin/
MAIN_PATH = get_res_path() / "MyPlugin"
sys.path.append(str(MAIN_PATH))

# 各类持久化路径
CONFIG_PATH = MAIN_PATH / "config.json"        # 配置（StringConfig 用）
PLAYER_PATH = MAIN_PATH / "players"            # 用户数据
RESOURCE_PATH = MAIN_PATH / "resource"         # 远程下载的素材
CACHE_PATH = MAIN_PATH / "cache"               # 缓存
CU_BG_PATH = MAIN_PATH / "bg"                  # 自定义背景

for p in (PLAYER_PATH, RESOURCE_PATH, CACHE_PATH, CU_BG_PATH):
    p.mkdir(parents=True, exist_ok=True)
```

---

## 二、SV 与触发器

### 2.1 创建 SV 实例

```python
from gsuid_core.sv import SV

sv = SV(
    name="查询用户信息",     # 必填，和插件名不同, 代表这一组功能的统称
    pm=6,                  # 允许什么权限等级的用户触发, 默认为6, 最高为0
                           # 0代表仅限主人用户触发, 2代表允许群主及以上权限触发, 3代表允许管理员及其以上权限触发
    priority=5,            # 优先级，数字越小越先执行，默认 5
    enabled=True,          # 是否启用，默认 True
    area="ALL",            # 作用范围：GROUP / DIRECT / ALL
    black_list=[],         # 用户 ID 黑名单
    white_list=[],         # 用户 ID 白名单
)

add_sv = SV(name="查询帮助信息", pm=6, area="ALL") # 一般只需要定义这三项即可
```

同一 `name` 的 SV 是单例，多次创建会返回同一个实例，可跨文件共享。

### 2.2 触发器语义速查

所有触发器都遵循 `force_prefix + 关键词` 的匹配规则——框架会自动给关键词前置插件的
`force_prefix`/`prefix`。**`prefix=False` 才能完全绕开前缀机制**（一般不要这么做）。

| 装饰器 | 关键词必须 | 后接参数 | 关键词是否独占触发 | 典型场景 |
|--------|-----------|---------|-------------------|---------|
| `on_command(kw)` | ✅ 完全匹配 `kw` | **可选** | 是 | `/查询`、`/查询 雷神` 都触发 |
| `on_prefix(kw)` | ✅ 必须以 `kw` 开头 | **必须**（否则不触发） | 是 | `/查询 雷神` 触发；`/查询` 不触发 |
| `on_suffix(kw)` | ✅ 必须以 `kw` 结尾 | 前置任意内容 | 是 | `/雷神 帮助` 触发 |
| `on_fullmatch(kw)` | ✅ 整条消息 == `kw` | 不允许 | 是 | `/帮助` 触发；`/帮助 啊` 不触发 |
| `on_keyword(kw)` | ✅ 消息中包含 `kw` | 任意位置 | 是 | 含 "雷神" 就触发，全局扫描 |
| `on_regex(pattern)` | ✅ 整条消息 `re.search` 命中 | 由正则约束 | 是 | 复杂结构、分组提取 |
| `on_file(ext)` | ❌ 不看文本 | — | — | 收到 `.png` 等指定后缀文件 |
| `on_message(uid?)` | ❌ 任意消息都触发 | — | — | 监听全部消息（**慎用**） |

**关键对比要点**：

1. **`on_command` vs `on_prefix`**：唯一区别在"关键词后面没有参数时，前者照样触发、后者不触发"。
   - 想要"用户发 `查询` 也触发（走默认逻辑），发 `查询 雷神` 也触发（带参数）"→ 用 `on_command`。
   - 想要"必须 `查询 XXX` 才生效，纯 `查询` 不响应"→ 用 `on_prefix`。
   - 触发后两者都把"剩余文本"放在 `ev.text`、把"匹配到的关键词本身"放在 `ev.command`。

2. **`on_fullmatch` vs `on_keyword`**：前者是"整条消息 = 关键词"严格相等；后者是"消息里**任意位置**
   含关键词"。后者会污染全局消息流（任何消息只要含关键词就被截胡），用之前确认不会误伤。

3. **`on_regex` 的 `ev.regex_dict` 与 `ev.regex_group`**：
   ```python
   @sv.on_regex(r"查询\s*(?P<name>\S+)\s*的\s*(?P<attr>\S+)")
   async def regex_handler(bot: Bot, ev: Event):
       name = ev.regex_dict["name"]       # 命名分组
       attr = ev.regex_group[2]           # 位置分组（按出现顺序）
   ```

4. **`on_message` 关键警告**：会接收**所有消息**，框架内部会按 `priority` 排序所有触发器，
   `on_message` 在最低优先级。一旦你写了一个 `on_message` 不限制条件就 `await bot.send(...)`，
   机器人会复读所有消息——只用于"消息计数"、"被动观察"、"日志记录"等非应答场景。

5. **`on_file(ext)` 的 `ev.file_name`**：用户上传 `cat.png` 时，`ext` 必须是 `"png"`（不带点），
   `ev.file_name` 给你 `"cat.png"`，`ev.file` 为 `True`，但**真实文件内容**需要你自己从平台拉取
   （`ev.file_url` / 平台 SDK），框架不替你下载。

### 2.3 触发器装饰器的通用参数

所有 `on_xxx` 装饰器都支持以下通用参数：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `block` | `bool` | `False` | 匹配后阻止事件继续传递给其他低优先级触发器（防"多个触发器都响应同一条消息"） |
| `to_me` | `bool` | `False` | 是否必须 @ 机器人才触发（`ev.is_tome=True`） |
| `prefix` | `bool` | `True` | 是否给关键词自动拼上插件的 `force_prefix`/`prefix`（**几乎总是 True**） |
| `to_ai` | `str` | `""` | 非空时自动把本触发器注册为 AI 工具（详见第十章） |

**`block=True` 使用建议**：
- 高优先级"短路"触发器（如插件级帮助、错误提示）应 `block=True`，避免兜底触发器再次响应。
- 一般业务命令保持 `block=False`，由 `priority` 控制顺序。

### 2.4 触发器注册示例

```python
# on_command：用户发 "查询 雷电将军" 触发，ev.text = "雷电将军"
# 用户发 "查询" 同样触发，ev.text = ""——可在此走"无参提示"分支
@sv.on_command("查询")
async def query_handler(bot: Bot, ev: Event) -> None:
    name = ev.text.strip()
    if not name:
        return await bot.send("请输入角色名，例如：查询 雷电将军")
    await bot.send(f"查询角色: {name}")

# on_prefix：用户必须发 "查询 雷电将军" 才触发；纯 "查询" 不会进入这里
@sv.on_prefix("查询")
async def query_strict(bot: Bot, ev: Event) -> None:
    name = ev.text.strip()   # 保证非空
    await bot.send(f"查询角色: {name}")

# on_suffix：用户发 "雷电将军 帮助" 触发；ev.text="雷电将军"、ev.command="帮助"
@sv.on_suffix("帮助")
async def suffix_help(bot: Bot, ev: Event) -> None:
    target = ev.text.strip()
    await bot.send(f"以下是 {target} 的帮助：")

# on_fullmatch：精确匹配 "帮助"，多一个字都不触发
@sv.on_fullmatch("帮助", block=True)
async def show_help(bot: Bot, ev: Event) -> None:
    await bot.send("这是帮助")

# 多关键词复用同一处理函数（任一关键词命中都触发）
@sv.on_command(("绑定", "bind", "绑定UID"))
async def bind_handler(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    await bot.send(f"绑定 UID: {uid}")

# on_regex：复杂结构 + 命名分组
@sv.on_regex(r"查询\s*(?P<name>\S+)\s*的\s*(?P<attr>\S+)")
async def regex_handler(bot: Bot, ev: Event) -> None:
    name = ev.regex_dict.get("name", "")
    attr = ev.regex_dict.get("attr", "")
    await bot.send(f"角色: {name}, 属性: {attr}")

# on_file：用户上传 .png 文件触发
@sv.on_file("png")
async def file_handler(bot: Bot, ev: Event) -> None:
    await bot.send(f"收到图片：{ev.file_name}")
```

### 2.5 处理函数签名规范

所有处理函数必须遵循此签名，**不得更改**：

```python
from gsuid_core.bot import Bot
from gsuid_core.models import Event

async def my_handler(bot: Bot, ev: Event) -> None:
    ...
```

> **`Bot` vs `_Bot` 提醒**：触发器拿到的 `bot: Bot` 是高层封装（含 `Event` 引用），
> 提供 `send()` / `receive_resp()` / `target_send()` 等业务 API。框架内部连接管理用的
> `_Bot` 是低层实现，**触发器代码绝不要直接 import `_Bot`**——详见 LLM.md §8。

---

## 三、消息收发

### 3.1 Event 对象常用属性

```python
ev.user_id         # str：发送者用户 ID
ev.group_id        # str | None：群 ID（私聊为 None）
ev.bot_id          # str：Bot 标识
ev.bot_self_id     # str：机器人自身 ID
ev.raw_text        # str：原始消息文本（含前缀+命令）
ev.text            # str：去掉前缀+命令后的参数部分
ev.command         # str：匹配到的命令关键词
ev.is_tome         # bool：是否 @ 了机器人
ev.at              # str | None：被 @ 的用户 ID
ev.message         # list：消息段列表
ev.user_nickname   # str：用户昵称
ev.file            # bool：是否有文件附件
ev.file_name       # str | None：文件名
ev.regex_dict      # dict：正则命名分组 (?P<name>...)
ev.regex_group     # tuple：正则位置分组
```

### 3.2 Bot 发送方法

```python
from gsuid_core.segment import MessageSegment
from gsuid_core.message_models import Button

# ----- 发送纯文本 -----
await bot.send("Hello!")                      # 直接传字符串
await bot.send(MessageSegment.text("Hello!")) # 或显式构造 Message
# 多段文本可放在列表中
await bot.send(["第一行", "第二行"])

# ----- 发送图片 -----
# 支持 bytes、base64 字符串、URL、Path、PIL.Image
await bot.send(MessageSegment.image(b"图片字节"))
await bot.send(MessageSegment.image("base64://...."))
await bot.send(MessageSegment.image("https://example.com/img.png"))
await bot.send(MessageSegment.image(Path("local.png")))

# ----- 发送 Markdown（仅部分平台支持）-----
await bot.send(MessageSegment.markdown("# 标题\n内容"))
# 也可携带按钮
await bot.send(MessageSegment.markdown(
    "请选择：",
    [[Button("选项1"), Button("选项2")]]
))

# ----- 发送按钮（仅部分平台支持）-----
await bot.send([
    MessageSegment.text("点击下方按钮："),
    MessageSegment.buttons([[Button("确认"), Button("取消")]])
])

# ----- @某人 -----
await bot.send([MessageSegment.text("提醒："), MessageSegment.at("123456")])

# ----- 合并转发节点 -----
await bot.send(MessageSegment.node(["消息1", "消息2"]))

# ----- 发送文件 / 语音 / 视频 -----
await bot.send(MessageSegment.file(Path("doc.pdf"), file_name="doc.pdf"))
await bot.send(MessageSegment.record("base64://..."))
await bot.send(MessageSegment.video("https://example.com/video.mp4"))
```

### 3.3 **强烈推荐：用 `bot.send_option` 发选项 / 按钮**

只要消息后面跟着的是"用户该如何接着发"的选项，**首选 `bot.send_option`** 而不是
`bot.send` 拼一行"请输入：xxx / yyy / zzz"。理由：

- 在支持按钮的平台（QQ Bot 官方频道、QQ 机器人 Markdown 模板、Telegram、Discord 等），
  框架会自动渲染为可点击按钮，体验远好于让用户手敲命令。
- 在**不支持按钮**的平台，框架会自动 fallback 为纯文本"请输入以下命令之一: ..."，
  你只要传 `unsuported_platform=True` 它就帮你兜底——一份代码全平台通吃。
- `bot.send_option` 内部调用 `receive_resp(is_recive=False)`，**不阻塞、不等待回复**，
  纯粹发选项；想"发完等用户点一个"用 `bot.receive_resp(... option_list=[...])`。

```python
from gsuid_core.message_models import Button

@sv.on_fullmatch("菜单")
async def show_menu(bot: Bot, ev: Event) -> None:
    # 单行选项：3 个按钮自动布局
    await bot.send_option(
        reply="请选择你要查询的内容：",
        option_list=["查角色", "查武器", "查抽卡记录"],
        unsuported_platform=True,   # 不支持按钮的平台自动转纯文本
    )

@sv.on_fullmatch("绑定向导")
async def bind_wizard(bot: Bot, ev: Event) -> None:
    # 多行布局：每行一组
    await bot.send_option(
        reply="选择绑定类型：",
        option_list=[
            ["绑定UID", "绑定米游社ID"],
            ["绑定Cookie", "扫码登陆"],
            ["取消"],
        ],
        unsuported_platform=True,
    )

@sv.on_fullmatch("确认")
async def confirm_with_callback(bot: Bot, ev: Event) -> None:
    # 自定义按钮：Button(text, data, click_text)
    # - text: 按钮显示文字
    # - data: 点击后**作为下一条消息发送**给机器人（驱动下一轮触发器）
    # - click_text: 点击后给用户的反馈短语（可选）
    await bot.send_option(
        reply="是否确认绑定 UID 12345678？",
        option_list=[
            Button("✅ 确认", "确认绑定 12345678", "已确认"),
            Button("❌ 取消", "取消绑定", "已取消"),
        ],
        unsuported_platform=True,
    )
```

**`Button(text, data, click_text)` 关键点**：
- `data` 是**真正会被发送给机器人**的命令文本——点了"确认"按钮就相当于用户发了 `data` 这条
  消息，可以让框架重新走 `@sv.on_xxx` 触发链。
- 同一个 `option_list` 里混用 `str` 和 `Button` 都可以，纯字符串等价于 `Button(s, s, s)`。

**什么时候用 `bot.send`，什么时候用 `bot.send_option`**：

| 场景 | 用哪个 |
|------|--------|
| 单纯通知 / 结果 / 错误信息（没有"下一步"） | `bot.send` |
| 让用户从 N 个选项里挑一个继续 | `bot.send_option(... unsuported_platform=True)` |
| 让用户挑一个并等他回复 | `bot.receive_resp(... option_list=[...], unsuported_platform=True)` |
| 让群里**任何人**挑一个并等回复 | `bot.receive_mutiply_resp(... option_list=[...], unsuported_platform=True)` |

### 3.4 多步会话（Response）

用于需要用户多次交互的场景，分为**单用户响应**和**多用户响应**两种模式。

#### 单用户响应

仅接收触发命令的同一用户后续消息。

```python
@sv.on_fullmatch("开始测试")
async def get_resp_msg(bot: Bot, ev: Event):
    await bot.send("开始多步会话测试")

    # 发送提示并等待该用户回复（默认超时60秒）
    resp = await bot.receive_resp("接下来你说的话我都会提取出来噢？")
    if resp is not None:
        await bot.send(f"你说的是 {resp.text} 吧？")
```

#### 多用户响应

接收群内任意用户的后续消息，常用于游戏、投票等场景。

```python
@sv.on_fullmatch("开始多用户测试")
async def get_resp_msg(bot: Bot, ev: Event):
    await bot.send("开始多步会话测试")
    await bot.send("接下来开始游戏！？所有人的会话我都会收集起来的哦！")
    while True:
        resp = await bot.receive_mutiply_resp()
        if resp is not None:
            await bot.send(f"你说的是 {resp.text} 吧？")
```

如需限制收集时长，可配合 `asyncio.timeout`（Python 3.11+）或 `async_timeout`：

```python
@sv.on_fullmatch("开始一场60秒的游戏")
async def get_time_limit_resp_msg(bot: Bot, ev: Event):
    await bot.send("接下来开始60秒的游戏！？")
    try:
        async with asyncio.timeout(60):  # 限制时长60秒
            while True:
                resp = await bot.receive_mutiply_resp()
                if resp is not None:
                    await bot.send(f"你说的是 {resp.text} 吧？")
    except asyncio.TimeoutError:
        await bot.send("时间到!!现在开始计算每个人的分数...")
```

#### 主要方法对比

| 方法 | 说明 |
|------|------|
| `bot.send_option(...)` | 发送按钮或选项提示，**不等待**回复。 |
| `bot.receive_resp(...)` | 发送可选消息，并等待**触发命令用户**的下一条消息。 |
| `bot.receive_mutiply_resp(...)` | 发送可选消息，并等待**群内任意用户**的后续消息。 |

`receive_mutiply_resp` 和 `send_option` 内部均调用 `receive_resp`，因此参数基本一致。

#### 常用参数

- **`reply`**：可填入 `bot.send()` 接受的任何值（字符串、`Message`、`MessageSegment` 等），会在等待回复前先发送一次消息。
- **`option_list`**：类型 `List[str]`、`List[Button]`、`List[List[str]]` 或 `List[List[Button]]`，用于生成按钮或多选提示（部分平台支持）。
- **`timeout`**：等待回复的超时时间（秒），默认 `60`。
- **`unsuported_platform`**：当平台不支持按钮时，是否转为发送多选文本提示（默认 `False`）。
- **`sep`**、**`command_tips`**、**`command_start_text`**：在文本模式下自定义选项分隔符和提示语。

完整参数可参考代码中 `Bot.receive_resp` 的签名。

---

## 四、配置管理

### 4.1 定义插件配置项

在插件目录（如 `my_plugin/`）下创建文件夹 `my_plugin_config`，在其中创建 `config_default.py`，使用 `Dict[str, GSC]` 定义默认配置。

```python
# my_plugin/config_default.py
from typing import Dict
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsStrConfig,
    GsBoolConfig,
    GsIntConfig,
    GsListStrConfig,
)

CONFIG_DEFAULT: Dict[str, GSC] = {
    "api_key": GsStrConfig(
        title="API Key",
        desc="第三方服务的 API Key",
        data="",
    ),
    "max_count": GsIntConfig(
        title="最大查询数量",
        desc="单次最多返回多少条结果",
        data=10,
    ),
    "enable_cache": GsBoolConfig(
        title="启用缓存",
        desc="是否缓存查询结果",
        data=True,
    ),
    "blocked_users": GsListStrConfig(
        title="屏蔽用户列表",
        desc="不响应的用户 ID",
        data=[],
    ),
}
```

> **注意**：所有配置类型的字段名是 `title`、`desc`、`data`，而非示例代码中的 `title`、`description`、`default`。

### 4.2 创建配置实例并注册到 Web 控制台

在插件配置文件夹（如 `my_plugin/my_plugin_config/my_plugin_config.py`）中：

- 用 `get_res_path()` 获取插件资源目录并指定配置文件路径。
- 创建 `StringConfig` 单例。

```python
# my_plugin/__init__.py
from gsuid_core.sv import SV
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from .config_default import CONFIG_DEFAULT

sv = SV("my_plugin")

# 配置文件路径
CONFIG_PATH = get_res_path() / "my_plugin" / "config.json"

# 创建配置实例（全局单例，config_name 应唯一）
my_config = StringConfig("my_plugin", CONFIG_PATH, CONFIG_DEFAULT)


```

### 4.3 读取与修改配置

在其他模块中直接导入 `my_config` 使用。

```python
from my_plugin.my_plugin_config.my_plugin_config import my_config

# 读取配置值（注意 .data）
api_key: str = my_config.get_config("api_key").data          # GsStrConfig
max_count: int = my_config.get_config("max_count").data      # GsIntConfig
enable_cache: bool = my_config.get_config("enable_cache").data  # GsBoolConfig

# 运行时修改（自动持久化到文件）
my_config.set_config("api_key", "new_key")
my_config.set_config("max_count", 20)
```

> **提示**：`set_config` 会校验类型，类型不匹配将拒绝写入并打印警告。

### 4.4 完整示例目录结构

```
my_plugin/
├── __init__.py          # 包含 my_config 实例和 __plugin_config_class__
├── config_default.py    # 配置项定义
├── ...
```

### 4.5 所有可用配置类型

从 `gsuid_core.utils.plugins_config.models` 导入：

```python
from gsuid_core.utils.plugins_config.models import (
    GsStrConfig,      # 字符串
    GsBoolConfig,     # 布尔
    GsIntConfig,      # 整数 (可限制 max_value)
    GsListStrConfig,  # 字符串列表
    GsListConfig,     # 整数列表
    GsDictConfig,     # 字典
    GsImageConfig,    # 图片配置 (上传相关)
    GsTimeRConfig,    # 时间点配置 (定时任务配置相关)
)
# 联合类型 GSC = Union[GsStrConfig, GsBoolConfig, GsIntConfig, GsListStrConfig, GsListConfig, GsDictConfig, GsImageConfig]
```

---

## 五、数据库操作

GsCore 使用 SQLModel 作为 ORM，**所有数据库操作必须在模型类内部**，使用 `@with_session` 装饰器管理会话。

### 5.1 三级基类

```python
from gsuid_core.utils.database.base_models import (
    BaseIDModel,      # 只有 id 字段（自增主键）
    BaseBotIDModel,   # id + bot_id
    BaseModel,        # id + bot_id + user_id  ← 最常用
)
```

### 5.2 定义数据模型

```python
# utils/database/models.py
from typing import Optional
from sqlmodel import Field
from gsuid_core.utils.database.base_models import BaseModel, with_session
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


class GameBind(BaseModel, table=True):
    """游戏账号绑定表"""
    uid: str = Field(title="游戏 UID")
    region: str = Field(default="cn", title="大区")
    cookie: Optional[str] = Field(default=None, title="Cookie")

    @classmethod
    @with_session
    async def get_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str
    ) -> Optional["GameBind"]:
        """根据用户 ID 查询绑定"""
        stmt = (
            select(cls)
            .where(cls.user_id == user_id)
            .where(cls.bot_id == bot_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def bind_uid(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        region: str = "cn",
    ) -> "GameBind":
        """绑定或更新 UID"""
        existing = await cls.get_bind(user_id, bot_id)
        if existing:
            existing.uid = uid
            existing.region = region
            session.add(existing)
            return existing
        bind = cls(user_id=user_id, bot_id=bot_id, uid=uid, region=region)
        session.add(bind)
        return bind

    @classmethod
    @with_session
    async def get_uid_list(
        cls, session: AsyncSession, user_id: str, bot_id: str
    ) -> list[str]:
        """获取用户所有绑定的 UID 列表"""
        stmt = (
            select(cls.uid)
            .where(cls.user_id == user_id)
            .where(cls.bot_id == bot_id)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def delete_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str, uid: str
    ) -> bool:
        """删除绑定"""
        stmt = (
            select(cls)
            .where(cls.user_id == user_id)
            .where(cls.bot_id == bot_id)
            .where(cls.uid == uid)
        )
        result = await session.execute(stmt)
        bind = result.scalar_one_or_none()
        if bind is None:
            return False
        await session.delete(bind)
        return True
```

### 5.3 `@with_session` 规则

- **必须是 `classmethod`** 且 **`async def`**
- `session: AsyncSession` 必须是第二个参数（紧跟 `cls`）
- 装饰器自动 commit，异常自动回滚
- `@with_session` 已处理事务，**不要**在方法内手动 `await session.commit()`

### 5.4 类方法外的手动 session

```python
from gsuid_core.utils.database.base_models import async_maker

async def batch_cleanup():
    async with async_maker() as session:
        from sqlalchemy import delete
        stmt = delete(GameBind).where(GameBind.cookie == None)
        await session.execute(stmt)
        await session.commit()
```

### 5.5 把数据库表注册到 Web 控制台

参照 ZZZeroUID / SayuStock 的写法，给业务表加一个 `@site.register_admin` 装饰的
`GsAdminModel` 子类，Web 控制台启动后会自动出现该表的可视化管理页（增删改查 + 字段过滤）。

```python
# MyPlugin/utils/database/models.py
from typing import Optional
from sqlmodel import Field

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import Push  # 或 Bind / BaseModel


class MyPush(Push, table=True):
    __table_args__ = {"extend_existing": True}
    bot_id: str = Field(title="平台")
    my_uid: str = Field(default=None, title="游戏 UID")

    # title / schema_extra.hint 会在 webconsole 表格中作为列头 / 提示文字展示
    energy_push: Optional[str] = Field(
        title="体力推送",
        default="off",
        schema_extra={"json_schema_extra": {"hint": "mp 开启体力推送"}},
    )
    energy_value: Optional[int] = Field(title="电量阈值", default=180)
    energy_is_push: Optional[str] = Field(title="电量是否已推送", default="off")


@site.register_admin
class MyPushAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="MyPlugin 推送管理",   # 左侧菜单显示文本
        icon="fa fa-bullhorn",       # Font Awesome 图标
    )  # type: ignore

    # 把上面的 SQLModel 表绑定到这个 admin 页
    model = MyPush
```

**注意要点**：
- `Push` / `Bind` / `BaseModel` 等基类已经在 `utils/database/base_models.py` 中包含 `bot_id` / `user_id` / `uid` 等
  公共字段；按业务实际需要选基类。
- `__table_args__ = {"extend_existing": True}` 必加——允许同名表在多次 reload 时重新绑定。
- `Field` 的 `title=...` 同时是 SQLModel 字段元数据和 webconsole 列标题。
- `page_schema.icon` 取 [Font Awesome 4](https://fontawesome.com/v4/icons/) 图标名（不带版本号）。
- 业务字段的 `schema_extra={"json_schema_extra": {"hint": "..."}}` 会渲染为输入框下方的提示。

### 5.6 在触发器中使用数据库

```python
@sv.on_command(("绑定", "bind"))
async def bind_uid(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    if not uid or not uid.isdigit():
        return await bot.send("请输入正确的 UID（纯数字）")

    await GameBind.bind_uid(ev.user_id, ev.bot_id, uid)
    await bot.send(f"✅ 已绑定 UID: {uid}")

@sv.on_fullmatch("我的UID")
async def show_uid(bot: Bot, ev: Event) -> None:
    uid_list = await GameBind.get_uid_list(ev.user_id, ev.bot_id)
    if not uid_list:
        return await bot.send("您还没有绑定 UID，发送 '绑定 您的UID' 进行绑定")
    await bot.send("您绑定的 UID：\n" + "\n".join(uid_list))
```

---

## 六、定时任务与订阅

### 6.1 使用 APScheduler

```python
from gsuid_core.aps import scheduler

# cron 表达式：每天 8:30 执行
@scheduler.scheduled_job("cron", hour=8, minute=30)
async def daily_task():
    # 需要主动获取 bot 实例向用户推送
    from gsuid_core.gss import gss
    for bot_id, bot in gss.active_bot.items():
        await bot.target_send(
            bot_id=bot_id,
            target_type="group",
            target_id="目标群ID",
            message="今日早报",
        )

# interval：每 30 分钟执行一次
@scheduler.scheduled_job("interval", minutes=30)
async def refresh_cache():
    await do_cache_refresh()

# 一次性：在指定时间执行
from datetime import datetime, timedelta
scheduler.add_job(
    func=one_time_task,
    trigger="date",
    run_date=datetime.now() + timedelta(hours=1),
)
```

### 6.2 主动推送强制规范

**所有主动消息（签到提醒、阈值预警、公告推送、新版本提示、运营消息）一律走 `gs_subscribe`
订阅系统**，**不要**在定时任务里裸 `for bot_id, bot in gss.active_bot.items(): await bot.target_send(...)`
硬塞群号 / 用户 ID。

| 反模式（不要这样写） | 正确做法 |
|----------------------|---------|
| `for _, bot in gss.active_bot.items(): await bot.target_send(...)` 硬编码群号 | `for sub in await gs_subscribe.get_subscribe("XX"): await sub.send(...)` |
| 自己维护"哪个群订阅了哪个功能"的字典 | 让用户走"开启 XX 推送"命令，由 `gs_subscribe.add_subscribe` 持久化到数据库 |
| 跨进程 / 重启后丢失订阅状态 | 订阅持久化到 `subscribe` 表，重启不丢；webconsole 还有可视化管理（参见 §5.5） |
| 推 master / 主人时遍历 active_bot | 直接 `from gsuid_core.utils.message import send_msg_to_master; await send_msg_to_master(msg)` |

**`gs_subscribe` 自动路由的好处**：
- 自动按平台分发（QQ / OneBot / 飞书 / Discord ...），开发者不用 care 哪条订阅来自哪个 WS Bot。
- WS 连接断开重连后 `WS_BOT_ID` 变了，框架会自动回退到 `bot_id` 找对应活跃 Bot 并修正订阅记录。
- 同一 task_name 在 webconsole "订阅管理"页面可视化展示，运维容易。
- `sub.send()` **内部走 `bot.send_option`**——同样支持选项 / 按钮 / 不支持平台 fallback。

### 6.3 订阅 API 全集

**`add_subscribe`：注册订阅（用户主动触发）**

```python
from gsuid_core.subscribe import gs_subscribe

@sv.on_fullmatch("开启每日早报")
async def subscribe_notice(bot: Bot, ev: Event):
    await gs_subscribe.add_subscribe(
        subscribe_type="session",   # 见下表
        task_name="每日早报",         # 全局唯一的任务名（约定带插件前缀）
        event=ev,
        extra_message=None,          # 可存阈值 / 元数据等（字符串）
        uid=None,                    # 可绑定到某个 UID（多账户场景）
        extra_data=None,             # 第二个额外字段
    )
    await bot.send("✅ 已订阅每日早报！")
```

| `subscribe_type` | 行为 |
|-----------------|------|
| `"session"`     | 同一群 / 同一私聊**只保留一条**记录——公告 / 单实例推送 |
| `"single"`      | 同一群可保存**多条**（如多账号签到），同一私聊仍只一条 |

**`get_subscribe`：拉取订阅列表（在定时任务里用）**

```python
subs = await gs_subscribe.get_subscribe(
    task_name="每日早报",
    # 下面四个都可选，按需精确过滤；不传则返回所有该 task_name 的订阅
    user_id=None,
    bot_id=None,
    user_type=None,
    uid=None,
    WS_BOT_ID=None,
)
```

**`delete_subscribe`：删除订阅（用户主动取消）**

```python
await gs_subscribe.delete_subscribe("session", "每日早报", ev, uid=None)
```

**`update_subscribe_message` / `update_subscribe_data`：更新阈值或 extra 字段**

```python
# 用户改阈值：mp 设置体力阈值 180
await gs_subscribe.update_subscribe_message(
    "single", "[MyPlugin] 体力", ev, extra_message="180",
)
```

**`sub.send(...)`：推送方法**——一个订阅记录就是一个目标会话，参数和 `bot.send_option` 一致：

```python
@scheduler.scheduled_job("cron", hour=8)
async def send_daily_notice():
    subs = await gs_subscribe.get_subscribe("每日早报")
    if not subs:
        return
    for sub in subs:
        # sub.send 内部自动路由到对应平台 / Bot，且支持 option_list
        await sub.send(
            reply="📢 早报：今日维护已完成。",
            option_list=["查看详情", "暂停推送"],
            unsuported_platform=True,
        )
        # sub.extra_message 拿订阅时存的阈值
        # sub.uid 拿绑定的游戏 UID
        # sub.group_id / sub.user_id / sub.user_type 等均可读
```

> **提示**：`sub.send(force_direct=True)` 可把消息强制走私聊（即便订阅是 group 类型），
> `send_msg_to_master` 的"推送给主人"就是这么实现的（详见 §16）。

### 6.4 定时任务的硬约束

- **定时任务函数没有 `bot` / `ev` 注入**——所有 Bot 句柄要么从 `gs_subscribe` 拿、要么
  从 `gss.active_bot` 主动取（但后者一般只用于纯系统任务如缓存清理，不发用户消息）。
- 定时任务里 `raise` 的异常会被 APScheduler 吞掉，**必要时自己 `try/except` + `logger.exception`**。
  这一处异常处理是 §16 红线之外的特例。
- 短周期任务（< 5 分钟）频繁查库时记得加 `@gs_cache(expire_time=...)`（详见 §15）。

你还可以通过 `extra_message` 参数在订阅时保存额外数据，并在发送时通过 `sub.extra_message` 读取。


---

## 七、启动 / 关闭 / Bot 上线钩子

GsCore 提供 **4 个生命周期钩子**：两个启动期（阻塞前 + 启动后）、一个关闭期、一个 Bot 上线
回调。同一钩子可在不同模块重复注册，框架会按优先级合并并发执行。

### 7.1 钩子总览

| 钩子 | 时机 | 是否阻塞主流程 | 典型用途 |
|------|------|----------------|---------|
| `on_core_start_before` | **WS 服务启动前**（所有 Bot 都未连上来） | ✅ 阻塞——执行完才接连接 | 数据库迁移、表结构变更、必须先做完的全局初始化 |
| `on_core_start` | **WS 服务启动后** | ❌ 后台并发 | 缓存预热、资源下载、注册 AI 知识库、起后台任务 |
| `on_core_shutdown` | 进程**关闭前** | ✅ 阻塞 | 刷新缓冲、关数据库连接、保存中间状态 |
| `gss.on_bot_connect` | **每次** Bot 通过 WS 连上来时 | ❌ 后台并发 | 启动检查、推送遗留消息、与 Bot 自身相关的初始化 |

> **`on_core_start_before` vs `on_core_start` 关键区分**：前者在框架还没开放 WS 端口前就跑，
> 期间所有 Bot 连接尝试都被阻塞——所以"必须在用户能调用任何命令之前完成"的逻辑（schema 升级、
> 必须存在的配置文件预生成、`StringConfig` 字段补全）一律放 `on_core_start_before`；
> 后者在 WS 端口已开放之后**后台**跑，Bot 可能已经在收消息了，所以**不要**把"必须完成"的
> 初始化放这里。
>
> **`gss.on_bot_connect` vs `on_core_start`**：前者每次有新 Bot 连进来都会跑（包括重连），
> 后者只在框架启动一次；想"框架启动后做一次"用 `on_core_start`，想"每个 Bot 上线时单独做"
> 用 `gss.on_bot_connect`。

### 7.2 `on_core_start_before`（启动前阻塞钩子）

```python
from gsuid_core.server import on_core_start_before

# 不带括号：默认 priority=0
@on_core_start_before
async def upgrade_schema():
    await migrate_table()

# 带括号：自定义 priority（越小越先；同优先级并发）
@on_core_start_before(priority=10)
async def warm_critical_cache():
    await load_essential_data()
```

- **同步函数也支持**——框架内部用 `asyncio.to_thread` 包装；但能 async 就 async。
- 钩子内部抛异常会被框架捕获并 `logger.exception`，**不会**让进程崩溃，但**也不会**让插件
  的剩余初始化逻辑中断——所以 schema 迁移失败要明确告警 + 数据库标记，下一步用户的写操作
  自己会失败。

### 7.3 `on_core_start`（启动后后台钩子）

```python
from gsuid_core.server import on_core_start

@on_core_start(priority=5)
async def warmup_cache():
    await prefetch_role_icons()
    await build_alias_index()

@on_core_start
async def register_ai_knowledge():
    # 等 RAG 初始化完后再注册（priority 留 0 即可）
    from gsuid_core.ai_core.register import ai_entity
    ai_entity(KnowledgePoint(...))
```

### 7.4 `on_core_shutdown`（关闭前钩子）

进程收到 SIGTERM / Ctrl+C / `core重启` 命令时执行；用来**收尾**——刷写未持久化的缓冲、
关掉后台 task、保存中间状态。参考 `_XutheringWavesUID` 的写法：

```python
import asyncio
from gsuid_core.server import on_core_shutdown
from gsuid_core.logger import logger

_shutdown_event = asyncio.Event()
_flush_task = asyncio.get_event_loop().create_task(_activity_flush_loop())


@on_core_shutdown
async def _flush_on_shutdown():
    """退出前刷写缓冲区，防止数据丢失"""
    logger.info("[MyPlugin] 退出前刷写中...")
    _shutdown_event.set()
    try:
        await asyncio.wait_for(_flush_task, timeout=5)
    except asyncio.TimeoutError:
        logger.warning("[MyPlugin] 刷写超时，强制退出")
```

- 钩子有**总时间预算**，超时会被强制中止——内部务必加 `asyncio.wait_for(..., timeout=N)`。
- 同样支持 `priority` 参数；越小越先关闭。

### 7.5 `gss.on_bot_connect`（Bot 上线回调）

每次 Bot 通过 WS 连接到 core 时触发（首次连接 + 重连都触发）。无参数，无 priority。

```python
import asyncio
from gsuid_core.gss import gss
from gsuid_core.logger import logger

@gss.on_bot_connect
async def check_pending_messages():
    """Bot 上线后稍等一会儿，把启动期间积压的提醒推出去"""
    try:
        await asyncio.sleep(2)                # 等 Bot 自身完成握手
        await flush_pending_notifications()
    except Exception as e:
        logger.warning(f"[MyPlugin] 启动检查失败: {e}")
```

- 注册方式**和其他钩子不一样**——它是 `GsServer.on_bot_connect` 类方法，通过 `gss` 实例
  使用：`@gss.on_bot_connect`，**不是** `@on_bot_connect`。
- 函数签名**没有参数**——内部如需知道"哪个 Bot 上线了"应自己遍历 `gss.active_bot`。
- **同名同模块**重复注册会被去重，所以热重载时不会累积。
- 框架在 Bot 连进来之后会并发触发所有 `bot_connect_def`，**异常会被吞**——务必自己 `try/except`。

### 7.6 常见使用场景速查

| 场景 | 选哪个钩子 | 注意点 |
|------|-----------|--------|
| 数据库表结构变更 / 字段补全 | `on_core_start_before` | 阻塞所有 Bot 连接直到完成 |
| 加载全局配置 / 修复配置文件 | `on_core_start_before` | 避免运行时配置类抖动 |
| 注册 AI 知识库内容（`ai_entity`） | `on_core_start` | 等 RAG 初始化完 |
| 预热 HTTP / 图片缓存 | `on_core_start` | 后台跑、不阻塞用户命令 |
| 启动后台监控 / 数据同步任务 | `on_core_start` | 配合 `asyncio.create_task` |
| 推送遗留消息（重启前未处理的通知） | `gss.on_bot_connect` | Bot 在线才能推 |
| 启动检查（向主人汇报 Bot 上线） | `gss.on_bot_connect` | 注意防止重连刷屏 |
| 关闭前刷写缓冲 / 落盘 | `on_core_shutdown` | 加 `asyncio.wait_for` 超时 |
| 关闭前保存 task 状态 | `on_core_shutdown` | 不要在这里跑长任务 |

---

## 八、帮助系统注册

GsCore 提供统一的"插件帮助一览"页面，每个插件应注册一项条目，并提供发送图片帮助的命令。

### 8.1 `register_help` —— 把插件挂到一览页

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

### 8.2 `get_new_help` —— 渲染插件自身的帮助图

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

### 8.3 注册到 "core 状态"（`register_status`）

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

---

## 九、图片渲染范式

GsCore 中**首选 PIL 直接绘图**——可控、轻量、无外部依赖、跨平台稳定。仅当 PIL 表达能力
不够（复杂表格 / 富文本 / 图表）时才升级到 HTML 渲染。

### 9.1 三档渲染方案（优先级从高到低）

| 档位 | 库 | 适用场景 | 主要缺点 |
|------|----|---------|---------|
| **① PIL（首选）** | `Pillow` + `gsuid_core.utils.image.image_tools` | 角色面板 / 卡片 / 排行榜 / 半结构化展示 | 排版手写、长文本麻烦 |
| **② htmlkit（推荐）** | `gsuid_core.utils.html_render.render_html_to_bytes / render_md_to_bytes` | Markdown 报告 / 表格 / 简单 HTML | 不能跑 JS、不渲染 SVG 动画 |
| **③ playwright（兜底）** | `playwright.async_api.async_playwright` | 需要 JS / Plotly / ECharts / 图表交互的复杂可视化 | 启动重、依赖 chromium、首次需 `playwright install` |

> **决策口诀**：
> - 能用 PIL 拼出来的，就不要走 HTML。
> - 能用 htmlkit 渲染纯 HTML / Markdown 的，就不要拉 playwright。
> - 只有"非 JS 引擎渲染不出来"的图（K线、云图、3D Plotly）才上 playwright，并显式声明
>   `playwright>=1.49.0` 依赖、写明用户需手动 `playwright install`。

### 9.2 PIL 范式（首选）

利用 `gsuid_core.utils.image.image_tools` 提供的**复用度极高**的工具函数：

```python
from PIL import Image, ImageDraw

from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.image.image_tools import (
    get_color_bg,        # 自动从图库 + 主色 mask 生成背景
    crop_center_img,     # 居中裁切到指定尺寸
    easy_paste,          # 按 lt/lm/rb/center 等方向贴图
    easy_alpha_composite,
    draw_pic_with_ring,  # 头像加圆环（异步）
    CustomizeImage,      # 从自定义背景目录随机取图 + 提取主色
)
from gsuid_core.utils.image.convert import convert_img


async def render_role_card(uid: str, name: str, data: dict) -> bytes:
    # 1. 背景（自动主色 mask；bg_path 可指向插件的 CU_BG_PATH）
    img = await get_color_bg(based_w=950, based_h=1400)

    # 2. 文字
    draw = ImageDraw.Draw(img)
    draw.text((48, 60), f"角色: {name}", font=core_font(48), fill="white")
    draw.text((48, 120), f"UID: {uid}", font=core_font(32), fill=(200, 200, 200))

    # 3. 头像加圆环
    avatar = Image.open("xxx.png")
    ring_avatar = await draw_pic_with_ring(avatar, 200)
    easy_paste(img, ring_avatar, (380, 200), direction="center")

    # 4. 转字节并返回（convert_img 会按当前框架配置做缩放 / base64 等处理）
    return await convert_img(img)
```

**用 `core_font(size)` 拿字体**（自动选用框架预置的中英文兜底字体），不要 hardcode 字体路径。

### 9.3 htmlkit 范式（推荐）

适合一次性、不需要交互的 Markdown 报告 / 简单 HTML 卡片。**框架已封装**，直接 import 用：

```python
from gsuid_core.utils.html_render import (
    render_html_to_bytes,
    render_md_to_bytes,
    render_text_to_bytes,
)

async def render_report(stats: dict) -> bytes:
    md = f"""
# 今日早报

- 在线用户：**{stats['users']}**
- 今日查询：{stats['queries']}
- 错误数：{stats['errors']}
"""
    return await render_md_to_bytes(md=md, max_width=720)


async def render_dashboard(html: str) -> bytes:
    return await render_html_to_bytes(
        html,
        max_width=800,
        dpi=96,
        default_font_size=14,
        font_name="sans-serif",
        image_format="png",
        lang="zh",
    )
```

`pyproject.toml` 中显式声明依赖：
```toml
dependencies = ["pyrenderhtml>=0.0.5"]
```

### 9.4 playwright 范式（兜底）

只在 PIL / htmlkit 都不够用时才上 playwright（K 线 / 云图 / Plotly / ECharts 等）。
参考 `SayuStock/SayuStock/utils/image.py`：

```python
from pathlib import Path
from playwright.async_api import async_playwright
from gsuid_core.utils.image.convert import convert_img

async def render_image_by_pw(html_path: Path, w: int = 1920, h: int = 1080, scale: int = 2) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": w, "height": h},
            device_scale_factor=scale,
        )
        page = await context.new_page()
        await page.goto(html_path.absolute().as_uri())
        await page.wait_for_selector(".plot-container")     # 等关键元素就绪
        png_bytes = await page.screenshot(type="png")
        await browser.close()
        return await convert_img(png_bytes)
```

`pyproject.toml`：
```toml
dependencies = ["playwright>=1.49.0"]
```

**README 中必须显式说明**：
```
首次使用需运行：playwright install chromium
否则 launch() 会报"找不到 chromium"。
```

### 9.5 `convert_img`：所有发图前的最后一步

无论用哪种渲染方式，**最终发送给 `bot.send()` 的字节流都应过一次 `convert_img`**：

```python
from gsuid_core.utils.image.convert import convert_img

result = await convert_img(pil_image)   # PIL.Image / bytes / Path 都可以
await bot.send(result)
```

`convert_img` 会按框架配置做"是否转 base64""是否压缩""是否上传 RM"等统一处理，避免不同
平台适配器对裸 bytes 的兼容问题。

---

## 十、AI 集成：`to_ai` 与 `ai_return`

这是 GsCore 中将现有命令触发器零成本开放给 AI 调用的核心机制。

### 10.1 核心概念

**`to_ai` 参数**：在 `on_xxx` 装饰器上声明一段描述文字，启动时自动将触发器函数注册为 AI 工具（分类：`"by_trigger"`）。AI 按照这段描述理解"什么时候调用"以及"怎么构建参数"。

**`ai_return(text)`**：在触发器函数或其调用的数据处理函数中调用，向 AI 返回结构化文本摘要：
- **普通用户触发时**：完全静默，不影响任何逻辑
- **AI 调用时**：文本被收集，作为工具的返回值传回给 AI

**`MockBot`**：AI 调用触发器时，`bot` 对象被替换为 `MockBot`：
- `bot.send(bytes)` / `bot.send(Message(type="image"))` / `bot.send("base64://...")` → 通过 `RM.register()` 注册图片，返回资源 ID（如 `img_a1b2c3d4`），不传给 AI 也不发送给用户
- `bot.send(str)` / `bot.send(纯文字 Message)` → 文字被收集，作为工具返回值传回给 AI
- `bot.send_option(reply, buttons)` → reply 走 `send()` 拦截，buttons 忽略
- `bot.receive_resp(reply, ...)` → reply 走 `send()` 拦截，返回 `None`（AI 不支持交互式等待）
- AI 收到工具返回值（含资源 ID）后，决定是否调用 `send_message_by_ai(image_id=...)` 发送图片

**权限检查**：AI 调用触发器工具时，系统会自动检查 `plugins.pm` 和 `sv.pm` 权限，与用户直接触发一致。低权限用户通过 AI 调用高权限命令会收到 "❌ 权限不足" 错误。配置通过 webconsole 修改后实时生效。

### 10.2 `to_ai` 的 docstring 写法规范

**必须包含的内容**：

```
<一句话功能描述>
<用户在什么自然语言场景下会需要这个功能>

Args:
    text: <text 参数的完整格式，包括：>
          - <基础格式>
          - <可选前缀/后缀及其含义>
          - <多值分隔方式>
          - <至少两个具体例子>
          <如果是 on_fullmatch 且无参数：写"无需参数，留空即可">
```

**`to_ai` 写得好不好，决定 AI 能否正确调用触发器**。

### 10.3 基础用法示例

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import ai_return

sv = SV("游戏查询")

# ── 示例一：有参数的命令 ──────────────────────────────────────
@sv.on_command(
    ("查角色", "角色信息"),
    to_ai="""查询指定游戏角色的培养详情和属性数据。
    当用户询问某个角色的命座、圣遗物、天赋、伤害数据时调用。
    需要用户已绑定 UID。

    Args:
        text: 角色名称，支持昵称。
              例如 "雷电将军"、"雷神"、"胡桃"、"纳西妲"
    """,
)
async def get_char_info(bot: Bot, ev: Event) -> None:
    char_name = ev.text.strip()
    if not char_name:
        ai_return("错误：未提供角色名称，请在 text 中指定角色名")
        return await bot.send("请输入角色名，例如：查角色 雷电将军")
    uid = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if not uid:
        ai_return("错误：用户未绑定 UID，请先发送 '绑定 你的UID'")
        return await bot.send("请先绑定 UID")
    im = await render_char_image(uid.uid, char_name)
    await bot.send(im)


# ── 示例二：无参数的 fullmatch ─────────────────────────────────
@sv.on_fullmatch(
    "我的角色",
    to_ai="""查看用户当前绑定账号的全部角色列表。
    当用户说"帮我看看我有哪些角色"、"我的角色列表"时调用。
    无需参数，自动读取当前用户的绑定账号。

    Args:
        text: 无需参数，留空即可
    """,
)
async def my_chars(bot: Bot, ev: Event) -> None:
    uid = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if not uid:
        return await bot.send("请先绑定 UID")
    im = await render_char_list(uid.uid)
    await bot.send(im)


# ── 示例三：绑定操作（写操作，bot.send 的文字会被 MockBot 收集告知 AI）──
@sv.on_command(
    ("绑定", "bind"),
    to_ai="""绑定用户的游戏 UID 到账号。
    当用户说"帮我绑定UID"、"我的UID是xxx"时调用。

    Args:
        text: 游戏 UID，纯数字，例如 "123456789"
    """,
)
async def bind_uid_cmd(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    if not uid.isdigit():
        return await bot.send("UID 格式不正确，请输入纯数字")
    await GameBind.bind_uid(ev.user_id, ev.bot_id, uid)
    await bot.send(f"✅ 已成功绑定 UID: {uid}")
    # bot.send 的文字被 MockBot 收集，AI 会知道"绑定成功"
```

### 10.4 `ai_return` 在数据层的注入（推荐模式）

对于最终生成图片的触发器，在渲染层注入 `ai_return` 是最佳实践：

```python
# utils/renderer.py
from gsuid_core.ai_core.trigger_bridge import ai_return
from gsuid_core.logger import logger


async def render_char_image(uid: str, char_name: str) -> bytes:
    # 1. 获取数据
    char_data = await fetch_char_data(uid, char_name)

    # 2. 注入 AI 文本摘要（在数据拿到后、图片生成前）
    _ai_return_char(char_data, char_name)

    # 3. 生成图片
    fig = build_char_figure(char_data)
    return await render_image_by_pw(fig)


def _ai_return_char(char_data: dict, char_name: str) -> None:
    """提取角色关键数据作为 AI 可读文本摘要"""
    try:
        level = char_data.get("level", "N/A")
        constellation = char_data.get("constellation", 0)
        atk = char_data.get("fight_prop", {}).get("atk", "N/A")
        crit_rate = char_data.get("fight_prop", {}).get("crit_rate", 0.0)
        crit_dmg = char_data.get("fight_prop", {}).get("crit_dmg", 0.0)
        weapon = char_data.get("weapon", {}).get("name", "N/A")
        ai_return(
            f"【{char_name} 角色数据】\n"
            f"等级: {level}  命座: {constellation}命\n"
            f"攻击力: {atk:.0f}  暴击率: {crit_rate:.1%}  暴击伤害: {crit_dmg:.1%}\n"
            f"武器: {weapon}"
        )
    except Exception as e:
        # ai_return 的辅助函数允许 try/except，失败不影响图片生成
        logger.warning(f"[MyPlugin] ai_return 角色数据提取失败: {e}")
```

### 10.5 `ai_return` 应该包含什么内容

AI 拿到工具返回值后，会用这段文字来理解执行结果，并决定如何回复用户。

| 数据类型 | 应提取哪些字段 |
|---------|-------------|
| 游戏角色/装备 | 名称、等级、核心属性数值（至少3个）、关键装备 |
| 排行榜/列表 | 前 5 名 + 后 5 名 + 总计统计 |
| 行情/走势 | 名称、最新价、涨跌幅、开/高/低、关键指标 |
| K 线数据 | 名称、周期、最近 N 条记录（日期+核心数值） |
| 副本/任务 | 名称、进度（x/y）、完成状态、剩余次数 |
| 错误情况 | 错误原因，例如 `ai_return("错误：未找到角色 xxx")` |
| 写操作成功 | 不需要额外 `ai_return`，`bot.send` 的文字会被收集 |

### 10.6 哪些触发器不加 `to_ai`

| 情况 | 原因 |
|------|------|
| 管理员/超级用户专用命令 | 虽然系统会自动检查 `pm` 权限，但 AI 对大多数用户都会收到权限错误，浪费 token |
| 危险操作（清数据、重载配置） | AI 不应独立执行破坏性操作 |
| 需要多轮 Response 会话的命令 | `receive_resp` 在 AI 上下文中返回 `None`，交互流程会中断 |
| `on_file` 文件接收命令 | AI 无法构建文件输入 |
| 功能单一且 AI 无法获取有效信息 | 改造价值低 |

> **权限保障**：即使开发者错误地给高权限命令添加了 `to_ai`，系统也会在运行时检查 `plugins.pm` 和 `sv.pm`，低权限用户通过 AI 调用时会收到 "❌ 权限不足" 错误。

> **图片资源持久化**：AI 调用触发器时，图片通过 `RM.register()` 注册并返回资源 ID。资源 ID 在 RM 中持久存储，AI 可在后续轮次中通过 `send_message_by_ai(image_id=...)` 再次发送图片。

---

## 十一、AI 集成：`@ai_tools` 装饰器

当触发器的 `to_ai` 桥接不够用（例如你需要一个纯数据查询接口、不返回图片），用 `@ai_tools` 直接注册工具函数。

### 11.1 四种函数模式

```python
from pydantic_ai import RunContext
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.bot import Bot
from gsuid_core.models import Event

# 模式一：RunContext（推荐，可同时访问 bot 和 ev）
@ai_tools(category="default")
async def query_char_data(
    ctx: RunContext[ToolContext],
    char_name: str,
    uid: str,
) -> str:
    """
    查询游戏角色的基础属性数据（文本格式）。

    Args:
        char_name: 角色名称
        uid: 游戏 UID
    """
    bot = ctx.deps.bot
    ev = ctx.deps.ev
    data = await fetch_char_data(uid, char_name)
    return f"【{char_name}】攻击: {data['atk']}  暴击: {data['crit']}"


# 模式二：自动注入 Event/Bot（不暴露给 LLM，LLM 不需要填这些参数）
@ai_tools
async def get_my_uid(ev: Event) -> str:
    """获取当前用户绑定的游戏 UID。无需任何参数。"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if bind is None:
        return "您还没有绑定 UID"
    return f"您绑定的 UID：{bind.uid}"


# 模式三：无上下文（纯计算型工具）
@ai_tools(category="default")
async def calc_damage(
    atk: float,
    crit_rate: float,
    crit_dmg: float,
    multiplier: float = 1.0,
) -> str:
    """
    计算期望伤害。

    Args:
        atk: 攻击力
        crit_rate: 暴击率（0~1，如 0.7 表示 70%）
        crit_dmg: 暴击伤害（如 1.5 表示 150%）
        multiplier: 技能倍率，默认 1.0
    """
    expected = atk * multiplier * (1 + crit_rate * crit_dmg)
    return f"期望伤害：{expected:.0f}"
```

### 11.2 category 分类规则

| 分类 | 谁能调用 | 使用场景 |
|------|---------|---------|
| `"common"` | 主 Agent 直接调用 | 高频核心功能，主 Agent 直接可见 |
| `"default"` | 子 Agent（通过 `create_subagent`） | 复杂计算、文件操作等子任务 |
| `"<自定义>"` | 根据配置 | 插件专属分类 |

**主 Agent 工具越多 Token 消耗越大**，常用功能才放 `"common"`，其余放 `"default"` 或自定义分类（一般都放default）。

### 11.3 check_func 权限校验

```python
from gsuid_core.models import Event

async def check_bound(ev: Event) -> tuple[bool, str]:
    """校验用户是否已绑定账号"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if bind is not None:
        return True, ""
    return False, "⚠️ 请先绑定账号：发送 '绑定 您的UID'"

def check_admin(ev: Event) -> tuple[bool, str]:
    """同步校验函数也支持"""
    ADMIN_LIST = ["123456789"]
    if ev.user_id in ADMIN_LIST:
        return True, ""
    return False, "⚠️ 此工具仅管理员可用"

# 使用 check_func：校验失败时不执行函数，直接返回错误消息给 AI
@ai_tools(category="common", check_func=check_bound)
async def query_my_data(ev: Event) -> str:
    """查询我的游戏数据（需要先绑定）"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    return f"UID: {bind.uid}"  # type: ignore[union-attr]  # check_func 已保证 bind 非 None
```

### 11.4 工具 docstring 规范

AI 工具的 docstring 是 AI 判断"是否调用"以及"如何传参"的依据，**必须清晰**：

```python
@ai_tools(category="common")
async def search_game_data(
    ctx: RunContext[ToolContext],
    query: str,
    category: str = "all",
    limit: int = 5,
) -> str:
    """
    搜索游戏内的数据（角色、装备、副本等）。
    当用户询问游戏相关信息但不知道具体名称时调用。

    Args:
        query: 搜索关键词，例如 "雷元素长枪角色" 或 "高暴击圣遗物套装"
        category: 搜索类别，可选 "character"/"weapon"/"artifact"/"all"，默认 "all"
        limit: 返回结果数量，默认 5，最大 20

    Returns:
        匹配结果的文本列表
    """
    ...
```

---

## 十二、AI 集成：知识库与别名注册

### 12.1 注册知识库（`ai_entity`）

让 AI 在 RAG 检索时能找到插件相关的静态知识（命令说明、游戏数据等）：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint

# 在模块加载时调用，自动在启动时同步到向量数据库
ai_entity(KnowledgePoint(
    id="myplugin_commands",           # 全局唯一 ID，建议 {plugin}_{类型}_{编号}
    plugin="MyPlugin",
    title="MyPlugin 命令使用指南",
    content="""
# MyPlugin 使用指南

## 命令列表
- `查角色 <角色名>` — 查询角色培养详情，需要先绑定 UID
- `绑定 <UID>` — 绑定游戏账号
- `我的角色` — 查看全部角色列表
- `帮助` — 显示此帮助

## 注意事项
1. 所有查询功能需要先绑定账号
2. 每日查询上限为 100 次
3. 支持的游戏区域：cn（国服）、os（国际服）
""",
    tags=["MyPlugin", "帮助", "命令", "使用说明"],
))

ai_entity(KnowledgePoint(
    id="myplugin_genshin_shogun",
    plugin="MyPlugin",
    title="雷电将军 - 角色培养建议",
    content="""
# 雷电将军培养建议

## 推荐圣遗物
- 绝缘之旗印（4件套）：充能转化攻击，爆发效果极强

## 推荐武器
- 薙草之稻光（5星长枪）：充能提升+技能倍率加成

## 属性优先级
充能 160%+ → 暴击率 70%+ → 暴击伤害 → 攻击力
""",
    tags=["雷电将军", "雷神", "角色", "培养", "原神", "MyPlugin"],
))
```

**注意**：`id` 字段变化会触发重新索引，`content` 变化会通过 `_hash` 检测自动增量更新。

### 12.2 注册别名（`ai_alias`）

让 AI 在解析用户意图时进行专有名词归一化：

```python
from gsuid_core.ai_core.register import ai_alias

# 在模块级别调用（导入时即执行）
ai_alias("雷电将军", ["雷神", "将军", "影", "Raiden", "shogun"])
ai_alias("纳西妲", ["草神", "小草神", "Lesser Lord Kusanali"])
ai_alias("胡桃", ["小胡桃", "HuTao", "胡桃儿", "往生堂堂主"])

# 批量注册
GAME_ALIASES: dict[str, list[str]] = {
    "雷电将军": ["雷神", "将军"],
    "钟离": ["岩神", "摩拉克斯"],
    "万叶": ["楓原万叶", "枫原万叶"],
}
for name, aliases in GAME_ALIASES.items():
    ai_alias(name, aliases)
```

---

## 十三、AI 集成：`create_agent`

用于在触发器内部创建一个**临时的专用 AI Agent**，执行特定子任务（如文本分析、翻译、摘要）：

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 模块级别创建（复用 Agent 实例）
summarizer = create_agent(
    system_prompt="""你是一个文本摘要专家。
将用户提供的文本压缩为不超过 100 字的摘要，保留核心信息，输出中文。
直接输出摘要，不加任何说明。""",
    max_tokens=500,
)

translator = create_agent(
    system_prompt="你是一个翻译助手，只负责将输入翻译为中文，不做解释。",
    max_tokens=1000,
)

# 在触发器中调用
@sv.on_command("摘要")
async def summarize_cmd(bot: Bot, ev: Event) -> None:
    text = ev.text.strip()
    if not text:
        return await bot.send("请在命令后提供要摘要的文本")
    result = await summarizer.run(user_message=text)
    await bot.send(f"摘要：\n{result}")

# 带结构化输出
from pydantic import BaseModel

class CharAnalysis(BaseModel):
    name: str
    element: str
    recommended: bool
    reason: str

char_analyzer = create_agent(
    system_prompt="你是原神角色分析专家，根据用户描述给出角色评价。"
)

@sv.on_command("分析角色")
async def analyze_char(bot: Bot, ev: Event) -> None:
    char_name = ev.text.strip()
    result: CharAnalysis = await char_analyzer.run(
        user_message=f"分析角色：{char_name}",
        bot=bot,
        ev=ev,
        output_type=CharAnalysis,  # 强制结构化输出
    )
    await bot.send(
        f"角色：{result.name}\n"
        f"元素：{result.element}\n"
        f"推荐：{'✅' if result.recommended else '❌'}\n"
        f"理由：{result.reason}"
    )
```

---

## 十四、完整插件示例

以下是一个包含全部核心功能的完整游戏查询插件示例，遵循 GsCore 插件命名规范。

### 14.1 命名规范

参照 GenshinUID、SayuStock 等成熟插件的目录结构：

| 规则 | 说明 | 示例 |
|------|------|------|
| 插件目录名 | `_PluginName/`（下划线前缀表示 buildin 插件）或 `PluginName/`（用户插件） | `_GenshinUID/`、`SayuStock/` |
| 内部包名 | 与插件目录名同名（不含下划线前缀） | `GenshinUID/`、`SayuStock/` |
| 入口文件 | `__init__.py` + `__nest__.py`（嵌套加载模式） | `GenshinUID/__init__.py` |
| 子模块目录 | `{prefix}_{feature}/`，prefix 为插件名小写缩写 | `genshinuid_roleinfo/`、`stock_info/` |
| 共享工具 | `utils/`、`tools/`（不加前缀） | `utils/database/models.py` |
| 配置模块 | `{prefix}_config/` | `genshinuid_config/`、`stock_config/` |
| 资源目录 | 子模块内的 `texture2d/` 或 `texture2D/` | `genshinuid_enka/texture2D/` |

### 14.2 目录结构

省略插件所在文件夹（`gsuid_core/plugins/`）下的其他目录，只保留 `MyGameUID/` 目录。

```
/MyGameUID/                            # 用户插件目录
├── __init__.py                        # 插件入口（可留空）
├── __nest__.py                        # 空文件，标记启用嵌套加载
├── pyproject.toml                     # 插件依赖声明
├── README.md
├── LICENSE
├── ICON.png                           # 插件图标（帮助 / webconsole）
└── MyGameUID/                         # 内部包（与插件目录同名）
    ├── __init__.py                    # 定义 Plugins(...) + 可选 import 子模块
    ├── __full__.py                    # 空文件，向框架标记嵌套加载
    ├── version.py                     # MyGameUID_version = "1.0.0"
    ├── mygameuid_bind/                # 绑定功能子模块
    │   └── __init__.py
    ├── mygameuid_roleinfo/            # 角色查询子模块
    │   ├── __init__.py
    │   ├── draw_roleinfo.py           # 图片渲染（PIL）
    │   └── texture2d/                 # 子模块专属静态资源
    │       └── bg.png
    ├── mygameuid_help/                # 帮助子模块（推荐）
    │   ├── __init__.py
    │   ├── get_help.py
    │   ├── help.json
    │   ├── icon_path/
    │   └── texture2d/
    ├── mygameuid_config/              # 配置子模块
    │   ├── __init__.py                # 开启 / 关闭 / 设置阈值 命令
    │   ├── config_default.py
    │   └── mygame_config.py
    └── utils/                         # 共享工具（不加前缀）
        ├── database/
        │   ├── __init__.py
        │   └── models.py              # 表 + @site.register_admin
        ├── resource/
        │   └── RESOURCE_PATH.py       # 各类路径常量
        ├── api.py                     # 第三方 API 请求封装
        ├── image.py                   # get_footer / 颜色辅助
        └── hint.py                    # 公共提示文案
```

### 14.3 `MyGameUID/utils/database/models.py`

```python
from typing import Optional
from sqlmodel import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import BaseModel, with_session


class MyGameBind(BaseModel, table=True):
    """游戏账号绑定表"""
    __table_args__ = {"extend_existing": True}

    uid: str = Field(title="游戏 UID")
    server: str = Field(default="cn", title="服务器")

    @classmethod
    @with_session
    async def get_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str
    ) -> Optional["MyGameBind"]:
        stmt = select(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_session
    async def save_bind(
        cls, session: AsyncSession, user_id: str, bot_id: str, uid: str
    ) -> None:
        existing = await cls.get_bind(user_id, bot_id)
        if existing:
            existing.uid = uid
            session.add(existing)
        else:
            session.add(cls(user_id=user_id, bot_id=bot_id, uid=uid))


# 把这张表暴露到 Web 控制台，供管理员可视化管理
@site.register_admin
class MyGameBindAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="MyGame 绑定管理",
        icon="fa fa-link",
    )  # type: ignore
    model = MyGameBind
```

### 14.4 `MyGameUID/mygameuid_config/`

插件配置分为两个文件：`config_default.py` 定义默认配置项，`mygame_config.py` 创建 `StringConfig` 实例。

#### `config_default.py` — 配置项定义

```python
from typing import Dict
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsStrConfig,
    GsIntConfig,
    GsBoolConfig,
)

CONFIG_DEFAULT: Dict[str, GSC] = {
    "api_key": GsStrConfig(
        title="API Key",
        desc="游戏数据 API Key",
        data="",
    ),
    "cache_ttl": GsIntConfig(
        title="缓存时长（分钟）",
        desc="数据缓存时长",
        data=30,
    ),
    "enable_cache": GsBoolConfig(
        title="启用缓存",
        desc="是否启用数据缓存",
        data=True,
    ),
}
```

#### `mygame_config.py` — 创建 StringConfig 实例

```python
from gsuid_core.utils.plugins_config.gs_config import StringConfig
from .config_default import CONFIG_DEFAULT
from ..utils.resource.RESOURCE_PATH import CONFIG_PATH

MYGAME_CONFIG = StringConfig("MyGameUID", CONFIG_PATH, CONFIG_DEFAULT)
```

> **配置类型一览**（`gsuid_core/utils/plugins_config/models.py`）：
>
> | 类型 | 说明 | `data` 类型 |
> |------|------|------------|
> | `GsStrConfig` | 字符串配置 | `str` |
> | `GsBoolConfig` | 布尔配置 | `bool` |
> | `GsIntConfig` | 整数配置 | `int` |
> | `GsListStrConfig` | 字符串列表 | `List[str]` |
> | `GsListConfig` | 整数列表 | `List[int]` |
> | `GsDictConfig` | 字典配置 | `Dict[str, List]` |
> | `GsImageConfig` | 图片配置 | `str` |
> | `GsTimeRConfig` | 时间范围 | `Tuple[int, int]` |
>
> 所有配置类型继承自 `GsConfig(msgspec.Struct)`，必须包含 `title`、`desc`、`data` 字段。

### 14.5 `MyGameUID/mygameuid_roleinfo/draw_roleinfo.py`

```python
from gsuid_core.logger import logger
from gsuid_core.ai_core.trigger_bridge import ai_return
from MyGameUID.utils.api import fetch_char_data


async def render_char_image(uid: str, char_name: str) -> bytes | str:
    """渲染角色图片，同时注入 AI 可读数据"""
    data = await fetch_char_data(uid, char_name)
    if isinstance(data, str):
        ai_return(f"错误：{data}")
        return data
    _ai_return_char(data, char_name)
    return await _build_image(data)


def _ai_return_char(data: dict, char_name: str) -> None:
    level = data.get("level", "N/A")
    const = data.get("constellation", 0)
    props = data.get("properties", {})
    atk = props.get("atk", 0)
    crit_rate = props.get("crit_rate", 0.0)
    crit_dmg = props.get("crit_dmg", 0.0)
    weapon = data.get("weapon", {}).get("name", "N/A")
    ai_return(
        f"【{char_name} 角色详情】\n"
        f"等级: {level}  命座: {const}命\n"
        f"攻击力: {atk:.0f}  暴击率: {crit_rate:.1%}  暴击伤害: {crit_dmg:.1%}\n"
        f"武器: {weapon}"
    )


async def _build_image(data: dict) -> bytes:
    # 实际图片生成逻辑（略）
    ...
```

### 14.6 `MyGameUID/mygameuid_bind/__init__.py`

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import ai_return
from MyGameUID.utils.database.models import MyGameBind

sv = SV("MyGameUID")


@sv.on_command(
    ("绑定", "bind"),
    to_ai="""绑定用户的游戏 UID。
    当用户说"帮我绑定UID"、"我的UID是xxx"时调用。

    Args:
        text: 游戏 UID，纯数字，例如 "123456789"
    """,
)
async def bind_uid(bot: Bot, ev: Event) -> None:
    uid = ev.text.strip()
    if not uid.isdigit():
        return await bot.send("UID 格式不正确，请输入纯数字")
    await MyGameBind.save_bind(ev.user_id, ev.bot_id, uid)
    await bot.send(f"已成功绑定 UID: {uid}")


@sv.on_fullmatch(
    "我的绑定",
    to_ai="""查看当前用户绑定的游戏 UID。
    当用户询问"我绑定了什么"、"我的UID是多少"时调用。
    无需参数。

    Args:
        text: 无需参数，留空即可
    """,
)
async def show_bind(bot: Bot, ev: Event) -> None:
    bind = await MyGameBind.get_bind(ev.user_id, ev.bot_id)
    if not bind:
        return await bot.send("您还没有绑定 UID")
    await bot.send(f"您绑定的 UID：{bind.uid}")
```

### 14.7 `MyGameUID/mygameuid_roleinfo/__init__.py`

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.trigger_bridge import ai_return
from MyGameUID.utils.database.models import MyGameBind
from MyGameUID.mygameuid_roleinfo.draw_roleinfo import render_char_image

sv = SV("MyGameUID")


@sv.on_command(
    ("查角色", "角色信息"),
    to_ai="""查询游戏角色的培养详情和属性数据。
    当用户询问某个角色的命座、圣遗物、天赋、属性面板时调用。
    需要用户已绑定 UID。

    Args:
        text: 角色名称，支持昵称。
              例如 "雷电将军"、"雷神"（等同于雷电将军）、"胡桃"
    """,
)
async def get_char_info(bot: Bot, ev: Event) -> None:
    char_name = ev.text.strip()
    if not char_name:
        ai_return("错误：未提供角色名，请在 text 中指定角色名称")
        return await bot.send("请输入角色名，例如：查角色 雷电将军")
    bind = await MyGameBind.get_bind(ev.user_id, ev.bot_id)
    if not bind:
        ai_return("错误：用户未绑定 UID，请先发送 '绑定 你的UID'")
        return await bot.send("请先绑定 UID：发送 '绑定 你的UID'")
    im = await render_char_image(bind.uid, char_name)
    await bot.send(im)
```

### 14.8 `MyGameUID/__init__.py`（包入口）

包入口文件的核心职责：
1. **定义 `Plugins` 类** — 声明插件的全局配置（前缀、权限、别名等）
2. **导入子模块** — 触发各子模块中 `@sv.on_xxx` 装饰器的注册

```python
from gsuid_core.sv import Plugins

# ── Plugins 类：声明插件全局配置 ──────────────────────────────────
# Plugins 是单例模式，同名插件只创建一次。
# 它定义了插件内所有 SV 实例共享的前缀、权限等配置。
#
# 关键参数：
#   name:             插件名称，必须与目录名一致
#   force_prefix:     强制前缀列表，用户必须以此开头才能触发命令
#   allow_empty_prefix: 是否允许无前缀触发（默认根据 prefix/force_prefix 自动推断）
#   alias:            插件别名列表
#   pm:               权限等级（0-6，数字越小权限越高）
#   prefix:           可选前缀列表（与 force_prefix 的区别：force_prefix 强制，prefix 可选）
#   disable_force_prefix: 是否禁用强制前缀
#
# 实际示例（参照 GenshinUID）：
#   Plugins(name="GenshinUID", force_prefix=["gs"], allow_empty_prefix=False, alias=["gsuid"])
#
# 实际示例（参照 SayuStock）：
#   Plugins(name="SayuStock", force_prefix=["a", "股票"], allow_empty_prefix=True)

Plugins(
    name="MyGameUID",
    force_prefix=["mygame", "游戏"],
    allow_empty_prefix=False,
    alias=["mygame"],
)

# ── 导入子模块，触发 @sv.on_xxx 装饰器注册 ───────────────────────
from MyGameUID import mygameuid_bind  # noqa: F401
from MyGameUID import mygameuid_roleinfo  # noqa: F401
```

> **`Plugins` vs `SV` 的关系**：
> - `Plugins` 是**插件级**配置，定义整个插件的前缀、权限等共享设置
> - `SV` 是**服务模块级**配置，定义单个功能模块的触发器和权限
> - 一个 `Plugins` 下可以有多个 `SV` 实例
> - `SV` 创建时会自动查找同名 `Plugins` 实例，继承其前缀配置

### 14.9 `MyGameUID/__nest__.py`（嵌套加载入口）

空文件, 无需任何内容

```python
```

### 14.10 `__init__.py`（插件根目录入口）

```python
# 插件根目录的 __init__.py
# 对于 __nest__.py 模式，此文件可留空或仅做版本声明
```

---

## 十五、常用工具模块速查

这一节汇总插件开发最常用的"小工具"——多读两遍能省下大量重新发明轮子的功夫。

### 15.1 资源存储：`get_res_path`

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

### 15.2 推送主人消息：`send_msg_to_master`

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

### 15.3 错误码与提示：`error_reply`

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

### 15.4 限流：`CooldownTracker`

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

### 15.5 函数级图片缓存：`@gs_cache`

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

### 15.6 字体：`core_font`

```python
from gsuid_core.utils.fonts.fonts import core_font

font = core_font(48)        # 拿一个 size=48 的中英文兜底字体
draw.text((10, 10), "雷电将军", font=font, fill="white")
```

**不要 hardcode 字体路径**，`core_font` 内部用框架自带的 `MiSans-Bold.ttf`。

### 15.7 同步代码异步桥接：`to_thread`

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

### 15.8 第三方 API 缓存：`@cache_data`

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

### 15.9 群组 / 私聊批量播报：`send_board_cast_msg`

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

> 一般场景下 **优先用 `gs_subscribe`**，`send_board_cast_msg` 仅在你已经有外部数据源
> （比如从某 API 拉到的目标列表）时使用。

### 15.10 速查表：常用 import

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
    GSC, GsStrConfig, GsBoolConfig, GsIntConfig, GsListStrConfig,
    GsListConfig, GsDictConfig, GsImageConfig, GsTimeRConfig,
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

---

## 十六、代码规范红线

GsCore 对代码质量有严格要求，以下规则**绝对禁止**：

### 16.1 禁止事项

```python
# ❌ 禁止：try-except 兜底（掩盖类型和逻辑问题）
try:
    result = data.get("key")
except (AttributeError, KeyError):
    result = None

# ❌ 禁止：cast() 类型强制转换
from typing import cast
result = cast(str, some_value)

# ❌ 禁止：type: ignore 抑制自身代码的类型错误
data = some_function()  # type: ignore

# ❌ 禁止：getattr/dict.get 兜底
name = getattr(user, "name", None)
value = data.get("key", None)

# ❌ 禁止：同步阻塞函数（整个项目是异步的）
def fetch_data(url: str) -> dict:
    import requests
    return requests.get(url).json()
```

### 16.2 正确做法

```python
# ✅ 正确：Union + isinstance 守卫
from typing import Union

def process(result: str | int | None) -> str:
    if isinstance(result, int):
        return str(result)
    if result is None:
        return ""
    return result

# ✅ 正确：所有函数必须有类型注解
async def get_user(user_id: str, bot_id: str) -> GameBind | None:
    return await GameBind.get_bind(user_id, bot_id)

# ✅ 正确：TypedDict 代替裸字典
from typing import TypedDict

class CharData(TypedDict):
    level: int
    constellation: int
    weapon: str

# ✅ 正确：全部使用异步 I/O
async def fetch_data(url: str) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        return resp.json()
```

### 16.3 `ai_return` 辅助函数的特殊说明

`_ai_return_xxx()` 系列辅助函数是**唯一允许使用 `try/except`** 的地方，因为：
1. 它们是观测性代码，不属于业务逻辑
2. 提取失败绝对不能影响图片生成和发送
3. 失败时只记录 `logger.warning`，不 raise

```python
# ✅ 唯一允许 try/except 的地方
def _ai_return_xxx(data: dict) -> None:
    try:
        result = f"..."
        ai_return(result)
    except Exception as e:
        logger.warning(f"[插件名] ai_return 数据提取失败: {e}")
```
