# 二、`@ai_tools` 装饰器

## 2.1 函数签名

```python
@overload
def ai_tools(func: F, /) -> F: ...

@overload
def ai_tools(
    func: None = None,
    /,
    *,
    category: str = "default",
    check_func: Optional[CheckFunc] = None,
    context_tags: Optional[List[str]] = None,
    capability_domain: Optional[str] = None,
    visible_when: Optional[Callable[..., Union[bool, Awaitable[bool]]]] = None,
    timeout: Optional[float] = 300.0,
    approval: Optional[str] = None,
    **check_kwargs,
) -> Callable[[F], F]: ...
```

## 2.2 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `category` | `str` | `"default"` | 工具分类，决定工具放入哪个分类字典。`"self"` 为主Agent核心工具，`"buildin"` 为内置工具，`"common"` 为通用工具，`"default"` 为子Agent工具。详见 [§3 工具分类系统](./03-tool-categories.md) |
| `check_func` | `Callable` | `None` | 可选的权限校验函数，签名为 `async def check(ev: Event) -> Tuple[bool, str]` |
| `context_tags` | `List[str]` | `None` | 语境标签。声明后，框架会在匹配该语境的群聊中通过**语境工具池**自动加载本工具，无需依赖向量搜索命中 |
| `capability_domain` | `str` | `None` | **（C3-d 新增）** 能力域名称（如 `"原神数据"`）。声明后框架会按 domain 聚合成自然语言能力清单，注入 Bot 的自我认知；未声明时按 `category` 兜底。同时它也是一个可整族挂载的**工具能力族**名（AgentNode 的 `tool_packs` 可按此名整族装配） |
| `visible_when` | `Callable` | `None` | 可见性谓词：每 step 求值，返回 False 时该工具 schema 不下发给模型（源头减噪）。必须是廉价内存判定 |
| `timeout` | `float` | `300.0` | 工具单次执行超时秒数；超时返回错误字符串，Agent 可继续 |
| `approval` | `str` | `None` | **强制审批级别**（`"user"` / `"master"`）。声明后每次调用先过统一审批中心策略门：无有效放行时自动提交审批并拦截，批准后重新调用即执行——不依赖 LLM 自觉。`"user"` 级可被「完全访问」豁免（照常留审计记录）；`"master"` 级永不可豁免。详见 [§7.10 审批与授权](./07-builtin-tools.md#710-审批与授权统一审批中心) |
| `**check_kwargs` | `Any` | — | 额外传递给 `check_func` 的参数 |

> **语境工具池**：插件可通过 `context_tags` 声明工具的适用语境，例如：
> ```python
> @ai_tools(category="genshin", context_tags=["原神", "Genshin", "游戏"])
> async def get_genshin_characters(ctx: RunContext[ToolContext], user_id: str) -> str:
>     """获取指定用户的原神角色列表及练度信息"""
>     ...
> ```
> 当框架通过群组画像判定当前群聊语境为"原神"时，该群内所有声明了 `原神` 标签的工具会被自动加入工具列表（最多 8 个），解决"群里问游戏问题但向量搜索命中不到游戏工具"的问题。语境标签由记忆系统在摄入群组对话时自动维护，无需人工配置。

## 2.3 被装饰函数要求

被装饰的函数**必须是异步函数**，第一个参数支持三种上下文模式：

| 上下文模式 | 函数签名示例 | 说明 |
|------------|-------------|------|
| RunContext | `async def tool(ctx: RunContext[ToolContext], ...)` | 推荐方式，通过 `ctx.deps.bot` 和 `ctx.deps.ev` 访问上下文 |
| ToolContext | `async def tool(ctx: ToolContext, ...)` | 直接使用上下文对象 |
| 无上下文 | `async def tool(param1: str, ...)` | 简单工具，不需要上下文 |

> **特殊注入**：函数参数如果类型注解为 `Bot` 或 `Event`，会被自动注入，**不会暴露给 LLM**。

```python
from pydantic_ai import RunContext
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

# 模式一：RunContext（推荐）
@ai_tools(category="default")
async def my_tool_v1(ctx: RunContext[ToolContext], query: str) -> str:
    """工具描述，AI 会看到这段文档"""
    bot = ctx.deps.bot   # Bot 对象
    ev = ctx.deps.ev     # Event 对象
    return f"查询结果: {query}"

# 模式二：ToolContext
@ai_tools(category="default")
async def my_tool_v2(ctx: ToolContext, query: str) -> str:
    """工具描述"""
    return f"查询结果: {query}"

# 模式三：无上下文
@ai_tools(category="default")
async def my_tool_v3(query: str) -> str:
    """工具描述"""
    return f"查询结果: {query}"

# 模式四：自动注入 Bot/Event（不暴露给 LLM）
@ai_tools(category="default")
async def my_tool_v4(query: str, ev: Event) -> str:
    """工具描述"""
    return f"查询者: {ev.user_id}, 查询结果: {query}"
```

## 2.4 使用方式

支持两种调用方式：

```python
# 方式一：直接装饰（使用默认参数）
@ai_tools
async def my_tool(query: str) -> str:
    """工具描述"""
    return query

# 方式二：带参数装饰
@ai_tools(category="common", check_func=my_check_func)
async def my_tool(ctx: RunContext[ToolContext], query: str) -> str:
    """工具描述"""
    return query
```

## 2.5 `check_func` 权限校验

`check_func` 参数会根据类型注解**自动注入** `Bot`/`Event` 对象：

```python
from gsuid_core.models import Event
from gsuid_core.bot import Bot

# 同步 check_func
def check_admin(ev: Event) -> tuple[bool, str]:
    if ev.user_id in ADMIN_LIST:
        return True, ""
    return False, "⚠️ 权限不足：仅管理员可用"

# 异步 check_func
async def check_level(bot: Bot, ev: Event) -> tuple[bool, str]:
    level = await get_user_level(ev.user_id)
    if level >= 10:
        return True, ""
    return False, f"⚠️ 等级不足：需要10级，当前{level}级"

# 使用 check_func
@ai_tools(check_func=check_admin)
async def admin_tool(ctx: RunContext[ToolContext], uid: str) -> str:
    """管理员专用工具"""
    return f"查询用户: {uid}"
```

`CheckFunc` 类型定义见 [§10.7 `CheckFunc`](./10-registry-and-types.md)。

## 2.6 返回值类型

| 返回类型 | 处理方式 |
|----------|----------|
| `str` | 直接返回给 AI |
| `Message` | 调用 `bot.send()` 发送，并返回描述字符串 |
| `dict` | JSON 序列化后返回给 AI |
| `Image.Image` | 转换为图片并发送，返回资源ID |
| `bytes` | 作为资源发送 |
