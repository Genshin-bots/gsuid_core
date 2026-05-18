# 消息历史模块解耦变更记录（History → message_history）

> 本文档记录将 `history` 模块从 `ai_core` 中剥离、解耦为通用 Bot 消息历史模块的完整变更。

## 一、背景与动机

原 `gsuid_core/ai_core/history/` 模块同时承担了两类**正交**的职责：

1. **通用消息历史存储** —— 记录每个会话（群聊/私聊）的 Bot 消息输入/输出，滑动窗口 + Token 上限。
2. **AI 会话对象管理** —— 注册/查找/清理 `GsCoreAIAgent` 实例，以及 AI 专用的 prompt 格式化。

第一类职责本身与 AI 无关：用户消息在 `handler.py` 记录、Bot 回复在 `bot.py` 的 `target_send` 记录，
均不受 AI 总开关限制。但模块位于 `ai_core` 之下，且 `HistoryManager` 类中混入了 AI 会话相关字段与方法，
造成了不必要的耦合。

目标：将通用消息历史下沉为独立的顶层模块，使其成为纯粹的「Bot 消息输入/输出历史记录」，
不再耦合 AI；AI 需要对话历史时仍从此模块读取，但模块本身对 AI 无感知。

## 二、模块拆分结果

原 `gsuid_core/ai_core/history/`（`__init__.py` + `manager.py` + `README.md`）被拆分为三部分：

| 新位置 | 职责 | 是否耦合 AI |
|--------|------|------------|
| `gsuid_core/message_history/`（`__init__.py` + `manager.py`） | 通用消息历史：`MessageRecord`、`HistoryManager`、`get_history_manager`。仅依赖 `gsuid_core.models.Event` | 否 |
| `gsuid_core/ai_core/session_registry.py` | `AISessionRegistry`：AI 会话对象（`GsCoreAIAgent`）的注册、查找、空闲清理 | 是（属 ai_core） |
| `gsuid_core/ai_core/history_format.py` | AI 侧格式化：`history_to_prompt`、`history_to_messages`、`format_history_for_agent` | 是（属 ai_core） |

原 `gsuid_core/ai_core/history/` 目录整体删除。

### 2.1 职责归属对照

| 内容 | 拆分前 | 拆分后 |
|------|--------|--------|
| `MessageRecord` 数据类 | `ai_core/history/manager.py` | `message_history/manager.py` |
| 消息存取（`add_message`/`get_history`/`clear_history`/`delete_session` 等） | `HistoryManager` | `HistoryManager`（`message_history`） |
| 滑动窗口 + Token 上限（`MAX_HISTORY_TOKENS`） | `HistoryManager` | `HistoryManager`（`message_history`） |
| 持久化辅助（`get_all_histories`/`load_histories`/`get_stats`） | `HistoryManager` | `HistoryManager`（`message_history`） |
| AI 会话注册（`get/set/remove/has/get_all_ai_session`） | `HistoryManager` | `AISessionRegistry` |
| AI 会话空闲清理（`cleanup_idle_sessions`/`start_cleanup_loop`/`IDLE_THRESHOLD`/`CLEANUP_INTERVAL`） | `HistoryManager` | `AISessionRegistry` |
| AI 历史裁剪（`cleanup_long_ai_history`/`MAX_AI_HISTORY_LENGTH`） | `HistoryManager` | `AISessionRegistry` |
| prompt/messages/agent 上下文格式化 | `ai_core/history/manager.py` 模块级函数 | `ai_core/history_format.py` |

拆分后 `message_history/manager.py` 不含任何 `ai_core` 导入，唯一外部依赖为 `gsuid_core.models.Event`。
`AISessionRegistry` 与 `history_format` 反向依赖 `message_history`，依赖方向为 `ai_core → message_history`，
通用模块对 AI 无感知。

## 三、导入路径迁移对照

| 拆分前 | 拆分后 |
|--------|--------|
| `from gsuid_core.ai_core.history import get_history_manager` | `from gsuid_core.message_history import get_history_manager` |
| `from gsuid_core.ai_core.history import MessageRecord` | `from gsuid_core.message_history import MessageRecord` |
| `from gsuid_core.ai_core.history import format_history_for_agent` | `from gsuid_core.ai_core.history_format import format_history_for_agent` |
| `from gsuid_core.ai_core.history.manager import history_to_prompt` | `from gsuid_core.ai_core.history_format import history_to_prompt` |
| `from gsuid_core.ai_core.history.manager import history_to_messages` | `from gsuid_core.ai_core.history_format import history_to_messages` |
| `history_manager.get_ai_session(...)` 等 AI 会话方法 | `get_ai_session_registry().get_ai_session(...)` |

> `gsuid_core/ai_core/history/` 已被物理删除，**未保留兼容转发层**。第三方插件若直接导入了该路径，需按上表迁移。

## 四、受影响的文件清单

### 新增
- `gsuid_core/message_history/__init__.py`
- `gsuid_core/message_history/manager.py`
- `gsuid_core/ai_core/session_registry.py`
- `gsuid_core/ai_core/history_format.py`

