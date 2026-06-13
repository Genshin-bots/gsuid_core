# 五、`create_agent` 与 Agent 架构

## 5.1 `create_agent` — 创建临时 Agent

```python
from gsuid_core.ai_core.gs_agent import create_agent
```

**函数签名**：

```python
def create_agent(
    system_prompt: Optional[str] = None,
    max_tokens: int = 20000,
    max_iterations: Optional[int] = None,
    persona_name: Optional[str] = None,
    create_by: str = "LLM",
    max_history: int = 20,
    task_level: Literal["high", "low"] = "high",
) -> GsCoreAIAgent
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `system_prompt` | `str` | `None` | 系统提示词 |
| `max_tokens` | `int` | `20000` | 最大输出 token 数 |
| `max_iterations` | `int` | `None` | 最大迭代次数，`None` 时使用配置默认值 |
| `persona_name` | `str` | `None` | 绑定的 Persona 名称（用于热重载检测） |
| `create_by` | `str` | `"LLM"` | 创建者标识，影响工具加载策略 |
| `max_history` | `int` | `20` | 最大历史消息数 |
| `task_level` | `str` | `"high"` | 任务级别，`"high"` 或 `"low"`，用于选择对应模型配置 |

**示例**：

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 创建翻译 Agent
translator = create_agent(
    system_prompt="你是一个翻译助手，只负责将中文翻译成英文，不做任何解释。",
)

# 在异步函数中使用
async def translate(text: str) -> str:
    result = await translator.run(user_message=text)
    return result
```

> 端到端示例（含代码审查 Agent + Pydantic 结构化输出）见 [§13 完整示例](./13-full-examples-and-faq.md)。
> 业务域能力代理注册（替代/配合 `create_agent`）见 [§7.8 Capability Agent](./07-builtin-tools.md) 与 [`gscore-plugin-development` SKILL §十四](../gscore-plugin-development/references/14-ai-capability-profile.md)。

## 5.2 `GsCoreAIAgent.run()` 方法

```python
async def run(
    self,
    user_message: Union[str, Sequence[UserContent]],
    bot: Optional[Bot] = None,
    ev: Optional[Event] = None,
    rag_context: Optional[str] = None,
    tools: Optional[ToolList] = None,
    return_mode: Literal["always", "return", "by_bot"] = "by_bot",
    output_type: Optional[type[_T]] = None,
) -> Union[str, _T]
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_message` | `str \| Sequence` | 是 | 用户输入消息 |
| `bot` | `Bot` | 否 | Bot 对象，工具调用时注入 `ctx.deps.bot` |
| `ev` | `Event` | 否 | 事件对象，工具调用时注入 `ctx.deps.ev` |
| `rag_context` | `str` | 否 | 额外的 RAG 上下文，追加到 system_prompt |
| `tools` | `ToolList` | 否 | 自定义工具列表 |
| `return_mode` | `str` | 否 | 返回模式：`"always"` 始终返回、`"return"` 仅返回不发送、`"by_bot"` 由Bot决定发送 |
| `output_type` | `type[_T]` | 否 | 指定 Pydantic 模型类时，强制结构化输出 |

**返回**: AI 响应字符串，或指定的 Pydantic 模型实例

## 5.3 `get_main_agent_tools` — 获取主Agent保底工具集

```python
from gsuid_core.ai_core.rag.tools import get_main_agent_tools

async def get_main_agent_tools(query: str = "") -> ToolList
```

返回**框架保底工具池**——即 `category="self"` 和 `category="buildin"` 两个分类下的**全部**工具，
无条件加载、不受向量搜索影响。`query` 参数已废弃保留（仅作签名兼容），保底工具不再依赖 query 筛选。

主Agent 的完整工具列表 = 保底工具池（本函数）+ 语境工具池（`get_tools_by_context_tags`）
+ 查询工具池（`search_tools`），后两者合并去重后受附加数量上限约束。

> 保底池加载机制与 category 关系详见 [§3.2 分类说明](./03-tool-categories.md)。

## 5.4 `handle_ai_chat` — AI聊天入口

```python
from gsuid_core.ai_core.handle_ai import handle_ai_chat

async def handle_ai_chat(bot: Bot, event: Event)
```

**工作流程**：
1. 双层长度防护：硬截断 + 智能摘要
2. 意图识别：使用分类器判断用户意图（闲聊/工具/问答）
3. 获取 AI Session（含 system_prompt/Persona）
4. 准备上下文（历史记录 + 记忆检索）
5. 调用 Agent 生成回复
6. 发送回复给用户
