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
5.  主人识别：user_pm == 0 且未订阅 → 自动订阅"主人用户"
6.  用户/群组入库（CoreUser.insert_user / CoreGroup.insert_group）
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
有匹配则按 priority 排序执行 `trigger.func(bot, event)`。

> **权限不足 = 不匹配 → 落 AI**：若某 SV `pm=2`，而用户 `user_pm=3`（权限更低），该命令
> "不匹配"，消息落入 AI 流程。如果该触发器声明了 `to_ai`，AI 可能尝试调它对应的工具——
> 此时**桥接层会再做一次同样的权限检查**并把"权限不足"文本返回给 AI（见 [§07](./07-tool-registry-and-agent.md)）。

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
5. 任务入队：ws.queue.put_nowait(TaskContext(coro=handle_ai_chat(...)))
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

其他模式：`定时巡检`（见 [§08](./08-heartbeat-scheduled-planning.md)）；`趣向捕捉` / `困境救场`
暂不可用。

## 4.4 `handle_ai_chat` 全链路（`handle_ai.py`）

```
1. enable_ai 检查（动态）→ 关则 return
2. 并发控制：async with _ai_semaphore（Semaphore(10)）
3. 双层长度防护（见 §4.5）
4. 意图识别：classifier_service.predict_async(query) → 闲聊 / 工具 / 问答
5. 获取 AI Session：session = await get_ai_session(event)（见 §06）
6. 记忆检索：dual_route_retrieve() 双路检索（见 §09），拼进 full_context
7. 历史上下文：format_history_for_agent() 近 30 条 → rag_context = "【历史对话】..."
   注意：RAG 知识库检索不再前置强制，由主 Agent 的 search_knowledge 工具按需调用
8. 调 Agent：chat_result = await session.run(user_message, bot, ev, rag_context)
9. 发送回复：send_chat_result(bot, chat_result)（支持 @用户ID 解析 + 打字延迟）
10. 记忆观察：observe() 把 AI 回复入队记忆系统
```

> **软触发的沉默门**：续聊软触发的消息（见 §4.6）在进主链路前先过一道
> `heartbeat/decision.run_reactive_gate`（复用 Heartbeat 轻量结构判断"是否还在跟我说话"），
> 与 AI 无关则直接沉默、不进主链路、不消耗主 Agent。

## 4.5 双层长度防护

恶意/超长输入会击穿 Token 预算。`handle_ai_chat` 两层硬保护：

```python
ABSOLUTE_MAX_LENGTH = 60000   # 第一层：硬截断（防子 Agent Token 爆炸 / API 超限）
MAX_SUMMARY_LENGTH = 15000    # 第二层：超过则调 create_subagent 智能摘要

if len(event.raw_text) > ABSOLUTE_MAX_LENGTH:
    query = query[:ABSOLUTE_MAX_LENGTH] + "...[文本过长，已自动截断]"
    event.raw_text = query
if len(event.raw_text) > MAX_SUMMARY_LENGTH:
    summarized = await create_subagent(ctx=None, task=f"请总结...{event.raw_text}", ...)
    user_messages = summarized
```

| 层级 | 触发 | 处理 | 目的 |
|------|------|------|------|
| 一 | `> 60000` 字符 | 硬截断 + 提示 | 防子 Agent Token 爆炸 / API 超限 |
| 二 | `> 15000` 字符 | 子 Agent 智能摘要 | 压缩长文保留关键信息 |
| 无 | `≤ 15000` 字符 | 直接传主 Agent | 正常处理 |

> 第二层阈值 15000 是因为现代 LLM 上下文动辄 128K，2000 字符对它毫无压力。代码/报错日志等
> 长文摘要会丢细节，应尽量避免自动摘要——所以阈值定得较高。

## 4.6 免唤醒续聊（软触发）

`ai_core/followup_window.py`（纯进程内存 + TTL 惰性清理）。

- **登记**：`handler.py` 在硬触发（@/关键词/私聊）时登记窗口起点。
- **放行**：未硬触发时，若用户处于窗口内、且是群聊里未 @ 别人的普通发言，按"软触发"放行
  （`trigger_type="followup"`）。
- **沉默门**：软触发消息在 `handle_ai` 先过 `run_reactive_gate`，**默认偏沉默**——仅当明确接续
  你们刚才的话题才放行，判不出明确指向你（含模型输出非 str / JSON 解析失败 / 缺 `should_speak`）
  一律沉默；只有**真异常**（无历史 / 无人格 / LLM 调用崩溃）才放行交主 Agent 兜底。
- **三条硬规则**：① 窗口从硬触发起算；② 续聊**不续费**（说话不会延长窗口）；③ 有硬天花板。

| 配置 | 默认 | 说明 |
|------|------|------|
| `follow_up_window` | 30s | 续聊窗口长度 |
| `follow_up_max_total` | 300s | 硬天花板 |

`statistics` 触发分布新增 `followup` 维度。

> ⚠️ **成本**：默认 30s 内每条群消息触发一次沉默门 LLM 判定，群活跃时有额外开销；沉默门默认偏
> 沉默（仅模型明确判定"接续/指向你"才放行，模型输出无法解析时也按沉默处理），仅真异常放行交主
> Agent 兜底。窗口/天花板按群活跃度观察调整。进程内存状态多实例不共享。
> 详见 [§12](./12-developer-pitfalls.md)。

> 🧱 **三道沉默关卡（叠加）**：续聊要落地输出须穿过 ① `run_reactive_gate` 沉默门（默认偏沉默）
> → ② `handle_ai` 给主 Agent 追加的"续聊软触发默认按路过处理"提示 → ③ 主 Agent 系统提示词
> `## 沉默规则` 的"续聊场景"条款。三者口径已对齐为"判不出明确指向你就 `<SILENCE>`"，避免历史上
> 门放水 + 主 Agent 把续聊当"直接找你必须回应"导致几乎不沉默。改其一时务必同步另外两处。

## 4.7 触发方式统计维度

| 触发方式 | 说明 | 记录位置 |
|---------|------|----------|
| `mention` | 用户 @机器人 | `handler.py` |
| `keyword` | 关键词触发 | `handler.py` |
| `followup` | 免唤醒续聊软触发 | `followup_window` / `handle_ai` |
| `heartbeat` | 定时巡检主动发言 | `heartbeat/inspector.py` |
| `scheduled` | 定时/循环任务 | `scheduled_task/executor.py` |
