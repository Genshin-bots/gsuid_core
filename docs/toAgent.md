# GsCore AI Agent 开发指南

## 概述

GsCore 是一个基于 FastAPI + WebSocket 的 QQ 机器人核心框架，支持插件化扩展、命令触发机制和 AI 聊天功能。本文档旨在为 AI Agent 提供项目全部的模块设计、项目规范和设计流程等全部细节。

---

## 一、项目整体架构

### 1.1 目录结构

```
gsuid_core/
├── core.py              # 主入口，WebSocket/HTTP服务启动
├── handler.py           # 消息处理入口，分发到触发器或AI
├── bot.py               # Bot类，消息发送核心
├── models.py            # 核心数据结构：MessageReceive, Event, Message, TaskContext
├── sv.py                # SV服务类，插件定义
├── trigger.py           # 触发器定义，支持多种匹配模式
├── server.py            # 插件加载管理
├── gss.py               # GsServer单例，Bot连接管理
├── config.py            # 核心配置管理
├── subscribe.py         # 订阅系统
├── global_val.py        # 全局变量管理
├── data_store.py        # 数据存储路径
├── logger.py            # 日志模块
├── gs_logger.py         # Bot专用日志
├── aps.py               # APScheduler调度器
├── pool.py              # 连接池管理
├── segment.py           # 消息segment处理
├── message_models.py    # 按钮等消息模型
├── web_app.py           # FastAPI应用
├── webconsole/          # Web控制台API
├── buildin_plugins/     # 内置插件
├── utils/               # 工具模块
├── status/              # 状态相关
└── ai_core/             # AI核心模块
```

### 1.2 核心模块依赖关系

```
WebSocket/HTTP请求
       │
       ▼
  core.py::websocket_endpoint() / sendMsg()
       │
       ▼
  handler.py::handle_event()
       │
       ├──► 遍历SV触发器 ──► Bot._process() ──► 插件命令函数
       │
       └──► 无匹配时 ──► handle_ai_chat() ──► AI对话
                              │
                              ▼
                    ai_router.py::get_ai_session()
                              │
                              ▼
                    gs_agent.py::GsCoreAIAgent.run()
                              │
                              ▼
                    buildin_tools (RAG/WebSearch/...)
```

---

## 二、消息流转机制

### 2.1 消息入口

消息通过两种方式进入系统：

1. **WebSocket方式（主要）**：
   ```
   第三方QQ框架 ──WebSocket──► core.py::websocket_endpoint()
                                         │
                                         ▼
                                   MessageReceive解码
                                         │
                                         ▼
                                   handle_event(bot, msg)
   ```

2. **HTTP方式（可选）**：
   ```
   HTTP POST /api/send_msg ──► handle_event(_bot, MR, is_http=True)
   ```

### 2.2 核心处理流程

位于 [`handler.py::handle_event()`](gsuid_core/handler.py:56)

1. 全局开关检查 (`IS_HANDDLE`)
2. 黑名单/屏蔽检查
3. 权限等级获取 (`user_pm`)
4. 消息解析 - 构建 `Event` 对象
5. 用户/群组数据库记录
6. 命令前缀处理
7. 遍历所有SV触发器进行匹配
8. 优先级排序执行
9. 无匹配时进入AI聊天流程

### 2.3 关键数据结构

#### MessageReceive（输入消息）
```python
class MessageReceive(Struct):
    bot_id: str = "Bot"              # Bot ID
    bot_self_id: str = ""            # 机器人自身ID
    msg_id: str = ""                 # 消息ID
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "group"
    group_id: Optional[str] = None   # 群ID
    user_id: str = ""                # 用户ID
    sender: Dict[str, Any] = {}       # 发送者信息
    user_pm: int = 3                 # 用户权限等级
    content: List[Message] = []       # 消息内容列表
```

