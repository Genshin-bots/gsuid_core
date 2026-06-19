# 十三、完整示例 + 常见问题

## 13.1 完整示例

### 13.1.1 示例一：基础工具注册

```python
# my_plugin/ai_tools.py

from pydantic_ai import RunContext
from gsuid_core.models import Event
from gsuid_core.bot import Bot
from gsuid_core.ai_core.register import ai_tools, ai_entity, ai_alias
from gsuid_core.ai_core.models import ToolContext, KnowledgePoint

# ============================================================
# 1. 注册别名（插件加载时自动执行）
# ============================================================
ai_alias("雷电将军", ["雷神", "将军", "影", "屑"])
ai_alias("纳西妲", ["草神", "小草神"])

# ============================================================
# 2. 注册知识库
# ============================================================
ai_entity(KnowledgePoint(
    id="myplugin_intro",
    plugin="MyPlugin",
    title="MyPlugin 插件介绍",
    content="""
# MyPlugin 使用指南

## 命令列表
- `/query <角色名>` - 查询角色信息
- `/bind <uid>` - 绑定账号
- `/help` - 显示帮助

## 注意事项
1. 需要先绑定账号才能使用查询功能
2. 每日查询上限为100次
""",
    tags=["帮助", "命令", "MyPlugin"],
))

# ============================================================
# 3. 权限校验函数
# ============================================================
async def check_bound(ev: Event) -> tuple[bool, str]:
    """检查用户是否已绑定账号"""
    from my_plugin.database import is_user_bound
    if await is_user_bound(ev.user_id):
        return True, ""
    return False, "⚠️ 请先绑定账号：发送 /bind <你的UID>"

async def check_admin(ev: Event) -> tuple[bool, str]:
    """检查是否管理员"""
    ADMIN_IDS = ["123456789", "987654321"]
    if ev.user_id in ADMIN_IDS:
        return True, ""
    return False, "⚠️ 此工具仅管理员可用"

# ============================================================
# 4. 注册 AI 工具
# ============================================================

# 简单工具（无上下文）
@ai_tools(category="default")
async def calculate(
    expression: str,
) -> str:
    """
    计算数学表达式

    Args:
        expression: 数学表达式，如 "1+2*3"
    """
    try:
        result = eval(expression, {"__builtins__": {}})
        return f"计算结果：{expression} = {result}"
    except Exception as e:
        return f"计算失败：{str(e)}"


# 需要上下文的工具
@ai_tools(category="common", check_func=check_bound)
async def query_character(
    ctx: RunContext[ToolContext],
    character_name: str,
) -> str:
    """
    查询游戏角色详细信息

    Args:
        character_name: 角色名称，如"雷电将军"、"胡桃"
    """
    ev = ctx.deps.ev
    uid = await get_bound_uid(ev.user_id)
    data = await fetch_character_data(uid, character_name)
    return f"角色 {character_name} 的数据：\n{data}"


# 管理员工具
@ai_tools(category="common", check_func=check_admin)
async def admin_reset_cd(
    ctx: RunContext[ToolContext],
    user_id: str,
) -> str:
    """
    重置指定用户的冷却时间（管理员专用）

    Args:
        user_id: 要重置的用户ID
    """
    await reset_cooldown(user_id)
    return f"✅ 已重置用户 {user_id} 的冷却时间"


# 自动注入 Bot（不暴露给 LLM）
@ai_tools(category="default")
async def send_custom_message(
    bot: Bot,
    message: str,
) -> str:
    """
    发送自定义文本消息

    Args:
        message: 要发送的消息内容
    """
    await bot.send(message)
    return "✅ 消息已发送"
```

### 13.1.2 示例二：创建临时 Agent 做专项任务

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 创建翻译 Agent
translator = create_agent(
    system_prompt="""你是一个翻译助手，仅负责将中文翻译成英文。
规则：
1. 直接输出翻译结果，不加任何解释
2. 保持原文的语气和风格
3. 专有名词保持原样""",
)

