# 十三、AI 集成：`create_agent`

用于在触发器内部创建一个**临时的专用 AI Agent**，执行特定子任务（如文本分析、翻译、摘要）：

```python
from gsuid_core.ai_core.gs_agent import create_agent

# 模块级别创建（复用 Agent 实例）
summarizer = create_agent(
    system_prompt="""你是一个文本摘要专家。
将用户提供的文本压缩为不超过 100 字的摘要，保留核心信息，输出中文。
直接输出摘要，不加任何说明。""",
    max_tokens=500,
)

translator = create_agent(
    system_prompt="你是一个翻译助手，只负责将输入翻译为中文，不做解释。",
    max_tokens=1000,
)

# 在触发器中调用
@sv.on_command("摘要")
async def summarize_cmd(bot: Bot, ev: Event) -> None:
    text = ev.text.strip()
    if not text:
        return await bot.send("请在命令后提供要摘要的文本")
    result = await summarizer.run(user_message=text)
    await bot.send(f"摘要：\n{result}")

# 带结构化输出
from pydantic import BaseModel

class CharAnalysis(BaseModel):
    name: str
    element: str
    recommended: bool
    reason: str

char_analyzer = create_agent(
    system_prompt="你是原神角色分析专家，根据用户描述给出角色评价。"
)

@sv.on_command("分析角色")
async def analyze_char(bot: Bot, ev: Event) -> None:
    char_name = ev.text.strip()
    result: CharAnalysis = await char_analyzer.run(
        user_message=f"分析角色：{char_name}",
        bot=bot,
        ev=ev,
        output_type=CharAnalysis,  # 强制结构化输出
    )
    await bot.send(
        f"角色：{result.name}\n"
        f"元素：{result.element}\n"
        f"推荐：{'✅' if result.recommended else '❌'}\n"
        f"理由：{result.reason}"
    )
```
