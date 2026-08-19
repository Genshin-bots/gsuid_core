# Buildin Tools 模块文档

系统内建AI元工具模块，提供自主型AI常用的基础工具函数。

此模块是 Facade 模式，引用 `ai_core.rag`、`ai_core.web_search`、`ai_core.database` 等底层模块的实现。
核心逻辑在对应的底层模块中。

## 文件结构

```
buildin_tools/
├── __init__.py              # 模块导出（Facade接口）
├── rag_search.py            # RAG检索工具（引用 ai_core.rag.query_knowledge）
├── web_search.py            # Web搜索工具（引用 ai_core.web_search.web_search，多源调度）
├── web_fetch.py             # 网页抓取工具（引用 ai_core.web_fetch，Jina/local 多源）
├── message_sender.py        # 消息发送工具
├── command_executor.py      # 命令执行工具
├── favorability_manager.py  # 好感度管理工具（仅主人绝对值覆盖）
├── file_manager.py          # 文件管理工具（沙盒内读写执行文件）
├── file_operations.py       # 文件操作工具（artifacts 路径内移动/复制/打包 zip）
├── get_time.py              # 日期时间工具
├── html_render_tools.py     # HTML/Markdown 渲染为图片
├── image_reader.py          # 图片读取工具（按图片ID取回并转述为文字）
├── avatar_tools.py          # 用户头像工具（按用户ID取头像，返回RM图片ID）
├── meme_tools.py            # 表情包工具（发送/收藏/搜索）
├── scheduler.py             # 定时任务管理工具
├── self_info.py             # 自我认知信息工具
├── subagent.py              # 子Agent派生工具
├── dynamic_tool_discovery.py # 动态工具发现
└── README.md                # 使用文档
```

## 架构说明

`buildin_tools` 采用 Facade 模式，提供统一的工具接口：

| 工具模块 | 底层依赖 | 说明 |
|---------|---------|------|
| rag_search.py | ai_core.rag.query_knowledge | 知识库检索封装 |
| web_search.py | `ai_core.web_search.web_search` | Web 搜索封装（Jina/Tavily/Exa/MCP + 多源策略） |
| web_fetch.py | `ai_core.web_fetch.fetch_webpage_as_markdown` | 网页抓取转 Markdown（Jina Reader / local） |
| message_sender.py | bot.send() | 独立业务逻辑 |
| command_executor.py | asyncio.subprocess | 独立业务逻辑（安全检查） |
| favorability_manager.py | relationship.engine | 仅主人绝对值覆盖；增量由框架结算 |
| file_manager.py | pathlib / asyncio | 沙盒文件读写执行 |
| file_operations.py | shutil / zipfile | artifacts 路径内文件移动/复制/打包 zip |
| get_time.py | datetime | 日期时间获取 |
| html_render_tools.py | pytakumi / playwright | HTML/Markdown 渲染为图片 |
| image_reader.py | ai_core.image_understand + RM | 按图片ID取回图片并转述为文字 |
| avatar_tools.py | utils.image.image_tools + RM | 按用户ID取头像，注册RM返回图片ID |
| meme_tools.py | ai_core.meme | 表情包发送/收藏/搜索 |
| scheduler.py | APScheduler | 定时任务管理 |
| self_info.py | ai_core.persona | 自我认知信息查询 |
| subagent.py | ai_core.gs_agent | 子Agent派生 |
| dynamic_tool_discovery.py | _TOOL_REGISTRY | 动态工具搜索发现 |

## 工具列表

### RAG检索工具 (rag_search.py)

#### search_cognition()
主人格唯一「回想」动词：记忆 / 偏好 / 知识库 / 落盘 / 产物一次联邦检索。

```python
from gsuid_core.ai_core.buildin_tools import search_cognition

results = await search_cognition(ctx, query="已有资料里的入门说明")
```

**参数：**
- `query`: 自然语言查询
- `kinds`: 可选，逗号分隔 kind 过滤；留空=全查
- `limit`: 最大条数

旧名 `search_knowledge` 已删除，不要再注册或调用。

---

### Web搜索工具 (web_search.py)