async def translate_to_english(text: str) -> str:
    return await translator.run(user_message=text)


# 创建代码审查 Agent
code_reviewer = create_agent(
    system_prompt="""你是一个严格的代码审查专家。
请对用户提供的代码进行审查，关注：
1. 潜在的 Bug
2. 性能问题
3. 代码风格
4. 安全漏洞

输出格式：
## 审查结论
[总体评价]

## 问题列表
1. [问题描述] - [严重程度：高/中/低]

## 改进建议
[具体建议]""",
)

async def review_code(code: str, bot, ev) -> str:
    return await code_reviewer.run(
        user_message=f"请审查以下代码：\n```\n{code}\n```",
        bot=bot,
        ev=ev,
    )
```

### 13.1.3 示例三：完整插件入口文件

```python
# my_plugin/__init__.py

from gsuid_core.sv import SV
from gsuid_core.ai_core.register import ai_alias

# 注册别名（导入时自动执行）
ai_alias("我的插件", ["MyPlugin", "mp"])

# 导入 AI 工具模块（触发工具注册）
from my_plugin import ai_tools  # noqa: F401

# 注册插件命令
sv = SV("我的插件")

@sv.on_fullmatch("/help")
async def show_help(bot, ev):
    await bot.send("""
我的插件使用指南：
- /help: 显示此帮助
- /bind <uid>: 绑定账号
- /query <角色>: 查询角色

AI功能：
- 直接@机器人并描述需求，AI会自动调用相关工具
""")
```

---

## 13.2 常见问题

### Q1: 工具注册后 AI 能直接使用吗？

取决于 `category`：
- `category="self"`, `"buildin"`, `"common"`：主Agent直接可用
- `category="default"` 或其他：需通过 `create_subagent` 在子Agent中使用

详见 [§3 工具分类系统](./03-tool-categories.md)。

### Q2: 如何让插件工具被 AI 主Agent直接调用？

将 `category` 设置为 `"common"`，但要谨慎——主Agent的工具越多，token 消耗越大。推荐将高频核心工具注册为 `"common"`，其他通过子Agent完成。

### Q3: `check_func` 和工具自身的错误处理有什么区别？

- `check_func` 在工具执行**前**校验，失败时返回错误消息给 AI，工具函数**不会被执行**
- 工具函数内部的 `try/except` 处理执行过程中的异常

详见 [§2.5 `check_func` 权限校验](./02-ai-tools-decorator.md)。

### Q4: 工具函数的 docstring 有格式要求吗？

推荐使用 Google 风格的 docstring：

```python
async def my_tool(ctx: RunContext[ToolContext], param: str) -> str:
    """
    简短的工具描述（AI 将看到这行）

    详细说明工具的作用、限制和使用场景。

    Args:
        param: 参数说明（AI 会参考这里理解如何调用工具）

    Returns:
        返回值说明
    """
```

### Q5: `ai_entity` 和 `add_manual_knowledge` 什么时候同步到向量库？

- `ai_entity`：系统启动时，`rag/startup.py` 的 `init_all()` 自动同步
- `add_manual_knowledge`：不自动同步，需要手动调用向量库 API

详见 [§6 知识库、别名与图片实体注册](./06-knowledge-and-alias.md)。

### Q6: RAG 知识库检索的工作方式是什么？

RAG 知识库检索不再作为强制前置流程。`search_knowledge` 工具注册为 `buildin` 分类，主Agent会根据对话内容自主决定是否调用该工具进行知识库检索。

详见 [§7.2 `search_knowledge`](./07-builtin-tools.md)。

---

## 13.3 相关文档

- [gscore-development 框架开发指南](../../gscore-development/SKILL.md)
- [WebConsole API 文档](../../../gsuid_core/webconsole/API.md)
- [AI Agent 总架构](../../AI_AGENT_ARCHITECTURE.md)
- [插件开发工作流指南](../gscore-plugin-development/SKILL.md)
- [适配器开发指南](../gscore-adapter-development/SKILL.md)
