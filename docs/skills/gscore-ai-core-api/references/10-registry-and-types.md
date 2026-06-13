# 十、工具注册表查询 API + 全部类型定义

## 10.1 `get_registered_tools` / `get_all_tools`

```python
from gsuid_core.ai_core.register import get_registered_tools, get_all_tools

# 获取按分类组织的工具字典
# 返回: Dict[str, Dict[str, ToolBase]]
all_by_category = get_registered_tools()
# {
#   "self": {"query_user_favorability": ToolBase(...), ...},
#   "buildin": {"search_knowledge": ToolBase(...), ...},
#   "buildin": {"get_self_persona_info": ToolBase(...), ...},
#   "default": {"execute_shell_command": ToolBase(...), ...},
# }

# 查看某分类的工具
self_tools = all_by_category.get("self", {})
for name, tool_base in self_tools.items():
    print(f"{name}: {tool_base.description}")

# 获取平铺结构（所有分类合并）
# 返回: Dict[str, ToolBase]
all_flat = get_all_tools()
for name, tool_base in all_flat.items():
    print(f"{name} (plugin={tool_base.plugin})")
```

## 10.2 `ToolBase` 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 工具函数名 |
| `description` | `str` | 工具描述（来自函数 `__doc__`） |
| `plugin` | `str` | 工具来源（在 `plugins/` 下自动推断，核心工具为 `"core"`） |
| `tool` | `Tool[ToolContext]` | PydanticAI Tool 对象 |

---

## 10.3 `ToolContext`

```python
@dataclass
class ToolContext:
    """工具执行上下文"""
    bot: Optional[Bot] = None   # Bot 实例，用于发送消息
    ev: Optional[Event] = None  # 事件实例，包含用户ID、群组ID等
    extra: Dict[str, Any] = field(default_factory=dict)  # 工具间临时传递状态
```

**访问方式**：

```python
# 通过 RunContext
async def my_tool(ctx: RunContext[ToolContext], ...) -> str:
    bot = ctx.deps.bot
    ev = ctx.deps.ev
    user_id = ev.user_id if ev else None
    group_id = ev.group_id if ev else None

# 通过 ToolContext
async def my_tool(ctx: ToolContext, ...) -> str:
    bot = ctx.bot
    ev = ctx.ev
```

## 10.4 `KnowledgeBase`

```python
class KnowledgeBase(TypedDict):
    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # "plugin" 或 "manual"
```

## 10.5 `KnowledgePoint`

```python
class KnowledgePoint(KnowledgeBase):
    _hash: str  # 自动计算的内容哈希
```

## 10.6 `ManualKnowledgeBase`

```python
class ManualKnowledgeBase(TypedDict):
    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # 固定为 "manual"
```

## 10.7 `ImageEntity`

```python
class ImageEntity(TypedDict):
    id: str               # 唯一标识符
    plugin: str           # 插件名称
    path: str             # 图片文件路径
    tags: List[str]       # 描述标签
    content: str          # 详细描述文本
    source: str           # "plugin"
    _hash: Optional[str]  # 内容哈希
```

## 10.8 `ToolBase`

```python
class ToolBase:
    name: str                    # 工具名
    description: str             # 工具描述
    plugin: str                  # 所属插件（"core" 或插件名）
    tool: Tool[ToolContext]       # PydanticAI Tool 对象
```

## 10.9 `CheckFunc` 类型

```python
# 支持同步和异步
CheckFunc = Callable[..., Union[
    Tuple[bool, str],
    Awaitable[Tuple[bool, str]],
]]

# 返回值含义
# (True, "")        -> 校验通过
# (False, "原因")   -> 校验失败，"原因" 作为工具返回值告知 AI
```

`check_func` 的使用模式（同步/异步、自动注入 `Bot`/`Event`）见 [§2.5 `check_func` 权限校验](./02-ai-tools-decorator.md)。