#### Event（处理中事件）
```python
class Event(MessageReceive):
    raw_text: str = ""               # 原始文本
    command: str = ""                # 匹配到的命令
    text: str = ""                   # 命令参数
    image: Optional[str] = None       # 图片URL
    is_tome: bool = False             # 是否@机器人
    at: Optional[str] = None         # @的目标
    regex_dict: Dict[str, str] = {}  # 正则匹配结果
```

---

## 三、命令触发系统

### 3.1 触发器类型

位于 [`trigger.py`](gsuid_core/trigger.py)

| 类型 | 匹配方式 | 示例 |
|------|----------|------|
| `prefix` | 消息以指定前缀开头 | `#帮助` 匹配 `#帮助xxx` |
| `suffix` | 消息以指定后缀结尾 | `了吗` 匹配 `xxx了吗` |
| `keyword` | 消息包含关键词 | `帮助` 匹配 `帮我查一下帮助` |
| `fullmatch` | 消息完全匹配 | `帮助` 仅匹配 `帮助` |
| `command` | 命令匹配（带前缀） | `help` 匹配 `.help` |
| `regex` | 正则表达式匹配 | `^抽卡(.*)` 匹配 `抽卡十连` |
| `file` | 文件类型匹配 | 仅当消息包含文件时触发 |
| `message` | 所有消息都可能触发 | - |

### 3.2 SV（服务）定义

位于 [`sv.py`](gsuid_core/sv.py)

```python
sv = SV(
    name="抽卡",
    pm=6,           # 默认权限等级
    priority=5,     # 优先级
    area="ALL",     # 作用区域: ALL/GROUP/DIRECT
)

@sv.on_command("十连")
async def gacha_10(bot: Bot, event: Event):
    """十连抽卡"""
    await bot.send("正在抽卡...")
```

### 3.3 触发器注册流程

```
插件导入 ──► SV.__init__() ──► SL.lst[name] = sv ──► 装饰器触发
                                                      │
                                                      ▼
                                              Trigger.__init__()
                                                      │
                                                      ▼
                                              sv.TL[type][keyword] = trigger
```

### 3.4 权限系统

权限等级 (`user_pm`) 越小越高：

| 等级 | 说明 |
|------|------|
| 0 | 主人 (masters) |
| 1 | 超级用户 (superusers) |
| 2 | 普通用户 |
| 3+ | 受限用户 |

---

## 四、AI聊天系统

### 4.1 AI处理流程

位于 [`ai_core/handle_ai.py::handle_ai_chat()`](gsuid_core/ai_core/handle_ai.py:34)

```
用户消息
    │
    ▼
意图识别 (classifier_service.predict_async)
    │
    ├──► "闲聊" ──► enable_chat检查
    ├──► "工具" ──► enable_task检查 ──► RAG检索工具+知识库
    └──► "问答" ──► enable_qa检查 ──► RAG检索知识库
    │
    ▼
获取Session (get_ai_session)
    │
    ▼
调用Agent生成回复 (session.run)
    │
    ▼
发送回复 (bot.send)
```

### 4.2 AI Core模块结构

位于 `gsuid_core/ai_core/`

| 模块 | 说明 |
|------|------|
| `handle_ai.py` | AI聊天入口，意图识别，流程分发 |
| `ai_router.py` | Session管理，路由创建和清理 |
| `gs_agent.py` | PydanticAI Agent封装 |
| `ai_config.py` | AI配置管理 |
| `normalize.py` | 查询归一化 |
| `register.py` | 工具注册装饰器 `@ai_tools` |
| `models.py` | ToolContext, KnowledgeBase等数据模型 |
| `classifier/` | 意图分类器 |
| `persona/` | 角色提示词管理 |
| `buildin_tools/` | 内建工具（RAG搜索/Web搜索/消息发送等） |
| `rag/` | 向量数据库RAG实现 |
| `database/` | AI数据库访问层 |
| `web_search/` | Web搜索实现 |

### 4.3 Session管理

位于 [`ai_core/ai_router.py::SessionManager`](gsuid_core/ai_core/ai_router.py:40)

