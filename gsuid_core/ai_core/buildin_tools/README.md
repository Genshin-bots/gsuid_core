# Buildin Tools 模块文档

系统内建AI元工具模块，提供自主型AI常用的基础工具函数。

此模块是 Facade 模式，引用 `ai_core.rag`、`ai_core.web_search`、`ai_core.database` 等底层模块的实现。
核心逻辑在对应的底层模块中。

## 文件结构

```
buildin_tools/
├── __init__.py              # 模块导出（Facade接口）
├── rag_search.py             # RAG检索工具（引用 ai_core.rag.query_knowledge）
├── web_search.py             # Web搜索工具（引用 ai_core.web_search.tavily_search）
├── message_sender.py         # 消息发送工具
├── command_executor.py       # 命令执行工具
├── database_query.py         # 数据库查询工具
├── favorability_manager.py   # 好感度管理工具（引用 ai_core.database.AIDAL）
└── README.md                 # 使用文档
```

## 架构说明

`buildin_tools` 采用 Facade 模式，提供统一的工具接口：

| 工具模块 | 底层依赖 | 说明 |
|---------|---------|------|
| rag_search.py | ai_core.rag.query_knowledge | 知识库检索封装 |
| web_search.py | ai_core.web_search.tavily_search | Web搜索封装 |
| message_sender.py | bot.send() | 独立业务逻辑 |
| command_executor.py | asyncio.subprocess | 独立业务逻辑（安全检查） |
| database_query.py | gsuid_core.utils.database.SQLA | 绑定数据查询 |
| favorability_manager.py | ai_core.database.AIDAL | AI好感度数据访问 |

## 工具列表

### RAG检索工具 (rag_search.py)

#### search_knowledge()
根据自然语言查询从向量数据库检索匹配的知识条目。

```python
from gsuid_core.ai_core.buildin_tools import search_knowledge

results = await search_knowledge(
    query="原神圣瞳位置",
    limit=10,
    score_threshold=0.45
)
```

**参数：**
- `query`: 自然语言查询描述
- `limit`: 最大返回结果数量，默认10条
- `score_threshold`: 相似度分数阈值，默认0.45

**返回：** 匹配的知识条目列表，每条包含 `title`、`content`、`category`、`tags`、`_score` 字段

---

#### search_knowledge_by_category()
在指定类别和插件范围内检索知识条目。

```python
from gsuid_core.ai_core.buildin_tools import search_knowledge_by_category

results = await search_knowledge_by_category(
    query="角色培养",
    category="攻略",
    plugin="Genshin",
    limit=10
)
```

**参数：**
- `query`: 自然语言查询描述
- `category`: 知识类别，如"攻略"、"角色介绍"
- `plugin`: 可选，限定插件来源
- `limit`: 最大返回结果数量，默认10条
- `score_threshold`: 相似度分数阈值，默认0.4

**返回：** 匹配的知识条目列表

---

### Web搜索工具 (web_search.py)

#### web_search()
使用 Tavily API 进行 web 搜索。

```python
from gsuid_core.ai_core.buildin_tools import web_search

results = await web_search(
    query="原神 4.0 更新内容",
    limit=10
)
```

**参数：**
- `query`: 搜索查询关键词
- `limit`: 最大返回结果数量，默认10条

**返回：** 搜索结果列表，每条包含 `title`、`url`、`content`、`score` 字段

**底层实现：** 直接引用 `ai_core.web_search.tavily_search`

---

### 消息发送工具 (message_sender.py)

#### send_text_message()
向用户发送文本消息。

```python
from gsuid_core.ai_core.buildin_tools import send_text_message

result = await send_text_message(
    ctx,
    text="你好！这是一条主动消息。"
)
```

**参数：**
- `ctx`: 工具执行上下文（包含 `bot` 和 `ev` 对象）
- `text`: 要发送的文本内容
- `user_id`: 可选，目标用户ID

**返回：** 发送结果描述字符串

---

#### send_image_message()
向用户发送图片消息。

```python
from gsuid_core.ai_core.buildin_tools import send_image_message

result = await send_image_message(
    ctx,
    image_id="res_abc123",
    text="这是你要的图片！"
)
```

