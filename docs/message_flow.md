# GsCore 消息流转与命令触发机制

## 概述

GsCore 是一个基于 FastAPI + WebSocket 的 QQ 机器人核心框架，支持插件化扩展、命令触发机制和 AI 聊天功能。

---

## 一、消息流转时序图

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              消息流转完整时序图                                 │
└─────────────────────────────────────────────────────────────────────────────────┘

     用户                              GsCore                                插件/SV
      │                                  │                                      │
      │  发送消息                         │                                      │
      │────────────────────────────────►│                                      │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │  WebSocket  │                              │
      │                           │   接收消息   │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ 解码Message │                              │
      │                           │  Receive    │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ handle_event │                              │
      │                           │   处理入口   │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ 权限检查/   │                              │
      │                           │ 黑名单检查  │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ msg_process │                              │
      │                           │ 消息解析    │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ Event对象  │                              │
      │                           │   构建     │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ 命令前缀   │                              │
      │                           │   检查     │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ 遍历所有SV │                              │
      │                           │  触发器    │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ 触发器匹配  │                              │
      │                           │ 优先级排序  │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ 任务加入   │                              │
      │                           │  队列      │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ Bot实例   │                              │
      │                           │  创建     │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ _Bot._process│                             │
      │                           │  异步执行   │                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │                                  │─────►┌─────────────────────────┐    │
      │                                  │      │ 插件命令函数执行        │    │
      │                                  │      │ (SV.on_xxx decorated)   │    │
      │                                  │      └─────────────────────────┘    │
      │                                  │                                      │
      │                           ┌──────┴──────┐                              │
      │                           │ 消息发送   │                              │
      │                           │ target_send│                              │
      │                           └──────┬──────┘                              │
      │                                  │                                      │
      │◄─────────────────────────────────│                                      │
      │     消息发送回用户/群组            │                                      │
      │                                  │                                      │
```

---

## 二、消息流转详细过程

### 2.1 消息入口

消息通过两种方式进入系统：

#### WebSocket 方式（主要方式）
```
第三方QQ框架 ──WebSocket──► core.py::websocket_endpoint()
                                │
                                ▼
                          MessageReceive 解码
                                │
                                ▼
                          handle_event(bot, msg)
```

#### HTTP 方式（可选）
```
HTTP POST /api/send_msg ──► handle_event(_bot, MR, is_http=True)
```

### 2.2 核心处理流程

```python
# gsuid_core/handler.py::handle_event()

async def handle_event(ws: _Bot, msg: MessageReceive, is_http: bool = False):
    # 1. 全局开关检查
    if not IS_HANDDLE:
        return

    # 2. 黑名单/屏蔽检查
    if msg.user_id in black_list or msg.group_id in black_list:
        return

    # 3. 权限等级获取
    msg.user_pm = user_pm = await get_user_pml(msg)

    # 4. 消息解析 - 构建 Event 对象
    event = await msg_process(msg)

    # 5. 用户/群组数据库记录
    await CoreUser.insert_user(...)
    await CoreGroup.insert_group(...)

    # 6. 命令前缀处理
    for start in _command_start:
        if event.raw_text.strip().startswith(start):
            event.raw_text = event.raw_text.replace(start, "", 1)
            is_start = True

    # 7. 触发器遍历与匹配
    valid_event: Dict[Trigger, int] = {}
    for sv in SL.lst:
        for _type in SL.lst[sv].TL:
            for tr in SL.lst[sv].TL[_type]:
                # 权限、黑白名单、区域检查
                if conditions_match(trigger, event, user_pm, sv):
                    valid_event[trigger] = priority

    # 8. 优先级排序执行
    if len(valid_event) >= 1:
        sorted_event = sorted(valid_event.items(), key=lambda x: (not x[0].prefix, x[1]))
        for trigger, _ in sorted_event:
            bot = Bot(ws, _event)
            coro = trigger.func(bot, message)
            task_ctx = TaskContext(coro=coro, name=func_name, priority=user_pm)
            ws.queue.put_nowait(task_ctx)

            # 如果是 HTTP 模式，等待任务完成
            if _event.task_event:
                return await ws.wait_task(_event.task_id, _event.task_event)

            # 如果 trigger.block == True，停止处理后续触发器
            if trigger.block:
                break
