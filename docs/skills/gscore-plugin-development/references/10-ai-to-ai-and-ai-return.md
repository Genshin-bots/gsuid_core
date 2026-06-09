# 十、AI 集成：`to_ai` 与 `ai_return`

> **⚠️ `to_ai` 是优先方案**：`to_ai` 和 `@ai_tools` 功能等价（都是把函数注册为 AI 工具），**二者冲突，不可对同一函数同时使用**。
> - **优先使用 `to_ai`**：只要该函数同时需要被用户直接触发（`@sv.on_xxx`），就用 `to_ai` 参数——一份代码同时服务用户命令和 AI 调用。
> - **仅当函数只允许 AI 调用、不暴露为用户命令时**，才用 `@ai_tools`（如纯数据查询接口、不返回图片的计算工具）。
> - 对同一函数同时写 `@sv.on_command(..., to_ai="...")` 和 `@ai_tools` 会导致重复注册或行为异常。

这是 GsCore 中将现有命令触发器零成本开放给 AI 调用的核心机制。

## 10.1 核心概念

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

## 10.2 `to_ai` 的 docstring 写法规范

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

## 10.3 基础用法示例

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

## 10.4 `ai_return` 在数据层的注入（推荐模式）

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

## 10.5 `ai_return` 应该包含什么内容

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

## 10.6 哪些触发器不加 `to_ai`

| 情况 | 原因 |
|------|------|
| 管理员/超级用户专用命令 | 虽然系统会自动检查 `pm` 权限，但 AI 对大多数用户都会收到权限错误，浪费 token |
| 危险操作（清数据、重载配置） | AI 不应独立执行破坏性操作 |
| 需要多轮 Response 会话的命令 | `receive_resp` 在 AI 上下文中返回 `None`，交互流程会中断 |
| `on_file` 文件接收命令 | AI 无法构建文件输入 |
| 功能单一且 AI 无法获取有效信息 | 改造价值低 |

> **权限保障**：即使开发者错误地给高权限命令添加了 `to_ai`，系统也会在运行时检查 `plugins.pm` 和 `sv.pm`，低权限用户通过 AI 调用时会收到 "❌ 权限不足" 错误。

> **图片资源持久化**：AI 调用触发器时，图片通过 `RM.register()` 注册并返回资源 ID。资源 ID 在 RM 中持久存储，AI 可在后续轮次中通过 `send_message_by_ai(image_id=...)` 再次发送图片。
