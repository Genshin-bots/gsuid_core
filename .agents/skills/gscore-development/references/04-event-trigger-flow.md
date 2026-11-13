# 四、事件处理与触发器流转

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[三、插件加载与配置系统](./03-plugin-loading-and-config.md) · **下一章**：[五、Bot 三类](./05-bot-classes.md)

本章讲一条消息进 `handle_event` 后到底经过哪些判断、命令与 AI 怎么分流、AI 在什么条件下
被触发。改消息处理顺序、加全局拦截、调触发条件前必读。

## 4.1 `handler.py::handle_event` 13 步

入口：`async def handle_event(ws: _Bot, msg: MessageReceive, is_http=False)`

```
1.  IS_HANDDLE 全局开关：not IS_HANDDLE → return
2.  黑名单 / 屏蔽列表 / 相同用户事件冷却（black_list / shield_list / same_user_cd）
3.  msg_process() 解析消息 → Event 对象
4.  用户消息记录到历史（history_manager.add_message）
5.  主人识别：user_pm == 0 → 后台确保订阅"主人用户"（不挡命令）
6.  用户/群组记账（后台 insert_user / insert_group，不挡命令）
7.  生成 session_id（Event.session_id 属性，见 §06）
8.  重复消息检查（instances 单实例 / mutiply_instances 多实例）
9.  相同消息冷却（cooldown_tracker.is_on_cooldown）
10. 命令前缀处理（移除 command_start 前缀）
11. 触发器匹配 _check_command()（遍历 SL.lst 所有触发器）
12. 命令执行：有匹配则执行 trigger.func()
13. AI 处理：无命令匹配则进 AI 流程
```

> **顺序即语义**：黑名单/冷却在最前（省成本）；历史记录在触发判断**之前**（AI 关了也照样
> 记历史、攒记忆）；触发器匹配在 AI 之前（**命令优先于 AI**）。加全局拦截要想清楚插在哪一步。
> 第 5、6 步只把写库放进后台：命令入队时，主人订阅和 `CoreUser` 可能还没提交。

> **入历史门控（2026-07-12）**：第 4 步的条件是 `_has_text or event.at_list`——**纯 @ 消息
> （at 段无文字）也必须入历史**，否则 @ 目标从历史里凭空消失，AI/Heartbeat 会把紧随其后的
> "醒了吗"误认为在叫自己（生产实测误判）。已知缺口：纯图片消息（无文字无 @）仍不入历史；
> 该门控与记忆 observe 门控（`_has_text or _img_urls`）是两套谓词，改任一侧前先看
> `docs/AI_CORE_CHANGE_REVIEW_20260712.md` §5.4 的统一方案。
> 另：`msg_process` 里 @Bot 判定已做 str 归一化（`str(_msg.data) == str(event.bot_self_id)`）——
> 适配器可能给 int 型 at 目标，不归一会让 @Bot 落进 at_list，下游「@的不是你」标注
> （history_format / utils）会**反向压制**真正的 @Bot 消息。

## 4.2 触发器匹配（命令路径）

```
for sv in SL.lst:
    for _type in SL.lst[sv].TL:                 # 触发器类型（command/prefix/fullmatch/keyword/regex/...）
        for tr in SL.lst[sv].TL[_type]:
            if _check_command(trigger, priority, event):
                valid_event[trigger] = priority
```

`_check_command` 内部会校验：SV/插件 `enabled`、用户权限 `user_pm <= sv.pm`、触发器文本匹配。
有匹配则按 `dispatch_sort_key` 排序后执行 `trigger.func(bot, event)`：先比「是否带插件前缀」、再比 SV `priority`，然后按类型
`fullmatch` > `command` > `prefix` / `suffix` > `regex` > `keyword`，同一类型再按命令字从长到短。
`regex` / `keyword` 的 `keyword` 是 pattern 或子串，不参与长度比较，避免正则源码长度压过精确命令。

命中后按类型分轨：`type == "message"`（`on_message`）另轨入队——**不挡 AI、不进 Trace、
不调用 `count_data`**（keyword 常为 uuid4，计入命令会污染看板 / `core信息`）。其余触发器才记
命令统计，并在存在 `command_triggers` 时跳过 AI。启动时 `purge_uuid4_command_stats` 会清掉
历史 uuid4 伪命令行并回写 `CoreDataSummary.command`。

> **权限不足 = 不匹配 → 落 AI**：若某 SV `pm=2`，而用户 `user_pm=3`（权限更低），该命令
> "不匹配"，消息落入 AI 流程。如果该触发器声明了 `to_ai`，AI 可能尝试调它对应的工具——
> 此时**桥接层会再做一次同样的权限检查**并把"权限不足"文本返回给 AI（见 [§07](./07-tool-registry-and-agent.md)）。

