# 六、AI Session 路由与 Persona

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[五、Bot 三类](./05-bot-classes.md) · **下一章**：[七、工具注册表与 Agent 装配](./07-tool-registry-and-agent.md)

本章讲：一条消息怎么找到/创建对应的 `GsCoreAIAgent` 会话、Session ID 怎么设计、会话怎么
防 OOM、Persona 怎么配置与热重载。

## 6.1 Session ID 设计（关键）

`Event.session_id` 属性自动生成：

```
群聊: f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
      例: "ws-onebot:onebot:bot_001:group:789012"
私聊: f"{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
      例: "ws-onebot:onebot:bot_001:private:345678"
```

> **群聊 Session ID 不含 `user_id`**——群内所有用户共享同一个 Session 与记忆。这是历史致命
> 缺陷 D-1 的修复：旧格式 `{bid}%%%{gid}%%%{uid}` 绑了 user_id，导致"群里每个人各和 AI 聊各
> 的、互相没有上下文"。私聊才用 `user_id` 保证一对一独立。
>
> 配套：`HistoryManager` 群聊时把 `storage_event` 的 `user_id` **置空**，确保同群所有消息共享
> 同一个 deque。改 Session 标识时必须同时维护这个不变量。

## 6.2 AI Router（`ai_router.py`）

```python
async def get_ai_session(event: Event) -> GsCoreAIAgent:
    return await _get_or_create_ai_session(event)
```

`_get_or_create_ai_session` 流程：

```
1. session_id = event.session_id
2. history_manager.update_session_access(event)
3. registry.get_ai_session(session_id) 查已有
4. 已有 → 检查 _check_persona_changed()，变了则热重载（移除旧 + 重建）
5. 无/需重建 → get_persona_for_session() 取 persona_name
   └── 返回 None → raise ValueError（没配 persona，不该进到这）
6. build_persona_prompt(persona_name) 构建 system_prompt
7. create_agent(system_prompt, persona_name, create_by="Chat")
8. registry.set_ai_session(session_id, session)
```

> **身份不变量（system_prompt 内，`persona/prompts.py::SYSTEM_CONSTRAINTS`）**：
> **用户ID 仅供内部认人、绝不对外输出**。群聊靠 ID 区分发言人——历史经
> `format_history_for_agent` 渲染成 `id(昵称)`、当前轮经 `_build_relationship_description`
> 也带 `用户名(用户ID:…)`——但**回复对外只用昵称或已记的别名**（【群成员称呼】），
> 既无昵称也无别名时用角色化泛称带过。ID 唯一允许出现在输出的场合是 `@用户ID` 语法
> （由 `send_chat_result` 解析成真正的 @，见 [§04](./04-event-trigger-flow.md)）。原
> 「说话者感知 / 人际关系感知」两段重复内容已合并为一段「当前状态感知」去重。

## 6.3 消息历史与 AI 会话已解耦（两个独立模块）

| 模块 | 类 | 文件 | 职责 |
|------|----|----|----|
| 消息历史 | `HistoryManager` | `gsuid_core/message_history/manager.py` | 记录 Bot 输入/输出历史，**与 AI 无关**，AI 关了也记 |
| AI 会话注册表 | `AISessionRegistry` | `ai_core/session_registry.py` | `GsCoreAIAgent` 对象注册与生命周期，仅 AI 开启时用 |

```python
class AISessionRegistry:
    _ai_sessions: Dict[str, Any]   # session_id -> GsCoreAIAgent
    def get_ai_session(self, session_id): ...
    def set_ai_session(self, session_id, session): ...
```

## 6.4 内存保护（**不存在 OOM 风险**，多重防线）

| 机制 | 所属 | 配置 | 效果 |
|------|------|------|------|
| 滑动窗口 | `HistoryManager` | `deque(maxlen=40)` | 每 Session 最多 40 条消息 |
| Token 上限 | `HistoryManager` | `MAX_HISTORY_TOKENS=160000` | 单 Session Token 超限淘汰最旧 |
| AI 历史限制 | `AISessionRegistry` | `MAX_AI_HISTORY_LENGTH=30` | AI 对话历史 ≤ 30 条 |
| Agent 内部截断 | `GsCoreAIAgent` | `max_history=50` | 超过安全截断（含 ToolCall/ToolReturn 配对保护） |
| 空闲清理 | `AISessionRegistry` | `IDLE_THRESHOLD=1800`(30min) | 30 分钟不活跃 Session 自动清除 |
| 定时清理 | `AISessionRegistry` | `CLEANUP_INTERVAL=3600`(1h) | 每小时检查一次 |

