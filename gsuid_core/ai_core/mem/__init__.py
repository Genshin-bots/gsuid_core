"""Mem模块 - Agent记忆层

基于memv的预测-校准提取架构，用于管理Agent的用户记忆。
直接复用ai_config中的配置。

使用示例:
```python
from gsuid_core.ai_core.mem import memory_client

# 获取用户记忆
result = await self.memory.retrieve(
    user_message,
    user_id=self.user_id,
    top_k=5,
)

# 添加用户记忆
await self.memory.add_exchange(
    user_id=self.user_id,
    user_message=user_message,
    assistant_message=response.output,
)

# 真正执行LLM保存
count = await memory.process(user_id)
print(f"Extracted {count} knowledge entries")
```
"""

from gsuid_core.ai_core.mem.memory import memory_client

__all__ = [
    # 核心函数
    "memory_client",
]