> **输入法空格**：`Trigger` 匹配时去掉整句首尾空格（含全角空格、NBSP），并允许插件前缀和命令之间有空格。
> 命令词内部只在「中文 ↔ 英文/数字」交界允许多余空格（`绑定uid` 对得上 `绑定 uid`）；中文内部、英文内部的空格是参数分隔，保留。
> 命中后 `ev.text` 去掉命令词紧外侧的那层空格，参数内部空格不动。`raw_text` 不改写。
> `on_regex` 只去掉前缀后面的那层空格再套正则，正则里的 `\s` 仍由插件自己写。`on_keyword` 仍是字面包含，不跨交界拼词。
> 群聊热路径仍是注册时拼好的 `prefix+keyword` 做一次 `startswith`：首字对不上前缀就返回。只有首字是空格或前缀首字时才走慢匹配。
> `on_prefix` 的命令词后可以跟空格（`市盈率对比 光大银行` 与 `市盈率对比光大银行` 等价），但后面必须还有内容，裸命令不命中。
> 这条判定统一由 `Trigger._check_prefix` 承担：`check_command` 里 `prefix` 分支不落到首字探针，否则空前缀触发器（`_probe` 为空）会被误杀。

## 4.3 AI 触发条件（`handler.py`）

无命令匹配后，按以下顺序决定是否进 AI：

```
1. enable_ai 全局开关（运行时动态读取 ai_config.get_config("enable").data）
2. 黑白名单检查（user/group 在 ai_black_list / ai_white_list）
3. Persona 配置检查：persona_config_manager.get_persona_for_session(session_id)
   └── 返回 None（没有 persona 命中该 session）→ 不触发 AI
4. AI Mode 检查：
   ├── "提及应答" in ai_mode → 检查 @机器人(is_tome) 或 命中 keywords
   └── 免唤醒续聊窗口内的普通群发言 → 软触发（trigger_type="followup"，见 §4.6）
5. 唤醒判定：classify_wake(event, …) → InboxScheduler.submit(PendingEntry)；落定合并后入队 TaskContext(coro=run_turn(turn))
```

> `enable_ai` 在 `handle_ai.py` 是**函数内动态读取**而非模块级常量——WebConsole 切总开关后
> 无需重启即生效（历史缺陷 D-21 的修复，见 [§12](./12-developer-pitfalls.md)）。

### 提及应答模式判断

```python
if "提及应答" in ai_mode:
    should_respond = event.is_tome          # @机器人
    if not should_respond and keywords:
        msg_text = getattr(event, "raw_text", "") or ""
        should_respond = any(kw in msg_text for kw in keywords)
    if not should_respond:
        return
```

其他模式：`定时巡检`（见 [§08](./08-heartbeat-scheduled-planning.md)）；`命令补充`
（命令已经把回复发出去之后，按概率再补一句，见下）；`自主行动`
（作息内自己看一眼，见 [§8.4](./08-heartbeat-scheduled-planning.md)）。

### 命令补充

人格 `ai_mode` 含 `命令补充` 时才启用，默认不含。它不替代命令，也不把命令改成提及应答。

命令协程返回后：

1. 记下这次 `bot.send` 交出的文字和图片。没有可见回执则停止。
2. 当前 AI 会话尾部写入用户原话和这次回执。概率没抽中也写。之后的提及应答读的是这份 history。
3. 只发图片、只发文字、图文混合分别用 `aside_image_probability`、`aside_text_probability`、
   `aside_mixed_probability`（0 到 1）决定要不要再跑一轮点评。
4. `aside_allow_tools` 为真时，这一轮可以使用当前人格的工具，并多一个只读工具
   `read_command_plugin_file`，范围是这条命令所属插件目录。为假时这一轮没有工具。

点评那一句插在 user 侧，以 `（系统：` 开头，入史前整段剥掉。留下的是回执和点评正文。
回执追加在 history 尾部，不改 `system_prompt`。

## 4.3b HTTP Agent 面（不是 `handle_event`）

`POST /api/v1/agent/chat/stream` **禁止**注入 `handle_event`。开流后只补 H00/A 轨，然后调用
与适配器同源的 `run_passive_interactive_chat`（H01/长度/预算/session/turn）。`is_http` 的
`wait_task` 只绑命令路径，不是禁 `handle_event` 的理由。AI 总开关关闭时
`register_http_agent_routes` 不挂该面。

## 4.4 `handle_inbox_turn` 收件箱轮（`handle_ai.py`）

群聊按「多方事件流」建模，不再是一对一问答：

