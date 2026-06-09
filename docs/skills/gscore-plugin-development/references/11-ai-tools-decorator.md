# 十一、AI 集成：`@ai_tools` 装饰器

> **⚠️ 与 `to_ai` 冲突，不可共存**：`@ai_tools` 和触发器的 `to_ai` 参数功能等价，**对同一函数只能选其一**。
> - **大多数场景应优先用 `to_ai`**（§十）：只要该函数同时是用户命令，就用 `@sv.on_xxx(..., to_ai="...")`，不要额外加 `@ai_tools`。
> - **仅当函数只允许 AI 调用、不暴露为用户命令时**，才用 `@ai_tools`——例如纯数据查询接口、不返回图片的计算工具、无需用户触发的辅助函数。

当触发器的 `to_ai` 桥接不够用（例如你需要一个纯数据查询接口、不返回图片），用 `@ai_tools` 直接注册工具函数。

## 11.1 四种函数模式

```python
from pydantic_ai import RunContext
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.bot import Bot
from gsuid_core.models import Event

# 模式一：RunContext（推荐，可同时访问 bot 和 ev）
@ai_tools(category="default")
async def query_char_data(
    ctx: RunContext[ToolContext],
    char_name: str,
    uid: str,
) -> str:
    """
    查询游戏角色的基础属性数据（文本格式）。

    Args:
        char_name: 角色名称
        uid: 游戏 UID
    """
    bot = ctx.deps.bot
    ev = ctx.deps.ev
    data = await fetch_char_data(uid, char_name)
    return f"【{char_name}】攻击: {data['atk']}  暴击: {data['crit']}"


# 模式二：自动注入 Event/Bot（不暴露给 LLM，LLM 不需要填这些参数）
@ai_tools
async def get_my_uid(ev: Event) -> str:
    """获取当前用户绑定的游戏 UID。无需任何参数。"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if bind is None:
        return "您还没有绑定 UID"
    return f"您绑定的 UID：{bind.uid}"


# 模式三：无上下文（纯计算型工具）
@ai_tools(category="default")
async def calc_damage(
    atk: float,
    crit_rate: float,
    crit_dmg: float,
    multiplier: float = 1.0,
) -> str:
    """
    计算期望伤害。

    Args:
        atk: 攻击力
        crit_rate: 暴击率（0~1，如 0.7 表示 70%）
        crit_dmg: 暴击伤害（如 1.5 表示 150%）
        multiplier: 技能倍率，默认 1.0
    """
    expected = atk * multiplier * (1 + crit_rate * crit_dmg)
    return f"期望伤害：{expected:.0f}"
```

## 11.2 category 分类规则

| 分类 | 谁能调用 | 使用场景 |
|------|---------|---------|
| `"common"` | 主 Agent 直接调用 | 高频核心功能，主 Agent 直接可见 |
| `"default"` | 子 Agent（通过 `create_subagent`） | 复杂计算、文件操作等子任务 |
| `"<自定义>"` | 根据配置 | 插件专属分类 |

**主 Agent 工具越多 Token 消耗越大**，常用功能才放 `"common"`，其余放 `"default"` 或自定义分类（一般都放default）。

## 11.3 check_func 权限校验

```python
from gsuid_core.models import Event

async def check_bound(ev: Event) -> tuple[bool, str]:
    """校验用户是否已绑定账号"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    if bind is not None:
        return True, ""
    return False, "⚠️ 请先绑定账号：发送 '绑定 您的UID'"

def check_admin(ev: Event) -> tuple[bool, str]:
    """同步校验函数也支持"""
    ADMIN_LIST = ["123456789"]
    if ev.user_id in ADMIN_LIST:
        return True, ""
    return False, "⚠️ 此工具仅管理员可用"

# 使用 check_func：校验失败时不执行函数，直接返回错误消息给 AI
@ai_tools(category="common", check_func=check_bound)
async def query_my_data(ev: Event) -> str:
    """查询我的游戏数据（需要先绑定）"""
    bind = await GameBind.get_bind(ev.user_id, ev.bot_id)
    return f"UID: {bind.uid}"  # type: ignore[union-attr]  # check_func 已保证 bind 非 None
```

## 11.4 工具 docstring 规范

AI 工具的 docstring 是 AI 判断"是否调用"以及"如何传参"的依据，**必须清晰**：

```python
@ai_tools(category="common")
async def search_game_data(
    ctx: RunContext[ToolContext],
    query: str,
    category: str = "all",
    limit: int = 5,
) -> str:
    """
    搜索游戏内的数据（角色、装备、副本等）。
    当用户询问游戏相关信息但不知道具体名称时调用。

    Args:
        query: 搜索关键词，例如 "雷元素长枪角色" 或 "高暴击圣遗物套装"
        category: 搜索类别，可选 "character"/"weapon"/"artifact"/"all"，默认 "all"
        limit: 返回结果数量，默认 5，最大 20

    Returns:
        匹配结果的文本列表
    """
    ...
```
