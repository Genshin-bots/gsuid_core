# 十二、AI 集成：知识库与别名注册

## 12.1 注册知识库（`ai_entity`）

让 AI 在 RAG 检索时能找到插件相关的静态知识（命令说明、游戏数据等）：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint

# 在模块加载时调用，自动在启动时同步到向量数据库
ai_entity(KnowledgePoint(
    id="myplugin_commands",           # 全局唯一 ID，建议 {plugin}_{类型}_{编号}
    plugin="MyPlugin",
    title="MyPlugin 命令使用指南",
    content="""
# MyPlugin 使用指南

## 命令列表
- `查角色 <角色名>` — 查询角色培养详情，需要先绑定 UID
- `绑定 <UID>` — 绑定游戏账号
- `我的角色` — 查看全部角色列表
- `帮助` — 显示此帮助

## 注意事项
1. 所有查询功能需要先绑定账号
2. 每日查询上限为 100 次
3. 支持的游戏区域：cn（国服）、os（国际服）
""",
    tags=["MyPlugin", "帮助", "命令", "使用说明"],
))

ai_entity(KnowledgePoint(
    id="myplugin_genshin_shogun",
    plugin="MyPlugin",
    title="雷电将军 - 角色培养建议",
    content="""
# 雷电将军培养建议

## 推荐圣遗物
- 绝缘之旗印（4件套）：充能转化攻击，爆发效果极强

## 推荐武器
- 薙草之稻光（5星长枪）：充能提升+技能倍率加成

## 属性优先级
充能 160%+ → 暴击率 70%+ → 暴击伤害 → 攻击力
""",
    tags=["雷电将军", "雷神", "角色", "培养", "原神", "MyPlugin"],
))
```

**注意**：`id` 字段变化会触发重新索引，`content` 变化会通过 `_hash` 检测自动增量更新。

## 12.2 注册别名（`ai_alias`）

让 AI 在解析用户意图时进行专有名词归一化：

```python
from gsuid_core.ai_core.register import ai_alias

# 在模块级别调用（导入时即执行）
ai_alias("雷电将军", ["雷神", "将军", "影", "Raiden", "shogun"])
ai_alias("纳西妲", ["草神", "小草神", "Lesser Lord Kusanali"])
ai_alias("胡桃", ["小胡桃", "HuTao", "胡桃儿", "往生堂堂主"])

# 批量注册
GAME_ALIASES: dict[str, list[str]] = {
    "雷电将军": ["雷神", "将军"],
    "钟离": ["岩神", "摩拉克斯"],
    "万叶": ["楓原万叶", "枫原万叶"],
}
for name, aliases in GAME_ALIASES.items():
    ai_alias(name, aliases)
```