```

### 2.3 触发器类型

| 类型 | 匹配方式 | 配置示例 |
|------|----------|----------|
| `prefix` | 消息以指定前缀开头 | `#帮助` 匹配 `#帮助xxx` |
| `suffix` | 消息以指定后缀结尾 | `了吗` 匹配 `xxx了吗` |
| `keyword` | 消息包含关键词 | `帮助` 匹配 `帮我查一下帮助` |
| `fullmatch` | 消息完全匹配 | `帮助` 仅匹配 `帮助` |
| `command` | 命令匹配（带前缀） | `help` 匹配 `.help` |
| `regex` | 正则表达式匹配 | `^抽卡(.*)` 匹配 `抽卡十连` |
| `file` | 文件类型匹配 | 仅当消息包含文件时触发 |
| `message` | 消息类触发器 | 所有消息都可能触发 |

### 2.4 权限系统

```
权限等级 (user_pm 越小越高):
0  - 主人 (masters)
1  - 超级用户 (superusers)
2  - 普通用户
3+ - 受限用户
```

---

## 三、命令触发模式

### 3.1 插件/SV 定义方式

```python
# gsuid_core/sv.py

# 定义一个 SV（服务）
sv = SV(
    name="抽卡",
    pm=6,           # 默认权限等级
    priority=5,     # 优先级
    area="ALL",     # 作用区域: ALL/GROUP/DIRECT
)

# 定义触发器 - 使用装饰器
@sv.on_command("十连")
async def gacha_10(bot: Bot, event: Event):
    """十连抽卡"""
    await bot.send("正在抽卡...")
    # 抽卡逻辑
    await bot.send(result)
```

### 3.2 触发器注册流程

```
插件导入 ──► SV.__init__() ──► SL.lst[name] = sv ──► 装饰器触发
                                                      │
                                                      ▼
                                              Trigger.__init__()
                                                      │
                                                      ▼
                                              sv.TL[type][keyword] = trigger
```

### 3.3 命令匹配流程

```
消息 "帮助" 进入系统
         │
         ▼
┌─────────────────────────────────┐
│ 遍历 SL.lst 中所有 SV           │
│ 检查:                           │
│   - SV.enabled == True          │
│   - user_pm <= SV.pm            │
│   - 区域匹配 (GROUP/DIRECT/ALL)  │
│   - 黑白名单检查                 │
└─────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────┐
│ 遍历每个 SV 的 TL (Trigger List) │
│ 调用 trigger.check_command()    │
└─────────────────────────────────┘
         │
         ▼
   ┌─────┴─────┐
   │ 匹配成功?  │
   └─────┬─────┘
      Yes│    No
         │    │
         ▼    ▼
   加入valid_event  继续遍历
   字典，按优先级
   排序
         │
         ▼
   按优先级顺序
   执行触发器函数
```

### 3.4 消息回复机制

```python
# gsuid_core/bot.py::Bot.send()

class Bot:
    async def send(
        self,
        message: Union[Message, List[Message], str],
        at_sender: bool = False,
    ):
        await self.ws.target_send(
            message=message,
            target_type=self.event.user_type,
            target_id=self.event.user_id if self.event.user_type == "direct" else self.event.group_id,
            ...
        )
```

---

## 四、AI 聊天流程

当没有命令匹配时，系统会尝试进行 AI 聊天。详细流程请参阅 [`docs/ai_handle_flow.md`](ai_handle_flow.md)。

### 4.1 简化流程

```python
# handler.py::handle_event()

if len(valid_event) >= 1:
    # 有命令匹配，执行命令
    ...
else:
    # 无命令匹配，检查是否启用 AI
    if enable_ai:
        bot = Bot(ws, event)
        coro = handle_ai_chat(bot, event)  # 异步执行
```

### 4.2 AI Handle 流程

```python
# handle_ai.py::handle_ai_chat()

async def handle_ai_chat(bot: Bot, event: Event):
    # 1. 意图识别
    res = await classifier_service.predict_async(event.raw_text)
    intent = res["intent"]  # "闲聊" / "工具" / "问答"

    # 2. 根据意图检查开关
    if intent == "闲聊" and not enable_chat:
        return
    elif intent == "工具" and not enable_task:
        return
    elif intent == "问答" and not enable_qa:
        return

    # 3. 获取Session (persona只在创建时设置一次)
    session = await get_ai_session(event)

    # 4. 根据意图准备RAG上下文
    rag_context = None
    if intent == "工具":
        # 工具模式：检索知识库作为上下文
        knowledge_results = await query_knowledge(normalized_query)
        rag_context = "【参考资料】\n" + ...
    elif intent == "问答":
        # 问答模式：检索知识库
        knowledge_results = await query_knowledge(normalized_query)
        rag_context = "【参考资料】\n" + ...

    # 5. 调用Agent生成回复
    chat_result = await session.run(
        user_message=user_messages,
        bot=bot,
        ev=event,
        rag_context=rag_context,  # RAG上下文传递
    )

    # 6. 发送回复
    await bot.send(chat_result)
```