```
1. handler 写 A 轨：_record_inbound_history 给每条消息分配会话内单调 seq（EventLink）
2. classify_wake 判定唤醒类型：private/mention/name/quote_self/followup/receipt（hard/soft）
3. InboxScheduler.submit(PendingEntry)：同会话单飞，soft 项进 settle 合并窗口
   （群聊 hard 1.5s / 私聊 hard 0.8s / soft 5s），一次落定后入队 TaskContext(run_turn)
4. handle_inbox_turn(turn)：并发信号量 + 长度防护 → 意图分类 → get_ai_session
   → TurnRouter（soft 项先读线程视图判定，无关则沉默不进主模型）
   → run_interactive_turn：render_delta 线程视图作 B 轨输入
   → InboxKit 注入（待你回应）/（工具身份）/（相关往来）/（本群近况）块
   → turn.reached_model=True 紧邻 session.run
5. 出站：send_chat_result 先 parse_reply_plan 解析 <reply #seq>，分组发送；
   OutboundBinding 写入 EventLink（in_reply_to_seqs/evidence），回执通道不拦截
6. 结算：answered 集合只含 pending 项；未达模型不推进 read_seq，错误重试 1 次后过期
```

> `enable_ai` 在 `handle_ai.py` 仍是函数内动态读取——WebConsole 切总开关后无需重启（D-21）。

## 4.5 双层长度防护

恶意/超长输入会击穿 Token 预算，两层硬保护（沿用原 `handle_ai_chat` 口径）：

```python
ABSOLUTE_MAX_LENGTH = 60000   # 硬截断
MAX_SUMMARY_LENGTH = 15000    # 超过则子 Agent 摘要
```

| 层级 | 触发 | 处理 | 目的 |
|------|------|------|------|
| 一 | `> 60000` 字符 | 硬截断 + 提示 | 防子 Agent Token 爆炸 / API 超限 |
| 二 | `> 15000` 字符 | 子 Agent 智能摘要 | 压缩长文保留关键信息 |
| 无 | `≤ 15000` 字符 | 直接传主 Agent | 正常处理 |

## 4.6 免唤醒续聊与软唤醒（收件箱 settle）

旧「软防抖 + reactive_gate 沉默门」已由收件箱落定合并 + TurnRouter 取代：

- **续聊窗口**：`followup_window.py` 不变——硬触发登记窗口、不续费、有硬天花板与
  `follow_up_max_replies` 上限；窗口内普通发言按 `followup` soft 唤醒提交。
- **soft 唤醒**：`quote_self`（引用 Bot 的消息）与 `followup` 先入 pending，
  settle 合并多条为一轮；TurnRouter 读 `render_router_view` 线程视图判相关性，
  判无关 → `route_decision.soft_speak=None` 即沉默，不进主模型。正文点名人格升 hard。
- **引用≠找你**：TurnRouter 与主模型读同一份事实（被引用对象的完整出处：
  `↩#n(你→名 · 时间 · 闲聊回复)`），router prompt 写明原则，不再替模型下结论。
- **回执唤醒**：`command_aside` 命令回执以 `receipt` 类型进收件箱（A 轨 `你[回执]` 行），
  批次含 hard 项时不受回执工具策略限制。

| 配置 | 默认 | 说明 |
|------|------|------|
| `follow_up_window` | 30s | 续聊窗口长度 |
| `follow_up_max_total` | 300s | 硬天花板 |
| `follow_up_max_replies` | 2 | 单窗口内续聊成功回复上限 |
| `inbox_settle_max_ms` | 5000 | 落定合并上限 |
| `inbox_feedback_window` | 120 | 「有人接/没人接」观察窗 |

`statistics` 触发分布按 wake kind 记录。

## 4.7 命令补充（`aside` → 回执唤醒）

`ai_core/command_aside/` 不再做概率点评 run，改为向收件箱提交 `receipt` 唤醒项：

- **回执入 A 轨**：命令执行完毕的回执以 `你[回执]` 行进线程视图，增量块可见；
  不再向会话追加合成 request/response 对（不打断增量历史与 run 锁）。
- **建议记忆**：仅对失败或用法说明抽取骨架存入记忆，脱敏数字与变量，7 天 TTL、200 条上限。
- **工具策略**：轮内含回执项时按回执的 allow_tools/advice_memory 绑定工具策略；
  批次含 hard 项时不封锁工具。
- **结构排除**：`pm <= 2` 的服务、`SV(aside=...)`、触发器 `aside`、元事件
  `on_meta(aside=...)`、人格 `aside_skip_plugins` 与 `aside_cooldown_seconds` 全部沿用。

## 4.8 触发方式统计维度

| 唤醒类型 | 说明 | 记录位置 |
|---------|------|----------|
| `private` | 私聊消息 | `inbox/state.py::classify_wake` |
| `mention` | @机器人 | `inbox/state.py::classify_wake` |
| `name` | 正文呼名（人格名/关键词/别名） | `inbox/state.py::classify_wake` |
| `quote_self` | 引用 Bot 已发消息 | `inbox/state.py::classify_wake` |
| `followup` | 免唤醒续聊窗口内发言 | `followup_window` + `classify_wake` |
| `receipt` | 命令回执（原 aside） | `command_aside/runner.py` |
| `heartbeat` | 定时巡检主动发言 | `heartbeat/inspector.py` |
| `scheduled` | 定时/循环任务 | `scheduled_task/executor.py` |
