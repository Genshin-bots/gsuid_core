# 六、定时任务与订阅

## 6.1 使用 APScheduler

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

## 6.2 主动推送强制规范

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

## 6.3 订阅 API 全集

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

## 6.4 定时任务的硬约束

- **定时任务函数没有 `bot` / `ev` 注入**——所有 Bot 句柄要么从 `gs_subscribe` 拿、要么
  从 `gss.active_bot` 主动取（但后者一般只用于纯系统任务如缓存清理，不发用户消息）。
- 定时任务里 `raise` 的异常会被 APScheduler 吞掉，**必要时自己 `try/except` + `logger.exception`**。
  这一处异常处理是 §16 红线之外的特例。
- 短周期任务（< 5 分钟）频繁查库时记得加 `@gs_cache(expire_time=...)`（详见 §15）。

你还可以通过 `extra_message` 参数在订阅时保存额外数据，并在发送时通过 `sub.extra_message` 读取。
