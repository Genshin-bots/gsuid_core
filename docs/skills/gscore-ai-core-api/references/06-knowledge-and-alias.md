# 六、知识库、别名与图片实体注册

## 6.1 `ai_entity` — 插件知识注册

**入口**：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint
```

**函数签名**：

```python
def ai_entity(entity: Union[KnowledgePoint, KnowledgeBase]) -> None
```

**`KnowledgePoint` 字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `str` | 是 | 全局唯一标识符，建议格式：`{plugin}_{类型}_{编号}` |
| `plugin` | `str` | 是 | 插件名称（在 `plugins/` 下会自动推断） |
| `title` | `str` | 是 | 知识点标题，用于 RAG 检索 |
| `content` | `str` | 是 | 知识点内容，支持 Markdown，内容越详细越好 |
| `tags` | `List[str]` | 是 | 标签列表，用于过滤和检索 |
| `source` | `str` | 自动 | 固定为 `"plugin"`，系统自动设置 |
| `_hash` | `str` | 自动 | 内容哈希，系统自动计算，不需传入 |

`KnowledgePoint` / `KnowledgeBase` 完整定义见 [§10.3-10.4 类型定义](./10-registry-and-types.md)。

**示例**：

```python
from gsuid_core.ai_core.register import ai_entity
from gsuid_core.ai_core.models import KnowledgePoint

ai_entity(KnowledgePoint(
    id="genshin_character_shogun",
    plugin="GenshinUID",
    title="雷电将军 - 角色介绍",
    content="""
# 雷电将军

## 基本信息
- 元素：雷
- 武器类型：长枪
- 命之座：万世之座
- 稀有度：5星

## 技能说明
### 普通攻击 - 源流
进行五段枪类普通攻击。

### 元素战技 - 奥义·梦想真说
创造「愿力」储蓄机制，并召唤眼之核心。

## 适用阵容
雷电将军在超导、感电等队伍中效果优异。
""",
    tags=["原神", "雷电将军", "雷神", "角色", "长枪", "雷元素"],
))
```

**注意**：
- 启动时自动同步到向量数据库
- 内容发生变化时（通过 `_hash` 检测）自动增量更新
- 在 `plugins/` 目录下注册时 `plugin` 字段会被自动推断

## 6.2 `add_manual_knowledge` — 手动知识添加

**入口**：

```python
from gsuid_core.ai_core.register import add_manual_knowledge
from gsuid_core.ai_core.models import ManualKnowledgeBase
```

**函数签名**：

```python
def add_manual_knowledge(entity: ManualKnowledgeBase) -> bool
```

**返回**: `True` 表示添加成功，`False` 表示 ID 已存在

**示例**：

```python
success = add_manual_knowledge(ManualKnowledgeBase(
    id="faq_bind_account",
    plugin="custom",
    title="如何绑定账号",
    content="Q: 如何绑定游戏账号？\nA: 发送 '绑定 你的UID' 即可完成绑定。",
    tags=["FAQ", "绑定", "账号"],
    source="manual",
))
```

## 6.3 手动知识管理 API

| 函数 | 签名 | 说明 |
|------|------|------|
| `add_manual_knowledge` | `(entity: ManualKnowledgeBase) -> bool` | 添加，ID 已存在返回 `False` |
| `update_manual_knowledge` | `(entity_id: str, updates: dict) -> bool` | 更新指定字段（不能修改 `id`、`source`） |
| `delete_manual_knowledge` | `(entity_id: str) -> bool` | 删除，不存在返回 `False` |
| `get_manual_entities` | `() -> List[ManualKnowledgeBase]` | 获取所有手动知识（副本） |
| `get_manual_entity` | `(entity_id: str) -> Optional[ManualKnowledgeBase]` | 获取指定知识 |

**与 `ai_entity` 的区别**：

| 特性 | `ai_entity` | `add_manual_knowledge` |
|------|-------------|----------------------|
| 启动同步 | ✅ 自动同步 | ❌ 不自动同步 |
| 增量更新 | ✅ 自动检测 | ❌ 手动管理 |
| 适用场景 | 插件固定知识 | 前端 API 动态添加 |
| source 字段 | `"plugin"` | `"manual"` |

## 6.4 `ai_alias` — 别名注册

### 入口

```python
from gsuid_core.ai_core.register import ai_alias
```

### 函数签名

```python
def ai_alias(name: str, alias: Union[str, List[str]], scope: str = "global") -> None
```

别名系统用于专有名词归一化。

> **C2 变更（2026-05-19）**：别名注册表已**接入 AI 记忆摄入链路**。注册的别名会在
> 实体抽取时作为"本群已知别名"注入提取提示词，指导 LLM 把别名对齐到正式名（C2-a/c）；
> 检索期也用于查询展开与动态实体链接消歧（C2-e）。原先 `ai_alias` 的唯一消费者
> `normalize_query` 是 dead code，现已降级为命令层非 LLM fallback（R1），
> 不再参与 AI 推理链路。

### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | — | 标准名称（归一化目标） |
| `alias` | `str \| List[str]` | — | 别名，可以是单个字符串或列表 |
| `scope` | `str` | `"global"` | **（C2-d 新增）** 别名作用域。默认 `"global"` 为通用别名；插件可传业务 scope（如 `"Genshin"`）隔离同名别名，避免"深渊"等词在不同游戏间串味 |

### 示例

```python
from gsuid_core.ai_core.register import ai_alias

