# 十一、统计 / 网页控制台 / 数据库 / 帮助系统

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十、RAG 知识库与嵌入](./10-rag-knowledge-embedding.md) · **下一章**：[十二、已知坑与开发注意事项](./12-developer-pitfalls.md)

本章集中讲四块支撑系统：AI 统计、网页控制台（含认证加密）、数据库基类（含 AI 表与总开关）、
帮助系统。它们是改框架时频繁打交道、又容易踩约定的地方。

## 11.1 AI Statistics 统计系统（`ai_core/statistics/`）

收集/聚合/持久化 AI 各类统计，喂前端面板。

```
statistics/
├── manager.py           # StatisticsManager 单例
├── models.py            # 7 个数据库表
├── dataclass_models.py  # 内存数据结构（BotState / LatencyStats / TokenUsage）
└── startup.py           # @on_core_start / @on_core_shutdown / 零点重置
```

**统计维度**：Token 消耗（分模型/分类型）、Session 内存占用、活跃度（Persona 排行/触发方式占比/
群组用户活跃榜）、性能质量（P95 延迟/环节耗时/意图分布/失败率错误码）、Heartbeat 专项、RAG 命中率、
**记忆系统 7 项**（observations/ingestions/ingestion_errors/retrievals/entities/edges/episodes）。

**数据库表**（均 `BaseIDModel`，全局统计无 bot_id）：`AIDailyStatistics`（含 memory_* 字段）、
`AITokenUsageByModel`、`AITokenUsageByType`、`AIHeartbeatMetrics`、`AIGroupUserActivityStats`、
`AIRAGMissStatistics`、`AIRAGDocumentStatistics`。

**持久化机制**：

```python
@on_core_start  async def init_ai_core_statistics():      # 启动：起空闲清理 + 巡检（查总开关）+ 回灌今日
@on_core_shutdown async def shutdown_ai_core_statistics(): # 关闭：持久化到 DB
@scheduler.scheduled_job("cron", hour=0, minute=0)  _scheduled_ai_core_reset   # 零点：落盘 + 重置计数器
@scheduler.scheduled_job("cron", minute="*/30")     _persist_loop              # 每 30 分钟持久化
```

用法：直接用全局单例 `statistics_manager`，`record_token_usage(model_name, chat_type, ...)` /
`record_latency` / `record_intent` / `record_trigger` / `record_memory_*` / `get_summary()`。

**Token 数值来源与流式 usage 语义（2026-07-04）**：统计/预算的 token 数全部来自
`gs_agent.py::_execute_run` 末尾的 `result.usage()`，其正确性取决于 pydantic_ai 对流式
chunk usage 的累计方式。vLLM/SGLang 系网关（如 SiliconFlow）**每个 chunk 都带累计 usage**，
默认逐 chunk 累加会令统计膨胀约 chunk 数倍（output 近似平方级）。已由 OpenAI 配置项
`usage_stats_mode`（auto/incremental/cumulative）+ auto 模式在线探测修复，详见
[§12.17 流式 usage 累计语义膨胀](./12-developer-pitfalls.md)。

前端 API：`GET /api/ai/statistics/*`（summary / token-by-model / persona-leaderboard /
active-users / trigger-distribution / intent-distribution / errors / heartbeat / rag / history）。

## 11.2 网页控制台（`gsuid_core/webconsole/`）

FastAPI 路由，全部走鉴权依赖：

```python
from fastapi import APIRouter, Depends
from gsuid_core.webconsole.auth_api import require_auth
app = APIRouter()
@app.get("/api/example")
async def example(_user: Dict = Depends(require_auth)): ...
```

主要业务 API 文件：`persona_api.py`、`mcp_config_api.py`、`embedding_config_api.py`、
`ai_scheduled_task_api.py`、`knowledge_base_api.py`、`ai_skills_api.py`、`agent_debug_api.py`
（C10 可视化调试台：记忆图谱浏览/软删 Edge、Kanban 看板/步骤改写、self_model 查看修正）、
`budget_api.py`（按 Session 的 Token 预算限制，见下）。

### AI 预算限制子系统（`ai_core/budget/`）

按 Session（`global`/`group`/`member`/`user` 四维度，与 Session ID 语义对齐：群聊全群共享、
私聊按人）对 Token 设 **5h/天/周** 三档上限，支持白名单 + 主人豁免。三张表
（`AIBudgetRule`/`AIBudgetWhitelist`/`AIBudgetUsageRecord`，受总开关控制建表，已登记进
`AI_DATABASE_MODEL_MODULES`）。判定/记账走 `budget/manager.py::budget_manager`：
拦截在 `handle_ai.py::handle_ai_chat` 调 LLM **前**（`check`），记账在
`gs_agent.py::_execute_run` 记 Token **后**（`record_usage`，仅带 `ev` 的交互式 run）。
全局策略在 `budget/config.py`（`budget_config.json`）。前端 API 见
`webconsole/budget_api.py`（`/api/ai/budget/*`）与 `webconsole/docs/41-ai-budget.md`。