### 4.3 Session 管理

```python
# ai_router.py::SessionManager

class SessionManager:
    CLEANUP_INTERVAL = 3600   # 每小时检查一次
    IDLE_THRESHOLD = 86400    # 24小时无访问则清理

# Session创建时会根据群组配置选择persona
async def get_ai_session(event: Event) -> GsCoreAIAgent:
    session_id = f"{event.user_id}_{event.group_id}"
    # 根据group_id匹配persona配置
    for p in personas:
        if event.group_id in group_personas.get(p, []):
            base_persona = await build_persona_prompt(p)
            break
    else:
        base_persona = await build_persona_prompt("智能助手")
```

---

## 五、插件生命周期

### 5.1 插件加载

```
项目启动
    │
    ▼
load_gss(dev_mode)
    │
    ▼
gss.load_plugins()
    │
    ▼
遍历 PLUGIN_PATH 和 BUILDIN_PLUGIN_PATH
    │
    ├──► 发现 __init__.py ──► 作为插件包加载
    ├──► 发现 __full__.py ──► 加载目录内所有模块
    ├──► 发现 __nest__.py ──► 嵌套加载模式
    └──► 发现 *.py ──► 作为单文件插件加载
    │
    ▼
cached_import()  # 动态导入
    │
    ▼
写入 sys.modules 缓存
    │
    ▼
触发 @sv.on_xxx 装饰器注册触发器
```

### 5.2 配置填充

```
插件首次加载
    │
    ▼
Plugins.__new__() 检查 name 是否已存在
    │
    ▼
若不存在:
    │
    ├──► 从 plugins_sample 复制默认配置
    ├──► config_plugins[plugins_name] = _plugins_config
    └──► 写入 core_config (懒加载)
    │
    ▼
SV.__init__() 注册到 SL.lst
    │
    ▼
装饰器 @sv.on_command() 等
    │
    ▼
Trigger 注册到 sv.TL
```

---

## 六、关键数据结构

### 6.1 MessageReceive (输入消息)

```python
class MessageReceive(Struct):
    bot_id: str = "Bot"              # Bot ID
    bot_self_id: str = ""            # 机器人自身 ID
    msg_id: str = ""                 # 消息 ID
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: Optional[str] = None   # 群 ID
    user_id: str = ""                # 用户 ID
    sender: Dict[str, Any] = {}       # 发送者信息
    user_pm: int = 3                 # 用户权限等级
    content: List[Message] = []       # 消息内容列表
```

### 6.2 Event (处理中事件)

```python
class Event(MessageReceive):
    # 额外字段
    raw_text: str = ""               # 原始文本
    command: str = ""                # 匹配到的命令
    text: str = ""                   # 命令参数
    image: Optional[str] = None       # 图片 URL
    is_tome: bool = False             # 是否 @ 机器人
    at: Optional[str] = None         # @ 的目标
    regex_dict: Dict[str, str] = {}  # 正则匹配结果
```

### 6.3 Trigger (触发器)

```python
class Trigger:
    type: Literal["prefix", "suffix", "keyword", ...]  # 触发类型
    keyword: str                      # 关键词
    func: Callable[[Bot, Event], Awaitable[Any]]  # 处理函数
    prefix: str = ""                  # 命令前缀
    block: bool = False               # 是否阻止后续触发器
    to_me: bool = False               # 是否需要 @ 机器人
```

### 6.4 Bot (_Bot) 核心类

```python
class _Bot:
    bot_id: str                       # Bot ID
    bot: WebSocket                     # WebSocket 连接
    logger: GsLogger                   # 日志器
    queue: asyncio.PriorityQueue       # 任务优先级队列
    send_dict: Dict                    # HTTP 模式发送缓存
    bg_tasks: Set                      # 后台任务集合
    sem: asyncio.Semaphore            # 并发信号量 (10)
```

---

## 七、关键配置文件

| 配置项 | 路径 | 说明 |
|--------|------|------|
| `config.json` | `gsuid_core/data/config.json` | 核心配置 |
| `plugins/` | 各插件目录下 | 插件独立配置 |
| `sp_config` | `gsuid_core/utils/plugins_config/sp_config.py` | 特殊配置 |
| `bm_config` | `gsuid_core/utils/plugins_config/buttons_and_markdown_config.py` | 按钮/_markdown配置 |