# 单个别名
ai_alias("雷电将军", "雷神")

# 多个别名
ai_alias("胡桃", ["小胡桃", "HuTao", "胡桃儿"])
ai_alias("丝柯克", ["skk", "斯柯克", "SKK", "丝绸之路"])

# C2-d：用 scope 隔离跨游戏同名别名
ai_alias("幽境危战", "深渊", scope="WutheringWaves")
ai_alias("渊月螺旋", "深渊", scope="Genshin")

# 在插件初始化时批量注册
ALIASES = {
    "雷电将军": ["雷神", "将军", "影"],
    "纳西妲": ["草神", "小草神", "Lesser Lord Kusanali"],
}

for name, aliases in ALIASES.items():
    ai_alias(name, aliases)
```

## 6.5 `ai_image` — 图片实体注册

### 入口

```python
from gsuid_core.ai_core.register import ai_image
from gsuid_core.ai_core.models import ImageEntity
```

### 函数签名

```python
def ai_image(entity: ImageEntity) -> None
```

### `ImageEntity` 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | `str` | 是 | 唯一标识符 |
| `plugin` | `str` | 是 | 插件名称 |
| `path` | `str` | 是 | 图片路径（绝对路径或相对路径） |
| `tags` | `List[str]` | 是 | 描述标签，用于语义检索 |
| `content` | `str` | 是 | 详细描述文本 |
| `source` | `str` | 自动 | 固定为 `"plugin"` |
| `_hash` | `str` | 自动 | 内容哈希，传入空字符串即可 |

`ImageEntity` 完整定义见 [§10.5 `ImageEntity`](./10-registry-and-types.md)。

### 示例

```python
from gsuid_core.ai_core.register import ai_image
from gsuid_core.ai_core.models import ImageEntity

ai_image(ImageEntity(
    id="genshin_hutao_illustration",
    plugin="GenshinUID",
    path="./resources/characters/hutao.png",
    tags=["胡桃", "原神", "角色立绘", "火元素"],
    content="胡桃角色立绘图片，往生堂第七十七代堂主，性格活泼爱捉弄人。",
    source="plugin",
    _hash="",
))
```

### 图片检索使用

注册图片后，AI 可以通过 RAG API 进行语义检索：

```python
from gsuid_core.ai_core.rag import search_and_load_image

# 在插件命令处理中使用
async def show_character_image(bot, ev):
    image = await search_and_load_image("给我看看胡桃的图片")
    if image:
        await bot.send(image)
```