### 删除
- `gsuid_core/ai_core/history/__init__.py`
- `gsuid_core/ai_core/history/manager.py`
- `gsuid_core/ai_core/history/README.md`

### 修改（仅调整导入与调用点，逻辑不变）
- `gsuid_core/handler.py`、`gsuid_core/bot.py` —— 改用通用 `message_history`
- `gsuid_core/ai_core/handle_ai.py`、`gsuid_core/ai_core/heartbeat/decision.py` —— 格式化函数改用 `history_format`
- `gsuid_core/ai_core/ai_router.py`、`gsuid_core/ai_core/buildin_tools/subagent.py` —— AI 会话调用改用 `AISessionRegistry`
- `gsuid_core/ai_core/heartbeat/inspector.py` —— 改用通用 `message_history`
- `gsuid_core/ai_core/statistics/startup.py` —— 启动 `AISessionRegistry` 的空闲清理任务
- `gsuid_core/webconsole/history_api.py` —— 见第五节
- `gsuid_core/webconsole/ai_session_logs_api.py` —— AI 会话调用改用 `AISessionRegistry`
- `gsuid_core/webconsole/chat_with_history_api.py` —— 更新注释中的导入路径

## 五、history_api.py 兼容 AI 关闭（enable_ai=False）

由于消息历史已解耦为通用模块，`webconsole/history_api.py` 现支持在 AI 总开关关闭时正常工作。
新增 `_is_ai_enabled()` 判定（读取 `ai_config.get_config("enable")`），各接口降级规则如下：

| 接口 | AI 开启 | AI 关闭 |
|------|---------|---------|
| `GET /api/history/sessions` | 返回 `has_ai_session` / `ai_history_length` 真实值 | `has_ai_session=false`、`ai_history_length=0` |
| `GET /api/history/{session_id}` | 正常读取消息历史 | 正常读取消息历史（完全不受影响） |
| `DELETE /api/history/{session_id}` | 清空/删除「消息历史 + AI 会话对象」 | 仅清空/删除「消息历史」 |
| `GET /api/history/{session_id}/persona` | 返回 persona 内容 | 统一返回「session 不存在或尚未创建」 |
| `GET /api/history/stats` | `ai_router_sessions` 返回真实统计 | `ai_router_sessions` 返回空统计 |

AI 相关导入（`session_registry`）在 `history_api.py` 中改为**惰性导入并由 `enable_ai` 守卫**，
AI 关闭时不会触及 `ai_core` 的 AI 会话相关代码。

### 5.1 新增：向 Session 发送消息接口（支持文本 / 图片 / 图文混排）

新增 `POST /api/history/{session_id}/send`（带 `require_auth` 鉴权）：

- 请求类型为 `multipart/form-data`，表单字段：`message`（文本）、`images`（图片文件，可多张）、
  `image_urls`（图片直链，可多个）、`at_sender`。**图片由前端直接上传文件，无需 base64 编码。**
- 通过 `_parse_session_id()` 从 `session_id` 解析出 `WS_BOT_ID` / `bot_id` / `group_id` / `user_id`。
- 从 `gss.active_bot` 使用 `WS_BOT_ID` 精确定位对应的 `_Bot` 连接。
- 后端读取上传图片的二进制，连同文本、直链一起经 `MessageSegment` 组装为消息段列表，
  构造 `Event` 与高层 `Bot` 包装器后调用 `bot.send()` 发送。
- 发送的消息经由 `target_send` 自动记录进该 session 的消息历史。

安全约束：`image_urls` 仅接受 `http`/`https` 直链，避免 `MessageSegment.image` 将任意字符串
当作服务器本地文件路径读取造成文件泄露。

该接口属于通用 Bot 能力，与 AI 总开关无关，`enable_ai=False` 时同样可用。
接口文档见 `webconsole/docs/18-history.md` §18.6。

## 六、行为变更说明

整体重构以「不影响原功能」为前提，仅以下一处为细微行为变化：

- **完全删除 Session 时会落盘 AI 会话日志**：原 `HistoryManager.delete_session` 删除 AI 会话时
  仅 `del` 字典项，**不会** flush session logger；拆分后 `DELETE /api/history/{session_id}?delete_session=true`
  改为调用 `AISessionRegistry.remove_ai_session()`，会正确触发 `_session_logger.close()` 落盘。
  这是行为上的**改善**（原行为可能丢失日志尾部），并非功能回归。

其余所有接口的入参、出参、返回结构与原行为完全一致。

## 七、验证

- 全部新增 / 修改文件通过 `py_compile` 语法检查。
- 四个新模块（`message_history`、`message_history.manager`、`ai_core.session_registry`、
  `ai_core.history_format`）导入测试通过，无循环导入。
- 功能冒烟测试通过：`add_message` / `get_history` / `get_stats`、群聊与私聊存取、
  `AISessionRegistry` 的 `set/get/remove`、`delete_session` 仅删历史等行为均正确。
- 解耦确认：`HistoryManager` 已不含 `_ai_sessions` 字段及任何 `*_ai_session` 方法。