#### `web_search_tool`（Agent 工具）
统一 Web 搜索：底层走 `ai_core.web_search.web_search`，按配置在 **AnySearch（默认主用，可匿名）/ Tavily / Jina / Exa / MCP** 间调度。

```python
from gsuid_core.ai_core.buildin_tools.web_search import web_search_tool
# 或由 Agent 自动调用；插件侧更推荐：
from gsuid_core.ai_core.web_search import web_search

results = await web_search(query="原神 4.0 更新内容", max_results=10)
```

**参数（工具侧）：**
- `query`: 搜索查询关键词
- `limit`: 最大返回条数；`None` 时用 `web_search_default_limit`

**返回：** 带边界的摘要文本（空结果有明确提示）。结构化列表字段为 `title` / `url` / `content` / `score`。

**多源策略：** `websearch_lb_strategy` 默认 `error_switch`；Key 失败、抛错或 **空列表** 会切下一已配置源。
**超时：** `@ai_tools(..., timeout=100.0)`，覆盖串行 failover。

详见 `.agents/skills/gscore-ai-core-api/references/11-mcp-image-search-and-meme.md` §11.3。

### 网页抓取工具 (web_fetch.py)

#### `web_fetch_tool`
默认 **Jina Reader**（Key 可选）+ 备用 **local**；`timeout=100.0`。见 §11.3b。

---

### 图片读取工具 (image_reader.py)

#### read_image()
按图片ID取回一张图片并转述为文字内容。

群聊里图片极多，框架默认只把图片本体存进 RM 资源池、用「图片ID」(`img_xxxxxxxx`)
以文字形式透传给 Agent（不直接塞进多模态上下文，避免 Token 爆炸 / 注意力稀释）。
当 Agent 需要真正"看清"某张图时再调用本工具读取。

```python
from gsuid_core.ai_core.buildin_tools import read_image

desc = await read_image(ctx, "img_1a2b3c4d")
desc = await read_image(ctx, "img_1a2b3c4d", question="图里这串报错是什么？")
```

**参数：**
- `ctx`: 工具执行上下文
- `image_id`: 图片资源ID，支持 `img_xxxxxxxx`（用户上传图）、`res_xxxxxxxx`
  （能力代理产物）、`http(s)://` / `base64://` / `data:image/` 直链
- `question`: 可选，想从图里知道什么，传入后描述会聚焦该问题

**返回：** 图片内容的文字描述；图片不存在/已过期/非图片资源时返回中文错误说明

**底层实现：** RM 取字节 → DataURI → `ai_core.image_understand.understand_image`
（模型原生多模态优先，不支持时回退 MCP 转述模型，带 10 分钟短期缓存）。**分类
`buildin`（保底常驻）**，保证 Agent 遇到图片ID 时总能读图。

---

### 用户头像工具 (avatar_tools.py)

#### get_user_avatar()
按用户ID取回头像，注册到 RM 资源池后返回图片ID。

```python
from gsuid_core.ai_core.buildin_tools import get_user_avatar

result = await get_user_avatar(ctx)                 # 当前发言者头像
result = await get_user_avatar(ctx, user_id="123")  # 指定用户头像
```

**参数：**
- `ctx`: 工具执行上下文
- `user_id`: 可选，目标用户ID。不传则取当前发言者；指定他人时 QQ 系平台
  （onebot / qqgroup）可按任意 ID 取头像

**返回：** 含资源ID（`img_xxxxxxxx`）及后续用法提示的说明文本；取不到时返回
中文不支持/错误说明

**后续消费：** 拿到 `img_xxx` 后，用 `read_image('img_xxx')` 看清头像内容，或用
`send_message_by_ai(image_id='img_xxx')` 把头像发给用户。

**底层实现：** 复用 `utils.image.image_tools` 的 `get_event_avatar` /
`get_qq_avatar` / `get_qqgroup_avatar`，PIL → bytes → `RM.register`。分类 `common`
（向量检索按需加载）。

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

### 好感度管理工具 (favorability_manager.py)

关系温度由框架每轮结算，没有增量工具。群聊记忆请用 `search_cognition`。

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
knowledge = await search_cognition(ctx, query="入门说明")
if knowledge:
    await send_text_message(ctx, f"根据资料：{knowledge[0]['content'][:100]}...")
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
