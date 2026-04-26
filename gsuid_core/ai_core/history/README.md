# History 历史会话管理模块

历史会话管理模块，提供滑动窗口机制管理每个会话（群聊/私聊）的最近30条消息，并支持将历史记录转换为AI可用的prompt格式。

## 核心设计

- **群聊场景**：整个群共享历史记录（不区分用户），记录群内所有用户的最近30条消息
- **私聊场景**：单独维护用户历史记录
- **滑动窗口**：每个session最多保留30条消息，超出后自动淘汰最旧消息

## 自动消息记录

本模块已集成到机器人核心流程中，自动记录消息：

- **用户消息记录**：在 [`handler.py`](../../handler.py:80) 中，所有收到的用户文本消息会自动记录到历史记录，包含用户ID、昵称、图片ID、@列表等元数据
- **AI回复记录**：在 [`handle_ai.py`](../../ai_core/handle_ai.py:90) 中，AI发送的回复会自动记录到历史记录

## 历史上下文注入

当用户触发AI时，[`handle_ai.py`](../../ai_core/handle_ai.py:98) 会自动：

1. 获取当前session的最近30条历史消息
2. 使用 `format_history_for_agent()` 格式化为Agent可用的上下文格式
3. 将历史上下文注入到RAG上下文中，作为AI的参考

格式示例：
```
【历史对话】
当前用户ID: 444835641：
"我觉得上周的新番挺不错"
--- 用户上传图片ID: sdafa444 ———
--- 提及用户(@用户): 444835642 ———

444835642：
"确实，那就是神作"

444835643：
"我也同意"
--- 用户上传图片ID: sdaaaa ———
```

## 文件结构

```
history/
├── __init__.py      # 模块导出
├── manager.py       # 核心实现
└── README.md        # 使用文档
```

## 核心类与函数

### HistoryManager

历史会话管理器，使用滑动窗口机制为每个session单独维护最近30条消息。

#### 特性
- **滑动窗口**: 每个session最多保留30条消息，超出自动淘汰最旧消息
- **线程安全**: 使用锁机制保证并发访问安全
- **群聊共享**: 群聊内所有用户共享历史记录
- **私聊独立**: 每个私聊用户有独立的历史记录

#### 创建实例

```python
from gsuid_core.ai_core.history import HistoryManager, get_history_manager

# 方式1: 创建独立实例
manager = HistoryManager(max_messages=30)

# 方式2: 使用全局单例（推荐）
manager = get_history_manager(max_messages=30)
```

#### 添加消息

```python
from gsuid_core.ai_core.history import get_history_manager

manager = get_history_manager()

# 群聊消息（整个群共享）
manager.add_message(
    group_id="123456",      # 群聊ID
    user_id="789",          # 发送者用户ID
    role="user",            # 角色: user/assistant/system
    content="你好",         # 消息内容
    user_name="张三",       # 发送者昵称（可选）
    metadata={              # 元数据（可选）
        "image_id_list": ["img001"],
        "at_list": ["user001"],
    },
)

# 私聊消息（group_id设为None）
manager.add_message(
    group_id=None,
    user_id="789",
    role="user",
    content="你好",
    user_name="张三",
)
```

#### 获取历史记录

```python
# 获取完整历史（群聊场景）
history = manager.get_history(group_id="123456", user_id="789")

# 获取最近10条
recent = manager.get_history(group_id="123456", user_id="789", limit=10)

# 获取消息数量
count = manager.get_history_count(group_id="123456", user_id="789")
```

#### 管理历史记录

```python
# 清空指定session的历史
manager.clear_history(group_id="123456", user_id="789")

# 删除整个session（释放内存）
manager.delete_session(group_id="123456", user_id="789")

# 列出所有活跃session
sessions = manager.list_sessions()

# 获取统计信息
stats = manager.get_stats()
```

### MessageRecord

单条消息记录的数据类。

```python
from gsuid_core.ai_core.history import MessageRecord

record = MessageRecord(
    role="user",                    # 角色
    content="你好",                 # 内容
    user_id="789",                  # 发送者用户ID
    user_name="张三",               # 发送者昵称
    timestamp=1234567890.0,         # 时间戳（自动填充）
    metadata={"key": "value"},     # 元数据
)

# 转换为字典
data = record.to_dict()

# 从字典创建
record = MessageRecord.from_dict(data)
```

### SessionKey

会话标识键，用于唯一标识一个session。

```python
from gsuid_core.ai_core.history import SessionKey

# 群聊session（整个群共享）
key = SessionKey(group_id="123456")

# 私聊session
key = SessionKey(group_id="user_789")  # 使用user_id作为key

# 转换为字符串
key_str = str(key)  # "group:123456" 或 "private"
```

### format_history_for_agent()

**核心函数**：将历史记录格式化为Agent可用的上下文格式。

这是给AI提供历史消息的主要方法，格式参考 persona/prompts.py 中的 User Input 格式。