```python
class SessionManager:
    CLEANUP_INTERVAL = 3600   # 每小时检查一次
    IDLE_THRESHOLD = 86400    # 24小时无访问则清理
    MAX_HISTORY_LENGTH = 50   # 最大历史消息长度
```

Session创建时根据group_id匹配persona配置，system_prompt只在创建时设置一次。

### 4.4 工具注册系统

位于 [`ai_core/register.py`](gsuid_core/ai_core/register.py)

使用 `@ai_tools` 装饰器注册工具：

```python
from gsuid_core.ai_core.register import ai_tools

@ai_tools()
async def search_knowledge(ctx: RunContext[ToolContext], query: str):
    """搜索知识库"""
    ...
```

使用 `@ai_alias` 注册别名：

```python
from gsuid_core.ai_core.register import ai_alias

ai_alias("丝柯克", ['skk', '斯柯克'])
```

使用 `@ai_entity` 注册实体知识：

```python
from gsuid_core.ai_core.register import ai_entity

@ai_entity
def character_entity():
    return KnowledgePoint(...)
```

### 4.5 内建工具

位于 `gsuid_core/ai_core/buildin_tools/`

| 工具 | 功能 |
|------|------|
| `rag_search.py` | 知识库RAG检索 |
| `web_search.py` | Web搜索（Tavily API） |
| `message_sender.py` | 发送消息 |
| `command_executor.py` | 执行Shell命令（带安全检查） |
| `database_query.py` | 数据库查询 |
| `favorability_manager.py` | 好感度管理 |

### 4.6 RAG系统

位于 `gsuid_core/ai_core/rag/`

- `base.py`: 向量数据库连接，embedding模型
- `tools.py`: 工具向量存储和检索
- `knowledge.py`: 知识库同步和查询
- `reranker.py`: 结果重排序

---

## 五、WebConsole系统

### 5.1 API概览

位于 `gsuid_core/webconsole/`

| API模块 | 路径前缀 | 功能 |
|---------|----------|------|
| `auth_api.py` | `/api/auth` | 用户认证 |
| `system_api.py` | `/api/system` | 系统信息 |
| `plugins_api.py` | `/api/plugins` | 插件管理 |
| `core_config_api.py` | `/api/core` | 核心配置 |
| `database_api.py` | `/api/database` | 数据库操作 |
| `backup_api.py` | `/api/backup` | 备份管理 |
| `logs_api.py` | `/api/logs` | 日志查看 |
| `scheduler_api.py` | `/api/scheduler` | 调度器管理 |
| `dashboard_api.py` | `/api/dashboard` | 仪表盘数据 |
| `message_api.py` | `/api/BatchPush` | 消息推送 |
| `assets_api.py` | `/api/assets` | 静态资源 |
| `theme_api.py` | `/api/theme` | 主题配置 |
| `persona_api.py` | `/api/persona` | AI角色管理 |
| `ai_tools_api.py` | `/api/ai/tools` | AI工具管理 |

### 5.2 通用响应格式

```json
{
    "status": 0,
    "msg": "ok",
    "data": {}
}
```

- `status`: 0=成功，1=失败
- `msg`: 状态描述
- `data`: 响应数据

### 5.3 认证方式

除特殊说明外，所有API需通过 `Authorization: Bearer <token>` Header携带访问令牌。

---

## 六、插件系统

### 6.1 插件加载机制

位于 [`server.py::GsServer`](gsuid_core/server.py)

插件目录：
- `PLUGIN_PATH`: 用户插件目录
- `BUILDIN_PLUGIN_PATH`: 内置插件目录

插件类型：
- **包插件**: 包含 `__init__.py` 的目录
- **`__full__.py`**: 加载目录内所有模块
- **`__nest__.py`**: 嵌套加载模式
- **单文件插件**: 单独的 `.py` 文件

### 6.2 插件配置

首次加载时从 `plugins_sample` 复制默认配置，包含：
- `pm`: 权限等级
- `priority`: 优先级
- `enabled`: 是否启用
- `area`: 作用区域
- `black_list`/`white_list`: 黑白名单
- `prefix`: 命令前缀

