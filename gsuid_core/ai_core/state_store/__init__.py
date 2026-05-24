"""通用持久状态存储模块

框架级跨会话状态存储，让复杂任务 / 周期任务 / 虚拟盘账户等场景的结构化数据
不依赖单一会话生命周期。所有插件都能通过下述工具直接读写持久数据，无需自建表。

## 两种原语

- **键值状态 (``state_*``)**：扁平 key → value。``value`` 可以是任意 JSON 可序列化对象，
  适合存"上次进度=N"、"上次回答=X"等简单状态。带乐观锁，多并发安全。
- **结构化集合 (``record_*``)**：在键值之上叠"具名集合 + 多条记录"语义。每条记录
  有独立 id，适合存账户 / 持仓 / 流水 / 名单 / 库存等场景。``record_summary`` 提供
  对集合做计数 / 求和 / 求均值的开箱即用聚合，方便 ``internal_reporter`` 出报告。

何时选 ``state_*``：单值、不需要追溯历史。
何时选 ``record_*``：会有多条 + 需要查询 / 更新单条 + 后期要聚合统计。

## 模块结构

- ``models.py``       : ``AIPersistentState`` 数据库模型（单表存所有 state / record）
- ``store.py``        : ``state_mutate`` 乐观锁底层 + 5 个 state_* 实现函数
- ``tools.py``        : ``@ai_tools`` 装饰过的 ``state_*`` LLM 工具
- ``record_tools.py`` : ``@ai_tools`` 装饰过的 ``record_*`` LLM 工具（基于 ``state_mutate``）

## 工具分类

所有 ``state_*`` 与 ``record_*`` 都注册为 ``category="buildin"`` —— **保底池**，
主人格与能力代理都能直接调用，不需要向量检索命中。
"""

from .store import (
    state_mutate,
    state_get_value,
    state_list_keys,
    state_set_value,
    state_append_item,
    state_delete_value,
)
from .tools import (
    state_get,
    state_set,
    state_list,
    state_append,
    state_delete,
)
from .models import AIPersistentState
from .record_tools import (
    record_get,
    record_put,
    record_list,
    record_append,
    record_delete,
    record_update,
    record_summary,
)

__all__ = [
    "AIPersistentState",
    "state_mutate",
    "state_get_value",
    "state_set_value",
    "state_delete_value",
    "state_list_keys",
    "state_append_item",
    "state_get",
    "state_set",
    "state_delete",
    "state_list",
    "state_append",
    "record_put",
    "record_get",
    "record_list",
    "record_append",
    "record_update",
    "record_delete",
    "record_summary",
]