> 预算记账与统计共享同一 usage 来源（`result.usage()`），自身不解析流式 usage，
> 流式语义修复（§12.17）自动惠及预算。注意内存账本回载近 8 天
> `AIBudgetUsageRecord`：修复前被膨胀的历史流水在滚动窗口内仍参与限额，
> 必要时手工清理该表让用户额度立即恢复正常。

> 插件也可复用 `gsuid_core.webconsole.app_app.app` 挂自己的 `/api/<插件名>/...` 路由 +
> `Depends(require_auth)`。详见 `gscore-plugin-development` 的 FastAPI 插件 API 章。

### 配置/巡检的热重载特殊处理

`PUT /api/persona/{name}/config` 更新配置时：改 `ai_mode` 含"定时巡检"→ `start_heartbeat_inspector()`；
改 `inspect_interval` 且已启用巡检 → `inspector.stop_for_persona()` + `start_for_persona()`。其余
配置 `set_config` 写盘后下次自然生效。

## 11.3 网页控制台认证报文加密（2026-06-15，**强制、无开关**）

`webconsole/auth_crypto.py` 提供 **X25519 ECDH + HKDF-SHA256 + AES-256-GCM** 应用层混合加密：

- `GET /api/auth/pubkey` 下发服务端公钥 + `key_id`；登录/注册/改密接口经 `_decrypt_auth_body()`
  统一解密后再走业务。
- **强制、无明文兼容**：所有认证报文必须加密（`enc=true` + 握手字段），明文/`enc!=true` 一律
  被 `AuthCryptoError` 拒绝。**不再有** `REQUIRE_ENCRYPTED_AUTH` 配置。
- **目标**：纯 HTTP 部署（无 HTTPS）下消除"密码明文上链路"的被动嗅探。具前向保密（每次握手用
  前端临时密钥对）+ 防重放（`ts` 时间戳 120s 窗口）。**不防主动 MITM 篡改前端 bundle**（敌意
  网络仍需 HTTPS）。
- **两道纵深防护**：① 解密层 IP 限流（解密前先 check IP 限流窗口，失败 `record_failure` 计入该
  IP，连续异常封禁——封堵畸形报文 DoS/探测）；② 业务层限流（解密后仍跑 `login:`/`register:`/
  `password:` 限流）。
- **密钥轮换**：`register_key_rotation_job()` 把 `AuthKeyStore.rotate()` 接入 APScheduler，默认
  每 `KEY_ROTATION_INTERVAL_HOURS=12` 小时轮换，旧密钥保留一代以容忍轮换瞬间在途请求。

> ⚠️ **前端必须实现加密协议**：部署/升级时须确保前端 bundle 已落地加密实现（先取
> `/api/auth/pubkey` 再提交加密报文），否则登录/注册/改密一律被拒。`auth_keystore` 是单进程
> 密钥，多实例不共享。

## 11.4 数据库基类与 AI 表（`utils/database/base_models.py`）

GsCore 用 **SQLModel** 作 ORM。继承体系：

```
BaseIDModel              # 最基础，只有 id
  └── BaseBotIDModel     # + bot_id
        └── BaseModel    # + bot_id + user_id
```

**铁律**（与 `docs/LLM.md` §3 一致）：

1. **不写 `__tablename__`**：表名 = 类名全小写无下划线（`AiMemeRecord` → `aimemerecord`）。
   自定义约束/索引用 `__table_args__`。
2. **数据库方法写在模型类里**，用 `@with_session`（自动建 session / 提交 / 异常回滚 / 归还连接池）。
   签名第二参必须是 `session: AsyncSession`（紧跟 cls/self）。
3. 复杂场景手动管理用 `async_maker()`。
4. 全异步；CPU 密集用 `to_thread`。

```python
class CoreUser(BaseBotIDModel, table=True):
    @classmethod
    @with_session
    async def get_user_by_name(cls, session: AsyncSession, name: str) -> "CoreUser | None":
        stmt = select(cls).where(cls.name == name)
        return (await session.execute(stmt)).scalar_one_or_none()
```

**AI 表与总开关的关系（重要）**：`create_core_tables`（`on_core_start_before`，见 [§02](./02-startup-lifecycle.md)）
在 **AI 总开关关闭时跳过创建所有 AI 表**。所以关 AI 时 SQLite 不会出现任何 AI 相关表，省体积。
加 AI 表时记得挂到这条受总开关控制的建表路径，而非无条件建。

**Schema 升级（给已部署用户补列/加索引）**：走 `on_core_start_before` 的 `trans_adapter`（框架）
或插件的 `exec_list`，**执行 ALTER / CREATE INDEX**。绝不能在运行期临时 ALTER。

## 11.5 帮助系统

帮助系统让插件把命令登记成可视化帮助图/状态面板。给框架开发者的要点：

- `register_help(plugin_name, help_data)` / `get_new_help(...)`：登记并生成帮助图。
- `register_status(...)`：登记状态面板项。
- 详细用法（给插件作者）见 `gscore-plugin-development` 的帮助系统章
  （`references/08-help-system.md`）。

> 框架侧改帮助系统时注意：帮助数据是插件在模块加载期登记的（与触发器同期，见 [§03](./03-plugin-loading-and-config.md)
> 的 `cached_import`），渲染走图片渲染范式（PIL → htmlkit → playwright）。