---

## 七、数据库模型

### 7.1 核心表

位于 `gsuid_core/utils/database/models.py`

| 表名 | 说明 |
|------|------|
| `User` | 用户表 |
| `Bind` | 绑定表 |
| `Push` | 推送表 |
| `Cache` | 缓存表 |
| `Subscribe` | 订阅表 |
| `CoreUser` | 核心用户表 |
| `CoreGroup` | 核心群组表 |
| `CoreTag` | 标签表 |

### 7.2 数据库访问

使用 SQLModel + AsyncSession，装饰器 `@with_session` 提供会话管理。

---

## 八、配置系统

### 8.1 核心配置

位于 [`config.py`](gsuid_core/config.py)

- `HOST`/`PORT`: 服务监听地址
- `WS_TOKEN`: WebSocket认证令牌
- `masters`/`superusers`: 权限用户列表
- `command_start`: 命令前缀列表
- `log`: 日志配置

### 8.2 AI配置

位于 `ai_core/ai_config.py`

- `enable`: 总开关
- `enable_chat`/`enable_qa`/`enable_task`: 各模式开关
- `need_at`: 是否需要@机器人
- `black_list`/`white_list`: AI黑白名单
- `persona_config`: 角色配置
- `openai_config`: 模型配置

---

## 九、关键设计模式

### 9.1 单例模式

- `GsServer`: 全局唯一服务器实例
- `SL (SVList)`: 全局SV列表
- `gss`: 全局GsServer实例

### 9.2 装饰器模式

- `@sv.on_command()`: 命令触发器注册
- `@ai_tools()`: AI工具注册
- `@on_core_start()`: 启动钩子

### 9.3 门面模式

`buildin_tools` 模块采用Facade模式，封装底层 `rag`、`web_search`、`database` 等模块。

---

## 十、项目规范

### 10.1 代码风格

- 使用类型注解
- 使用 `msgspec.Struct` 定义数据结构
- 使用 `async/await` 异步编程
- 日志使用 `logger`

### 10.2 日志级别

```python
logger.trace()  # 跟踪
logger.debug()  # 调试
logger.info()   # 信息
logger.warning() # 警告
logger.error()  # 错误
logger.exception()  # 异常（自动包含堆栈）
```

### 10.3 错误处理

- 使用 `try/except` 捕获异常
- 使用 `logger.exception()` 记录错误
- 不让异常向上传播导致服务崩溃

### 10.4 配置更新

使用懒加载机制 `core_config.lazy_set_config()` 批量更新配置。

---

## 十一、启动流程

```
python -m gsuid_core
    │
    ▼
core.py::main()
    │
    ├──► init_database() - 初始化数据库
    ├──► load_gss(dev_mode) - 加载插件
    │
    └──► uvicorn启动FastAPI服务
              │
              ├──► WebSocket /ws/{bot_id}
              └──► HTTP POST /api/send_msg
```

---

## 十二、常用工具函数

### 12.1 Bot.send()

位于 [`bot.py`](gsuid_core/bot.py:281)

```python
async def send(
    self,
    message: Union[Message, List[Message], str],
    at_sender: bool = False,
):
```

### 12.2 MessageSegment

位于 [`segment.py`](gsuid_core/segment.py)

```python
MessageSegment.text("文本")
MessageSegment.image("image_id")
MessageSegment.at(user_id)
MessageSegment.reply(msg_id)
```

### 12.3 资源管理

位于 `utils/resource_manager.py`

```python
RM.register(data) -> resource_id
RM.get(resource_id) -> data
```

---

## 十三、注意事项

1. **不要阻塞事件循环**: 所有IO操作必须使用async/await
2. **注意权限检查**: 操作前验证user_pm
3. **使用懒加载**: 配置更新使用lazy_set_config
4. **Session一致性**: Session的system_prompt只在创建时设置
5. **工具安全**: command_executor有危险命令检测
6. **RAG检索**: 使用query_knowledge进行知识库检索