```python
from gsuid_core.ai_core.history import (
    get_history_manager,
    format_history_for_agent,
)

manager = get_history_manager()

# 获取历史记录
history = manager.get_history(group_id="123456", user_id="789")

# 格式化为Agent上下文
context = format_history_for_agent(
    history=history,
    current_user_id="789",           # 当前触发AI的用户ID
    current_user_name="张三",          # 当前用户昵称（可选）
)

print(context)
# 输出:
# 当前用户ID: 789：
# "今天天气怎么样？"
#
# 444835642：
# "我觉得会下雨"
# --- 用户上传图片ID: img001 ———
#
# 444835643：
# "带伞吧"
```

### history_to_prompt()

将历史记录转换为简单的prompt字符串。

```python
from gsuid_core.ai_core.history import get_history_manager, history_to_prompt

manager = get_history_manager()
history = manager.get_history(group_id="123456", user_id="789")

# 默认格式
prompt = history_to_prompt(history)
# 输出:
# [用户-张三]: 你好
# [AI]: 你好！有什么可以帮助你的吗？
# [用户-李四]: 今天天气怎么样？

# 不包含system消息
prompt = history_to_prompt(history, include_system=False)

# 自定义格式
template = "[{index}] {role}({user_name}): {content}"
prompt = history_to_prompt(history, format_template=template)
```

### history_to_messages()

将历史记录转换为OpenAI格式的messages列表。

```python
from gsuid_core.ai_core.history import (
    get_history_manager,
    history_to_messages,
)

manager = get_history_manager()
history = manager.get_history(group_id="123456", user_id="789")

# 转换为OpenAI格式
messages = history_to_messages(history)
# 输出:
# [
#     {"role": "user", "content": "你好"},
#     {"role": "assistant", "content": "你好！有什么可以帮助你的吗？"},
#     {"role": "user", "content": "今天天气怎么样？"},
# ]
```

## 持久化支持

管理器提供数据导出/导入方法，支持将历史记录持久化到存储。

```python
# 导出所有历史（用于保存）
all_data = manager.get_all_histories()
# 返回: {SessionKey: [MessageRecord, ...]}

# 序列化为可JSON存储的格式
import json
save_data = {
    str(key): [record.to_dict() for record in records]
    for key, records in all_data.items()
}
json_str = json.dumps(save_data, ensure_ascii=False)

# 从JSON恢复
loaded_data = json.loads(json_str)
manager.load_histories(loaded_data)
```

## 使用示例

### 基础使用流程（已自动集成）

```python
from gsuid_core.ai_core.history import (
    get_history_manager,
    format_history_for_agent,
)

# 1. 获取管理器实例
manager = get_history_manager()

# 2. 记录用户消息（handler.py中已自动完成）
manager.add_message(
    group_id="123456",
    user_id="789",
    role="user",
    content="你好",
    user_name="张三",
)

# 3. 当AI被触发时，获取历史作为上下文
history = manager.get_history(group_id="123456", user_id="789")
context = format_history_for_agent(
    history=history,
    current_user_id="789",
    current_user_name="张三",
)

# 4. 将上下文传递给AI
# 在handle_ai.py中已自动完成：
# rag_context = f"【历史对话】\n{context}\n\n{rag_context}"

# 5. 记录AI回复（handle_ai.py中已自动完成）
manager.add_message(
    group_id="123456",
    user_id="789",
    role="assistant",
    content="你好！我是AI助手...",
)
```

### 群聊场景

```python
from gsuid_core.ai_core.history import get_history_manager

manager = get_history_manager()

# 用户A发送消息
manager.add_message(
    group_id="group_456",
    user_id="user_A",
    role="user",
    content="大家好",
    user_name="Alice",
)

# 用户B发送消息（同一群聊，共享历史）
manager.add_message(
    group_id="group_456",
    user_id="user_B",
    role="user",
    content="你好Alice",
    user_name="Bob",
)

# 获取该群的历史（包含Alice和Bob的消息）
history = manager.get_history(group_id="group_456", user_id="user_A")
# 返回: [Alice的消息, Bob的消息, ...]
```

### 私聊场景

```python
from gsuid_core.ai_core.history import get_history_manager

manager = get_history_manager()

# 私聊时 group_id 设为 None，使用 user_id 作为session key
manager.add_message(
    group_id=None,
    user_id="user_123",
    role="user",
    content="你好",
    user_name="张三",
)

# 获取该用户的历史
history = manager.get_history(group_id=None, user_id="user_123")
```

## 注意事项

1. **群聊共享**: 群聊内所有用户共享同一份历史记录，便于AI理解群聊上下文
2. **滑动窗口**: 每个session最多保留30条消息（可配置），超出后最旧的消息会被自动淘汰
3. **内存管理**: 长期运行的服务建议定期清理不活跃的session（使用 `delete_session`）
4. **线程安全**: 所有操作都是线程安全的，可在多线程/异步环境中使用
5. **持久化**: 当前为内存存储，重启后数据丢失。如需持久化，请使用 `get_all_histories()` 和 `load_histories()` 自行实现存储逻辑
