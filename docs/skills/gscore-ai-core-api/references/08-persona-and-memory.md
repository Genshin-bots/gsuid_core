# 八、Persona 角色系统 + Memory 记忆系统

## 8.1 Persona 角色系统

Persona 模块提供人格角色的提示词管理和资料存储功能。

### 8.1.1 模块导入

```python
from gsuid_core.ai_core.persona import (
    Persona,
    PersonaMetadata,
    PersonaFiles,
    build_persona_prompt,
    load_persona,
    save_persona,
    list_available_personas,
    get_persona_metadata,
    get_persona_image_path,
    get_persona_avatar_path,
    get_persona_audio_path,
    persona_config_manager,
)
```

### 8.1.2 核心类

```python
class Persona(TypedDict):
    id: str              # 唯一标识
    name: str            # 角色名称
    description: str     # 角色描述
    image_path: str      # 立绘图片路径
    avatar_path: str     # 头像图片路径
    audio_path: str      # 音频文件路径
    config_path: str     # 配置文件路径
    introduction: str    # 角色介绍（长文本）
```

### 8.1.3 构建 Persona 提示词

```python
from gsuid_core.ai_core.persona import build_persona_prompt

# 构建完整的 persona 提示词
prompt = await build_persona_prompt(
    persona_name="my_persona",
    user_name="用户",
    context="当前对话上下文"
)
```

### 8.1.4 Persona 资源管理

```python
from gsuid_core.ai_core.persona import (
    list_available_personas,
    get_persona_metadata,
    get_persona_image_path,
    get_persona_avatar_path,
    get_persona_audio_path,
)

# 列出所有可用 Persona
personas = list_available_personas()

# 获取 Persona 元数据
metadata = get_persona_metadata("my_persona")

# 获取各种资源路径
image_path = get_persona_image_path("my_persona")
avatar_path = get_persona_avatar_path("my_persona")
audio_path = get_persona_audio_path("my_persona")
```

> 运行时若要"把 AI 当前感知到的 Persona 资源文件路径返回给 LLM"，用保底工具 `get_self_persona_info`（[§7.3](./07-builtin-tools.md)）；完整的自我认知（身份/能力/主人列表）用保底工具 `get_self_info`（[§7.2](./07-builtin-tools.md)）。

---

## 8.2 Memory 记忆系统

基于 Mnemis 双路检索思想的多群组/多用户 Agent 记忆系统。

> 详细设计文档：[`docs/MEMORY_SYSTEM.md`](../../MEMORY_SYSTEM.md)

### 8.2.1 模块导入

```python
from gsuid_core.ai_core.memory import (
    memory_config,
    ScopeType,
    make_scope_key,
    observe,
    get_observation_queue,
    ObservationRecord,
    dual_route_retrieve,
    MemoryContext,
    get_ingestion_worker,
)
```

### 8.2.2 记忆检索

```python
# 双路检索获取记忆上下文
mem_ctx = await dual_route_retrieve(
    query="用户之前提到的游戏偏好",
    group_id="群组ID",
    user_id="用户ID",
    top_k=5,
    enable_system2=True,
    enable_user_global=True,
)

# 转换为提示词文本
memory_text = mem_ctx.to_prompt_text(max_chars=2000)
```

### 8.2.3 记忆配置

```python
from gsuid_core.ai_core.memory import memory_config

# 记忆系统配置
memory_config.enable_retrieval    # 是否启用检索
memory_config.enable_system2      # 是否启用 System-2 检索
memory_config.enable_user_global_memory  # 是否启用用户全局记忆
memory_config.retrieval_top_k     # 检索返回数量
```

### 8.2.4 消息观察

```python
from gsuid_core.ai_core.memory import observe, ObservationRecord

# 观察消息并记录到记忆
observe(
    group_id="群组ID",
    user_id="用户ID",
    content="用户说想养一只猫",
    message_type="text"
)

# 获取观察队列
queue = get_observation_queue()
```