**参数：**
- `ctx`: 工具执行上下文
- `image_id`: 图片资源ID，格式为 "res_xxxxxx"
- `text`: 可选，附带文字说明
- `user_id`: 可选，目标用户ID

**返回：** 发送结果描述字符串

---

### 命令执行工具 (command_executor.py)

#### execute_shell_command()
在服务器上执行系统命令。

```python
from gsuid_core.ai_core.buildin_tools import execute_shell_command

result = await execute_shell_command(
    ctx,
    command="ls -la /tmp",
    timeout=30
)
```

**参数：**
- `ctx`: 工具执行上下文
- `command`: 要执行的命令
- `timeout`: 执行超时时间（秒），默认30秒
- `use_shlex`: 是否使用shlex分割命令防止注入，默认True

**返回：** 命令执行结果字符串

**安全说明：** 内部包含危险命令模式检测，会拒绝执行包含 `rm -rf /` 等危险操作的命令。

---

### 数据库查询工具 (database_query.py)

#### query_user_favorability()
查询用户的好感度信息。

```python
from gsuid_core.ai_core.buildin_tools import query_user_favorability

result = await query_user_favorability(ctx)
# 或指定用户ID
result = await query_user_favorability(ctx, user_id="123456")
```

**参数：**
- `ctx`: 工具执行上下文
- `user_id`: 可选，指定用户ID

**返回：** 用户好感度信息字符串，包含好感度值和关系描述

**好感度与关系映射：**
| 好感度范围 | 关系描述 |
|------------|----------|
| < 0 | 厌恶 |
| 0 | 陌生 |
| 1-49 | 认识 |
| 50-79 | 熟人 |
| 80-99 | 朋友 |
| >= 100 | 挚友 |

---

### 好感度管理工具 (favorability_manager.py)

#### update_user_favorability()
增量更新用户好感度。

```python
from gsuid_core.ai_core.buildin_tools import update_user_favorability

result = await update_user_favorability(ctx, delta=5)
# delta: 好感度变化值，正数增加，负数减少
```

**参数：**
- `ctx`: 工具执行上下文
- `delta`: 好感度变化值
- `user_id`: 可选，指定用户ID

**返回：** 操作结果描述字符串

**底层实现：** 直接引用 `ai_core.database.AIDAL`

---

#### set_user_favorability()
设置用户好感度为绝对值。

```python
from gsuid_core.ai_core.buildin_tools import set_user_favorability

result = await set_user_favorability(ctx, value=50)
```

**参数：**
- `ctx`: 工具执行上下文
- `value`: 目标好感度值
- `user_id`: 可选，指定用户ID

**返回：** 操作结果描述字符串

---

## 上下文对象

所有工具函数的第一个参数都是 `ToolContext`，包含：

```python
@dataclass
class ToolContext:
    bot: Optional[Bot] = None   # Bot对象，用于发送消息
    ev: Optional[Event] = None  # 事件对象，包含用户信息
```

## 使用示例

### AI主动发送消息

```python
# AI根据用户状态主动发送提醒
await send_text_message(ctx, "检测到你已经在线很久了，注意休息！")
```

### AI检索知识后回复

```python
# AI检索相关知识后整合到回复中
knowledge = await search_knowledge("角色养成攻略")
if knowledge:
    await send_text_message(ctx, f"根据资料：{knowledge[0]['content'][:100]}...")
```

### AI查询并更新好感度

```python
# AI根据对话内容调整用户好感度
await update_user_favorability(ctx, delta=2)  # 对话愉快，增加好感度
favorability_info = await query_user_favorability(ctx)
```

### AI进行Web搜索

```python
# AI搜索最新信息
search_results = await web_search("今日新闻")
if search_results:
    await send_text_message(ctx, f"最新消息：{search_results[0]['title']}")
```

## 注意事项

1. **RAG检索**：需要先在 `ai_core/rag` 模块初始化 Embedding 模型和 Qdrant 向量库
2. **消息发送**：需要 Bot 对象可用才能发送消息
3. **命令执行**：高风险操作，已内置安全检测，实际部署建议配合权限验证
4. **数据库查询**：使用 `gsuid_core.utils.database.SQLA` 查询绑定数据
5. **好感度管理**：使用 `ai_core.database.AIDAL` 查询和更新AI好感度数据
