# Persona 模块文档

角色扮演系统模块，提供人格角色的提示词管理和资料存储功能。

## 文件结构

```
persona/
├── __init__.py      # 模块导出
├── processor.py     # 角色提示词组装器
├── prompts.py       # 提示词模板定义
├── resource.py      # 角色资料持久化
└── README.md        # 使用文档
```

## 模块职责

| 文件 | 职责 |
|------|------|
| `prompts.py` | 定义角色扮演所需的各种提示词模板 |
| `resource.py` | 管理角色资料的持久化存储（保存/加载/列表） |
| `processor.py` | 组装完整的角色提示词 |
| `__init__.py` | 模块导出 |

## 提示词模板 (prompts.py)

### CHARACTER_BUILDING_TEMPLATE
角色构建模板，用于从用户需求构建角色身份。

包含：
- 核心定义（姓名、身份、兴趣）
- 好感度逻辑（-100至100的不同行为表现）
- 语言结构约束（句式、截断逻辑、禁词表）
- 社交行为逻辑（活跃度、响应长度、信息保护）
- 工具使用协议（调用动机、前摇、后置反馈）
- 触发场景和典型回应示例

### ROLE_PLAYING_START
角色扮演开始提示词，引导AI进入角色扮演模式。

### SYSTEM_CONSTRAINTS
系统行为约束提示词，定义AI的系统级约束：

- 认知防火墙（反助手模式、能量守恒）
- 合规性检查（敏感内容处理）
- 工具调用协议
- 资源/文件处理协议
- 用户输入格式说明
- 好感度系统
- 发送消息权限
- 记忆系统

## 资源管理 (resource.py)

### persist_character_profile()
保存角色资料到本地存储。

```python
from gsuid_core.ai_core.persona.resource import persist_character_profile

await persist_character_profile("凯露", "# 凯露\n这是一个傲娇的角色...")
```

### retrieve_character_profile()
从本地存储加载角色资料。

```python
from gsuid_core.ai_core.persona.resource import retrieve_character_profile

profile = await retrieve_character_profile("凯露")
```

### list_available_characters()
列出所有可用的角色名称。

```python
from gsuid_core.ai_core.persona.resource import list_available_characters

characters = list_available_characters()
# ['凯露', '帕拉斯', ...]
```

## 提示词组装 (processor.py)

### assemble_persona_prompt()
组装完整的角色提示词。

```python
from gsuid_core.ai_core.persona.processor import assemble_persona_prompt

prompt = await assemble_persona_prompt("凯露")
# 返回完整的角色扮演prompt字符串
```

## 角色资料存储

角色资料以Markdown格式存储在 `AI_CORE_PATH/persona/` 目录下，每个角色对应一个`.md`文件。

例如：`AI_CORE_PATH/persona/凯露.md`

## 使用示例

### 构建角色提示词

```python
from gsuid_core.ai_core.persona.processor import assemble_persona_prompt
from gsuid_core.ai_core.persona.resource import persist_character_profile

# 1. 保存角色资料
await persist_character_profile("凯露", """
# 凯露

## 基本信息
姓名：凯露
种族：人类
职业：魔术师

## 性格
傲娇、毒舌、但内心善良

## 说话风格
- 常用"哼"表达不屑
- 短句为主
- 偶尔用古语
""")

# 2. 组装完整prompt
prompt = await assemble_persona_prompt("凯露")
```

### 列出所有角色

```python
from gsuid_core.ai_core.persona.resource import list_available_characters

characters = list_available_characters()
print(characters)
```
