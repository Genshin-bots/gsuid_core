# Persona 模块文档

角色扮演系统模块，提供人格角色的提示词管理和资料存储功能。

## 文件结构

```
persona/
├── __init__.py      # 模块导出
├── models.py        # 数据模型（PersonaFiles、PersonaMetadata）
├── persona.py       # Persona核心类
├── processor.py     # 角色提示词组装器
├── prompts.py       # 提示词模板定义
├── resource.py      # 角色资料持久化（向后兼容的函数接口）
├── startup.py       # 初始化默认角色
└── README.md        # 使用文档
```

## 模块职责

| 文件 | 职责 |
|------|------|
| `models.py` | 定义Persona相关的数据模型 |
| `persona.py` | Persona核心类，抽象和管理单个角色的资源 |
| `prompts.py` | 定义角色扮演所需的各种提示词模板 |
| `resource.py` | 管理角色资料的持久化存储（向后兼容的函数接口） |
| `processor.py` | 组装完整的角色提示词 |
| `startup.py` | 初始化默认角色 |
| `__init__.py` | 模块导出 |

## 角色存储结构

每个角色在 `AI_CORE_PATH/persona/{角色名}/` 目录下有独立的文件夹，包含：

| 文件 | 说明 | 是否必须 |
|------|------|----------|
| `persona.md` | 角色自述文件（Markdown格式） | 是 |
| `avatar.png` | 角色头像图片 | 否 |
| `image.png` | 角色立绘图片 | 否 |
| `audio.mp3` | 角色音频文件 | 否 |
| `audio.ogg` | 角色音频文件（备选格式） | 否 |
| `audio.wav` | 角色音频文件（备选格式） | 否 |
| `audio.m4a` | 角色音频文件（备选格式） | 否 |
| `audio.flac` | 角色音频文件（备选格式） | 否 |

**音频格式优先级**：mp3 > ogg > wav > m4a > flac

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

## Persona 核心类 (persona.py)

### Persona 类

用于抽象和管理单个角色的人格资源。

```python
from gsuid_core.ai_core.persona import Persona

# 创建Persona实例
persona = Persona("凯露")

# 检查角色是否存在
if persona.exists():
    print(f"角色 {persona.name} 存在")

# 加载角色内容
content = await persona.load_content()

# 保存角色内容
await persona.save_content("# 凯露\n\n这是一个傲娇的角色...")

# 获取资源路径
avatar_path = persona.get_avatar_path()      # 头像路径
image_path = persona.get_image_path()          # 立绘路径
audio_path = persona.get_audio_path()          # 音频路径（自动按优先级查找）

# 保存资源文件
await persona.save_avatar(image_data)          # 保存头像
await persona.save_image(image_data)           # 保存立绘
await persona.save_audio(audio_data, extension="mp3")  # 保存音频

# 获取元数据
metadata = persona.get_metadata()
print(metadata.to_dict())
# {'name': '凯露', 'has_avatar': True, 'has_image': True, 'has_audio': True}

# 删除角色
persona.delete()
```

### 类方法

```python
# 列出所有角色
personas = Persona.list_all()
for p in personas:
    print(p.name)

# 列出所有角色名称
names = Persona.list_all_names()
# ['凯露', '帕拉斯', ...]

# 获取指定角色
persona = Persona.get("凯露")
```

## 资源管理函数 (resource.py)

提供向后兼容的函数接口，内部使用Persona类实现。

### save_persona()
保存角色资料到本地存储。

```python
from gsuid_core.ai_core.persona import save_persona

await save_persona("凯露", "# 凯露\n这是一个傲娇的角色...")
```

### load_persona()
从本地存储加载角色资料。

```python
from gsuid_core.ai_core.persona import load_persona

profile = await load_persona("凯露")
```

### list_available_personas()
列出所有可用的角色名称。

```python
from gsuid_core.ai_core.persona import list_available_personas

personas = list_available_personas()
# ['凯露', '帕拉斯', ...]
```

### get_persona_avatar_path()
获取角色头像路径。

