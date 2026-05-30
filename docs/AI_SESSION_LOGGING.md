# AI Session 日志系统

> 本文档描述 `ai_core` 的会话日志系统在 **2026-05-30 统一简化重构**后的架构、规则
> 与格式契约。方案见 `plans/ai_session_log_simplification_20260529.md`，前序见
> `plans/proactive_message_session_unification_20260529.md`。

---

## 1. 一句话定位

`AISessionLogger`（`gsuid_core/ai_core/session_logger.py`）是整个 ai_core **唯一**的
会话日志序列化器。**所有来源**的 LLM 调用——用户对话、Heartbeat、ScheduledTask、
Kanban、工具主动发送、以及记忆/meme/评估等后台调用——都经过它的同一条写盘路径，
保证格式统一、链路统一。

**存储路径**：

| 路径 | 内容 |
|---|---|
| `data/ai_core/session_logs/` | 主 session（用户对话）日志 |
| `data/ai_core/session_logs/subagents/` | subagent 日志（含框架主动消息子 agent、自动派生的后台调用） |

**文件命名**：`{safe_session_id}_{session_uuid}_{create_time}.json`
（`safe_session_id` = session_id 把 `:`/`/` 替换为 `_`）。

---

## 2. 统一链路

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │  来源 A：用户对话                                                      │
 │  handle_ai → ai_router.get_ai_session → create_agent(                 │
 │      session_id=event.session_id, create_by="Chat")                   │
 │                                                                       │
 │  来源 B：框架主动消息（Heartbeat / ScheduledTask / Kanban / tool）     │
 │  emit_proactive_message → 活跃则写主 session logger，                   │
 │                           不活跃则 AISessionLogger.log_standalone_*    │
 │                                                                       │
 │  来源 C：后台 LLM 调用（评估 / meme / 记忆摄入·检索 / 人格构建）         │
 │  create_agent(create_by="MemEntityExtraction", …)  ← 不传 session_id  │
 │      → __init__ 自动派生 auto_<create_by>_<rand> + is_subagent=True    │
 └───────────────────────────────┬─────────────────────────────────────┘
                                 │  （唯一序列化器）
                                 ▼
                       ┌──────────────────────┐
                       │   AISessionLogger     │
                       │   _add_entry()        │ ← entry 类型白名单校验
                       │   _build_data()       │ ← 唯一文件结构来源
                       └──────────┬───────────┘
                                 ▼
                  data/ai_core/session_logs[/subagents]/*.json
```

**关键不变式**：任何 `GsCoreAIAgent` 都恒有 `_session_logger`（非 Optional）。未显式
给 `session_id` 的来源在 `__init__` 里自动派生一个一次性 subagent id——所以"所有调用
来源都写 session log"是**结构性保证**，无法被某个来源遗漏。

---

## 3. 会话窗口规则（"相同 session_id 同一文件 / 超时滚动"）

一个日志文件 = 一个 `(session_id, 会话窗口)`。窗口长度 `SESSION_WINDOW_SECONDS = 3600`
（1 小时）。

创建一个 logger 时如何决定写哪个文件：

```
若 is_subagent:
    永远新建文件                       # subagent 一次性、按 run 隔离，不续写
否则（主 session）:
    F = 该 session_id 在 session_logs/ 下 updated_at 最新的文件
    若 F 存在 且 (now - F.updated_at) <= 3600:
        续写 F                         # 复用 session_uuid / created_at / entries / 文件
        追加一条 session_resumed entry
    否则:
        新建文件（新 session_uuid）      # 上一段会话已"超时关闭"，滚动到新 session_log
        追加一条 session_created entry
```

- **主 session（用户对话）**：1 小时内继续对话 → 续写同一文件（"相同 session_id 写
  同一 session_log"）；空闲超 1 小时后再搭话 → 新文件（"会话超时 1 小时后写另一个
  session_log"）。
- **subagent**：永不续写。每次 run 独立成文件，靠父 session 的 `linked_agents` 串联，
  而不是把多个 run 合进一个文件——这样"一次子任务 = 一段可独立审计的日志"。

> 该规则只有一处实现：`AISessionLogger._find_existing_log_on_disk`。主 session
> 续写、`log_standalone_proactive` 磁盘回退都复用它。

---

## 4. 格式契约（"绝不允许不规范格式"）

### 4.1 文件顶层 schema

由唯一的 `_build_data()` 产出，字段固定：

| 字段 | 类型 | 含义 |
|---|---|---|
| `session_id` | str | 会话标识 |
| `session_uuid` | str | 本窗口文件的 8 位 uuid（webconsole 去重键） |
| `persona_name` | str \| null | 角色名 |
| `create_by` | str | 创建来源（见 §5） |
| `is_subagent` | bool | 是否 subagent 日志 |
| `created_at` / `updated_at` / `ended_at` | float \| null | 时间戳；`ended_at` 仅 `close()` 时写 |
| `entry_count` | int | entries 数量 |
| `entries` | list | 日志条目（见 §4.2） |
| `linked_agents` | list | 关联 agent（见 §4.3） |
| `linked_agent_count` | int | linked_agents 数量 |

### 4.2 entry 结构与类型白名单

每条 entry 固定为 `{"type": <白名单之一>, "timestamp": float, "data": {...}}`。

`type` 受 `SESSION_ENTRY_TYPES` 白名单约束：`_add_entry` 写入时校验，未登记类型记
`logger.warning`（开发期立即暴露），但**仍按统一结构落盘**——日志写入路径绝不因校验
而抛异常或丢数据。新增 entry 类型**必须**先在 `SESSION_ENTRY_TYPES` 登记。

| type | data 关键字段 | 产出方法 |
|---|---|---|
| `session_created` | session_id, session_uuid, persona_name, create_by | `__init__` |
| `session_resumed` | session_id, session_uuid, persona_name, create_by, resumed_from_entries | `__init__`（窗口续写） |
| `session_ended` | ended_at | `close()` |
| `system_prompt` | content（系统提示词的**唯一**记录处，不再塞进 session_created/resumed） | `__init__` 调 `log_system_prompt` |
| `run_start` | （空，纯时间线标记；用户输入见 `user_input`，不重复） | `log_run_start` |
| `run_end` | （空，纯时间线标记；最终输出见 `result`，不重复） | `log_run_end` |
| `result` | output, tool_calls（最终输出的**唯一**记录处） | `log_result` |
| `user_input` | content（用户输入的**唯一**记录处） | `log_user_input` |
| `thinking` | content | `log_thinking` |
| `text_output` | content | `log_text_output` |
| `tool_call` | tool_name, args, tool_call_id | `log_tool_call` |
| `tool_return` | tool_name, content（>2000 截断）, tool_call_id | `log_tool_return` |
| `tools_list` | tools | `log_tools_list` |
| `token_usage` | input_tokens, output_tokens, model_name | `log_token_usage` |
| `node_transition` | node_type, details | `log_node_transition` |
| `error` | error_type, message | `log_error` |
| `agent_linked` | （= 一条 linked_agent 记录） | `link_agent` |
| `proactive_emission` | source, content, trigger_reason, generator_log_files | `log_proactive_emission` |

### 4.3 linked_agents 记录

```json
{
  "agent_type": "sub_agent | peer_agent | parent_agent | proactive_generator",
  "session_id": "...", "session_uuid": "...",
  "persona_name": null, "create_by": "...",
  "log_file": "<被关联 agent 的日志文件路径>", "linked_at": 1717000000.0
}
```

主动消息生成子 agent 用 `agent_type="proactive_generator"`；`create_subagent` 派生的
普通子 agent 用 `sub_agent`（并反向给子 agent 写一条 `parent_agent`）。

### 4.4 proactive_emission payload

```json
{
  "source": "heartbeat | scheduled_task | kanban | tool",
  "content": "实际发出去的文本",
  "trigger_reason": "mood / task_id / subtask name / tool name",
  "generator_log_files": ["<决策/转译/执行体子 agent 的日志路径>", "..."]
}
```

---

## 5. session_id 命名约定

| 来源 | session_id 形态 | dir | 续写 |
|---|---|---|---|
| 用户对话（主干） | `event.session_id`（如 `Bot:...:group:123`） | `session_logs/` | 窗口内续写 |
| Heartbeat 决策 / 发言 | `heartbeat_decision_*` / `heartbeat_output_*` | `subagents/` | 否 |
| ScheduledTask 执行体 | `sched_task_<id>_<ts>` | `subagents/` | 否 |
| Kanban 转译 | `kanban_relay_<id>_<ts>` | `subagents/` | 否 |
| 能力代理 | `capagent_<profile>_<suffix>` | `subagents/` | 否 |
| create_subagent | `subagent_<hash>` | `subagents/` | 否 |
| **自动派生（后台 LLM）** | `auto_<create_by>_<rand>` | `subagents/` | 否 |
| 主动消息磁盘回退 | 复用用户 `event.session_id` | `session_logs/` | 窗口内续写 |

后台 LLM 调用（`create_by` ∈ `CapabilityEvaluator / MemeTagger / MemCategorization /
MemGroupSummary / MemNodeSelection / MemEntityExtraction / BuildPersona /
ImageUnderstand / ImageDescSummary`）不传 `session_id` → 自动派生
`auto_<create_by>_<rand>` 落 `subagents/`。

其中 **图片理解（`ImageUnderstand`，`image_understand/understand.py`）** 与 **图片描述
二次摘要（`ImageDescSummary`，`gs_agent._summarize_image_description`）** 这两条原先是
裸 `pydantic_ai.Agent()` 调用、完全不写日志的旁路，现已统一改走 `create_agent`；并在
拿得到调用方 session（主对话 `GsCoreAIAgent`）时通过 `link_agent` 把这条 subagent 日志
挂到调用方 session 的 `linked_agents`（"附到调用方 session"），webconsole 可从主会话
下钻——拿不到调用方时则以独立 `auto_*` subagent 日志存在。

> ⚠️ 例外：`understand_image` 的 **MCP 回退路径** 是对外部图片转述服务的一次 *工具调用*
> （非 pydantic_ai agent run），不创建 `GsCoreAIAgent`，因此不进 AISessionLogger——
> 它由 MCP 侧自行记录。`ai_core` 内**不再有任何裸 `Agent()` 的 LLM 旁路**。

---

## 6. 生命周期与落盘

- **主 session**：长生命周期，启动后台轮询（`POLL_INTERVAL=15s`；空闲
  `IDLE_PERSIST_THRESHOLD=60s` 或兜底 `PERSIST_INTERVAL=600s` 落盘）。被
  `AISessionRegistry` 空闲清理（`IDLE_THRESHOLD=1800s`）时 `close()` 最终落盘。
- **subagent（含 auto_*）**：**不启动**后台轮询——一次性，跑完即 `close()`（各调用方
  的 finally 已普遍调用），或由 `__del__` 兜底落盘。
- **主动消息磁盘回退**：`log_standalone_proactive` 用一个临时 logger 走会话窗口续写 +
  统一 `_build_data` 写盘，**格式与活跃 session 完全一致**，写完即 `close()`。

### 6.1 定时清理（保留策略）

AI 会话日志与框架日志共用同一个配置项 **`ScheduledCleanLogDay`**（"定时清理几天外
的日志"，默认 `8`）：

- 模块函数 `session_logger.clean_old_session_logs(days)` 删除 `session_logs/` 与
  `session_logs/subagents/` 下 **mtime 超过 `days` 天**的 `.json` 文件，返回删除数量。
- **`days == 0`（或负数）时不清理**，直接返回 0 —— 满足"配置为 0 则保留全部日志"。
- 由 `core_backup` 的每日维护任务 `database_backup`（cron 00:03）调用，与框架
  `clean_log()` 并列，配置改动重启后生效。
- 安全性：活跃 session 会周期性重写文件、mtime 一直很新，不会被误删；空闲超过
  `days` 天的 session 早已不在内存注册表，其日志可安全清理——这也回收了**自动派生
  subagent 日志**的磁盘占用（解决"所有来源都写"带来的文件增长）。

---

## 7. webconsole 读取契约（不变）

`webconsole/ai_session_logs_api.py` 合并内存活跃会话 + 磁盘文件，按 `session_uuid`
去重（内存优先），从 entries 聚合 `type_counts`，并沿 `linked_agents[].log_file`
下钻子 agent。本次重构**不改任何读取字段**：

- 顶层 schema（§4.1）与 `SessionLogSummary` 期望完全一致。
- 新增的 `auto_*` 仅多出若干 subagent 文件，自然出现在 subagents 列表。
- `proactive_emission` / `agent_type="proactive_generator"` 为前序方案已引入的追加项。

---

## 8. 本次重构改了什么（2026-05-30）

| 维度 | 重构前 | 重构后 |
|---|---|---|
| 序列化路径 | 两条：`_build_data()` + 手工拼文件的 `persist_proactive_emission_to_disk`（145 行） | **一条**：只有 `_build_data()`；磁盘回退改用临时 logger 复用之 |
| 会话窗口 | 无——永远复用最新文件，文件无限增长 | **1 小时窗口**：窗口内续写、超时滚动新文件 |
| 覆盖来源 | 评估 / meme / 记忆等后台调用**不写日志** | logger 恒在 + 未传 id 自动派生 → **所有来源都写** |
| logger 可空性 | `Optional[AISessionLogger]` + run() 里 ~15 处 None 守卫 | **非 Optional**，守卫全删 |
| 格式约束 | entry 类型自由字符串 | `SESSION_ENTRY_TYPES` 白名单 + 写入校验 |
| subagent 轮询 | 每个 subagent 各挂一个 15s 轮询任务 | subagent 不轮询，close/__del__ 落盘 |
| 日志清理 | 无（AI 日志永不清理） | 遵循 `ScheduledCleanLogDay`，每日清理 X 天外日志（0=不清理） |
| 裸 `Agent()` LLM 旁路 | 图片理解 / 图片描述二次摘要 / 超轮数兜底总结直接 `pydantic_ai.Agent().run()`，**不写任何会话日志** | 图片两条改走 `create_agent`（自动 subagent 日志 + link 调用方）；兜底总结就地写进当前 session 的 `text_output`/`result`——**`ai_core` 内无裸 `Agent()` LLM 旁路** |

净效果：**删除 ~145 行 + ~15 处守卫**，新增 ~40 行（窗口规则 + 白名单 + 自动派生），
代码净减少且只剩一条链路。

---

## 9. 不在范围（已知后续工单）

- **按数量 / 总大小滚动清理**：当前清理按"天数"（`ScheduledCleanLogDay`，见 §6.1），
  尚未支持"单 persona 最多 N 个 / 目录总大小上限"这类策略。
- **`log_standalone_proactive` 与主 session 同时创建的强一致**：低频场景，沿用既有
  "概率极低"口径，不加锁。
- **前端 dist 对 `auto_*` / `proactive_emission` 的展示样式**：前端工单。