> ⚠️ **隐形 Token 爆炸**：`deque(maxlen=40)` 只按**条数**截断。群里 5 个人各发 10 篇 5000 字
> 长文 = 50 条但 25 万字，瞬间突破 Token 上限。所以 `GsCoreAIAgent` 内部用
> `_truncate_history_with_tool_safety()` 按 Token 安全截断，并保证 `ToolCallPart` 与
> `ToolReturnPart` **始终配对**（否则 pydantic-ai 报 "tool result's tool id not found"）。
> 改历史截断逻辑时**必须**保留这个配对保护（历史缺陷见 [§12](./12-developer-pitfalls.md)）。

## 6.5 Persona Prompt 热重载（mtime 检测）

历史缺陷 D-3：Session 一旦创建 `system_prompt` 就固定，管理员改了人设老用户不生效。修复：

```python
_persona_mtime_cache: dict[str, float] = {}

def _check_persona_changed(session, persona_name) -> bool:
    if session.persona_name != persona_name:
        return True
    current = _get_persona_mtime(persona_name)
    if current > _persona_mtime_cache.get(persona_name, 0.0):
        _persona_mtime_cache[persona_name] = current
        return True
    return False
```

检测到 persona 文件 mtime 变化 → 移除旧 Session 并重建。`GsCoreAIAgent` 有 `persona_name`
属性追踪，`create_agent()` 支持 `persona_name` 参数。

## 6.6 Persona 配置系统（`persona/config.py`）

### 配置文件布局

```
RESOURCE_PATH/persona/{persona_name}/
├── config.json          # Persona 配置（不含 introduction）
├── persona.md           # 角色设定（Markdown）
├── avatar.png / image.png   # 头像 / 立绘（可选）
└── audio.{mp3,ogg,wav,m4a,flac}   # 音频（可选，优先级 mp3 > ogg > wav > m4a > flac）
```

### `DEFAULT_PERSONA_CONFIG`

| 配置项 | 类型 | 默认 | 说明 |
|--------|------|------|------|
| `ai_mode` | List[str] | `["提及应答"]` | AI 行动模式 |
| `scope` | str | `"disabled"` | 启用范围 |
| `target_groups` | List[str] | `[]` | 目标群聊 |
| `inspect_interval` | int | `30` | 巡检间隔（分钟） |
| `keywords` | List[str] | `[]` | 唤醒关键词 |

- **ai_mode**：`提及应答` / `定时巡检` / `趣向捕捉(暂不可用)` / `困境救场(暂不可用)`
- **scope**：`disabled`（不启用）/ `global`（对所有群启用，**全局唯一**）/ `specific`（仅
  `target_groups`）

### Persona 匹配规则（`get_persona_for_session`）

```
1. 先找 scope="specific" 且 target_groups 含该 group_id 的 persona
2. 没有 → 找 scope="global" 的 persona
3. 没有 → 返回 None（不触发 AI）
注意：全局只能有一个 scope="global" 的 persona（set_scope 时校验 validate_global_uniqueness）
```

`PersonaConfigManager` 提供 `set_scope` / `set_target_groups` / `set_ai_mode` /
`set_inspect_interval` / `set_keywords` 等方法，全部即时持久化。

### Persona 配置热重载特殊处理

- 改 `ai_mode` 含"定时巡检" → 调 `start_heartbeat_inspector()` 启动巡检。
- 改 `inspect_interval` 且已启用巡检 → `inspector.stop_for_persona()` + `start_for_persona()`
  重启该 persona 的巡检 job（这是唯一不能"下次自然生效"的配置，因为绑了 APScheduler job）。

## 6.7 Persona 进阶模块

| 模块 | 文件 | 作用 |
|------|------|------|
| 情绪状态机 | `persona/mood.py` | 角色情绪状态 |
| 群聊适应性 | `persona/group_context.py` | 按群画像调整口吻 |
| 自我认知 | `ai_core/self_cognition.py` | `self_model` 演化层（`commitments`/`preferences_learned`/`recurring_topics`/`self_notes`），每轮 `build_self_cognition_context` 拼"【关于我自己】"注入到**用户消息侧**（不进 system_prompt，避免 prompt cache 抖动） |

> `voice_anchor` 是逐轮口吻锚点（旁路字段），Persona 启动迁移会处理它。