```python
from gsuid_core.ai_core.persona import get_persona_avatar_path

path = get_persona_avatar_path("凯露")
```

### get_persona_image_path()
获取角色立绘路径。

```python
from gsuid_core.ai_core.persona import get_persona_image_path

path = get_persona_image_path("凯露")
```

### get_persona_audio_path()
获取角色音频路径（自动按优先级查找）。

```python
from gsuid_core.ai_core.persona import get_persona_audio_path

path = get_persona_audio_path("凯露")
# 优先返回 mp3，如果不存在则依次查找 ogg、wav、m4a、flac
```

### get_persona_metadata()
获取角色元数据。

```python
from gsuid_core.ai_core.persona import get_persona_metadata

metadata = get_persona_metadata("凯露")
# {'name': '凯露', 'has_avatar': True, 'has_image': True, 'has_audio': True}
```

### delete_persona()
删除角色及其所有资源文件。

```python
from gsuid_core.ai_core.persona import delete_persona

delete_persona("凯露")
```

## 提示词组装 (processor.py)

### build_persona_prompt()
组装完整的角色提示词。

```python
from gsuid_core.ai_core.persona import build_persona_prompt

prompt = await build_persona_prompt("凯露")
# 返回完整的角色扮演prompt字符串
```

### build_new_persona()
使用AI生成新的角色提示词。

```python
from gsuid_core.ai_core.gs_agent import build_new_persona

content = await build_new_persona("一个傲娇的猫耳魔法师")
# 返回AI生成的角色描述
```

## 使用示例

### 使用Persona类管理角色

```python
from gsuid_core.ai_core.persona import Persona

# 创建或获取角色
persona = Persona("凯露")

# 保存角色资料
await persona.save_content("""
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

# 保存头像和立绘
with open("kailu_avatar.png", "rb") as f:
    await persona.save_avatar(f.read())

with open("kailu_image.png", "rb") as f:
    await persona.save_image(f.read())

# 保存音频
with open("kailu_voice.mp3", "rb") as f:
    await persona.save_audio(f.read(), extension="mp3")

# 获取元数据
metadata = persona.get_metadata()
print(f"角色: {metadata.name}")
print(f"有头像: {metadata.has_avatar}")
print(f"有立绘: {metadata.has_image}")
print(f"有音频: {metadata.has_audio}")
```

### 使用函数接口（向后兼容）

```python
from gsuid_core.ai_core.persona import (
    save_persona,
    load_persona,
    build_persona_prompt,
    list_available_personas,
    get_persona_metadata,
)

# 保存角色资料
await save_persona("凯露", "# 凯露\n\n这是一个傲娇的角色...")

# 加载角色资料
content = await load_persona("凯露")

# 组装完整prompt
prompt = await build_persona_prompt("凯露")

# 列出所有角色
characters = list_available_personas()
print(characters)

# 获取元数据
metadata = get_persona_metadata("凯露")
```

### 列出所有角色

```python
from gsuid_core.ai_core.persona import Persona

# 获取所有角色
personas = Persona.list_all()
for p in personas:
    metadata = p.get_metadata()
    print(f"{metadata.name}: 头像={metadata.has_avatar}, 立绘={metadata.has_image}, 音频={metadata.has_audio}")
```

## WebConsole API

WebConsole 提供以下 RESTful API 管理角色：

- `GET /api/persona/list` - 获取角色列表（包含元数据）
- `GET /api/persona/{name}` - 获取角色详情
- `GET /api/persona/{name}/avatar` - 获取头像
- `GET /api/persona/{name}/image` - 获取立绘
- `GET /api/persona/{name}/audio` - 获取音频（自动识别格式）
- `POST /api/persona/{name}/avatar` - 上传头像
- `POST /api/persona/{name}/image` - 上传立绘
- `POST /api/persona/{name}/audio` - 上传音频（支持format参数）
- `POST /api/persona/create` - 创建新角色
- `DELETE /api/persona/{name}` - 删除角色

详见 [API.md](../../webconsole/API.md) 第13节。
