# 四、触发器 → AI 工具桥接（`to_ai`）

## 4.1 概述

`to_ai` 参数允许插件开发者将现有的触发器函数自动注册为 AI 工具，无需编写重复的 `@ai_tools` 函数。AI 调用时使用 `MockBot` 拦截 `bot.send()`，将图片/消息内容收集而非真正发送，由 AI 决定是否真正发给用户。

> **⚠️ `to_ai` 与 `@ai_tools` 二选一**：同一函数不可同时使用。命令也允许用户直接触发 → `to_ai`（一份代码服务用户命令 + AI 调用）；纯 AI 内部工具 → `@ai_tools`。详见 [`gscore-plugin-development` SKILL §十、§十一](../gscore-plugin-development/SKILL.md)。

**核心模块**：[`gsuid_core/ai_core/trigger_bridge.py`](../../../gsuid_core/ai_core/trigger_bridge.py:1)

## 4.2 `to_ai` 参数

在所有 `on_xxx` 装饰器上新增 `to_ai: str = ""` 参数：

| 参数值 | 行为 |
|--------|------|
| `""`（默认） | 不注册为 AI 工具，行为完全不变 |
| 非空字符串 | 将该字符串作为 AI 工具的 docstring，自动注册到 `_TOOL_REGISTRY["by_trigger"]` |

```python
from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

sv = SV("股票插件")

@sv.on_command(
    "个股",
    to_ai="""
    查询指定股票或ETF的K线图或分时图。
    当用户询问某只股票/ETF今天走势、分时图、日K、周K、月K时调用。

    Args:
        text: 查询内容，格式为 "[周期前缀] 股票名称或代码"
              - 无前缀：默认显示分时图，例如 "证券ETF"
              - "日k": 日K线，例如 "日k 证券ETF"
              - 多个标的以空格分隔，例如 "证券ETF 白酒ETF"
    """,
)
async def send_stock_img(bot: Bot, ev: Event):
    content = ev.text.strip().lower()
    if not content:
        return await bot.send("请后跟股票代码使用")
    # ... 原有逻辑完全不变 ...
    await bot.send(im)
```

## 4.3 `ai_return()` — 向 AI 返回中间文本

在触发器函数内调用，向 AI 返回纯文本中间结果（如错误提示、进度信息）。

```python
from gsuid_core.ai_core.trigger_bridge import ai_return

@sv.on_command("个股", to_ai="查询股票走势...")
async def send_stock_img(bot: Bot, ev: Event):
    content = ev.text.strip()
    if not content:
        ai_return("错误：未提供股票代码")
        return await bot.send("请后跟股票代码使用")
    # ...
```

| 调用场景 | 行为 |
|----------|------|
| 真实用户触发 | 静默忽略，不影响触发器正常执行 |
| AI 工具调用 | 文本被收集，最终作为工具返回值返回给 AI |

## 4.4 `MockBot` — AI 调用时的消息拦截

AI 调用触发器时，包装函数使用 `MockBot` 代理真实 Bot：

| 方法 | 行为 |
|------|------|
| `send(str)` | 纯文字存入 `bot_messages`；`base64://`/`http://` 等图片字符串通过 `RM.register()` 注册，返回资源 ID |
| `send(bytes)` | 通过 `RM.register()` 注册，返回资源 ID |
| `send(Message(type="image"))` | 提取图片数据通过 `RM.register()` 注册，返回资源 ID |
| `reply()` | 同 `send()` |
| `send_option(reply, buttons)` | reply 走 `send()` 拦截，buttons 忽略 |
| `receive_resp(reply, ...)` | reply 走 `send()` 拦截，返回 `None`（AI 不支持交互式等待） |
| 其他属性 | 代理到真实 Bot（如 `bot_self_id`） |

### 4.4.1 权限检查

AI 调用触发器工具时，会执行与用户直接触发相同的权限检查：

- `plugins.enabled` — 插件是否启用
- `sv.enabled` — SV 是否启用
- `user_pm <= plugins.pm` — 插件级权限
- `user_pm <= sv.pm` — SV 级权限

权限不足时返回错误文本给 AI，AI 据此向用户解释。配置通过 webconsole 修改后实时生效（运行时读取，非注册时快照）。

## 4.5 `send_message_by_ai` 工具发送图片

位于 `gsuid_core.ai_core.buildin_tools.message_sender`，`self` 分类。
AI 通过资源 ID 将图片发送给用户：

```python
# AI 工具返回值示例：
# "[已生成 1 张图片，资源ID: img_a1b2c3d4。请调用 send_message_by_ai 工具传入 image_id 将图片发送给用户]"
```

AI 可以：
- 调用 `send_message_by_ai(image_id="img_a1b2c3d4")` → 通过 `RM.get()` 取回图片并发送
- 不调用 → 图片保留在 RM 中（用户只收到 AI 的文字回复）

资源 ID 通过 `RM.register()` 生成，格式为 `img_xxxxxxxx`，在 RM 中持久存储，支持跨轮次使用。

## 4.6 交互流程对比

| 场景 | bot 对象 | bot.send 行为 |
|------|---------|--------------|
| 用户直接触发 | 真实 Bot | 立即发送给用户 |
| AI 调用，AI 决定发 | MockBot | `RM.register()` → 返回资源 ID → AI 调用 `send_message_by_ai(image_id=...)` → 发出 |
| AI 调用，AI 决定不发 | MockBot | `RM.register()` → 返回资源 ID → AI 不调用 → 图片保留在 RM 中 |

## 4.7 注册时序

```
插件加载阶段 (cached_import)
    │
    ├── 执行 @sv.on_command("个股", to_ai="...")
    │       │
    │       └── _on() 检测 to_ai 非空
    │               │
    │               └── _register_trigger_as_ai_tool(func, keyword, to_ai_doc, sv, trigger_type)
    │                       │
    │                       └── 写入 _TOOL_REGISTRY["by_trigger"]["send_stock_img"]
    │
    └── 插件加载完成
```

## 4.8 端到端改造指南

`to_ai` 的批量改造工作流（背景、Step 0~4、完整股票/游戏示例、质量检查清单、Q&A）见 [`gscore-plugin-development` SKILL §十八、触发器 → AI 工具改造指南](../gscore-plugin-development/references/18-ai-trigger-migration.md)。
