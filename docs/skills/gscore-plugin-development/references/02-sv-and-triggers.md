# 二、SV 与触发器

## 2.1 创建 SV 实例

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

## 2.2 触发器语义速查

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

## 2.3 触发器装饰器的通用参数

所有 `on_xxx` 装饰器都支持以下通用参数：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `block` | `bool` | `False` | 匹配后阻止事件继续传递给其他低优先级触发器（防"多个触发器都响应同一条消息"） |
| `to_me` | `bool` | `False` | 是否必须 @ 机器人才触发（`ev.is_tome=True`） |
| `prefix` | `bool` | `True` | 是否给关键词自动拼上插件的 `force_prefix`/`prefix`（**几乎总是 True**） |
| `to_ai` | `str` | `""` | 非空时自动把本触发器注册为 AI 工具（详见第十章）。⚠️ 与 `@ai_tools` 冲突，不可同时使用 |

**`block=True` 使用建议**：
- 高优先级"短路"触发器（如插件级帮助、错误提示）应 `block=True`，避免兜底触发器再次响应。
- 一般业务命令保持 `block=False`，由 `priority` 控制顺序。

## 2.4 触发器注册示例

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

## 2.5 处理函数签名规范

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
