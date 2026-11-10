# AGENTS.md

> 本文件遵循 [AGENTS.md](https://agents.md/)，取代已删除的 `docs/LLM.md`。**§1–§9 编号保持不变**（源码注释里的「§3.5.2」等仍指向这里）。
> 架构 / 触发链路 / 启动时序 / AI 子系统 / 已知坑按需读 [`.agents/skills/gscore-development/SKILL.md`](.agents/skills/gscore-development/SKILL.md)，不要一次把所有 `references/` 塞进上下文。源码是唯一事实源。

## Project overview

GsCore（早柚核心 / `gsuid-core`）是 **FastAPI + WebSocket + APScheduler** 单进程服务（默认端口 `8765`，WebConsole `http://<HOST>:<PORT>/app`，Python `>=3.11,<4.0`）：

- **不是 Bot**：不直连聊天平台，需配合 NoneBot2 / AstrBot / Koishi 等上游适配器连 `/ws/{bot_id}` 收发 `MessageReceive` / `MessageSend`。
- **插件宿主**：业务在 `gsuid_core/plugins/`，`@sv.on_xxx` 在 import 时登记触发器。
- **AI 中枢**：无命令匹配时由 `gsuid_core/ai_core/` 接管（Persona、工具、记忆、RAG、巡检、长任务）。

## Repository map

```
.
├── AGENTS.md                 # Agent 必读（红线 + 结构 + 命令）
├── pyproject.toml / ruff.toml / pyrightconfig.json
├── .agents/skills/           # 编码 Agent 开发指南（非模型运行时 Skill）
├── docs/                     # 已落地的专题说明（改对应机制后同步更新）
├── plans/                    # 新方案设计文档写这里（.gitignore，单次一份，不进 docs/）
├── gsuid_core/               # 框架包
│   ├── core.py / supervisor.py / app_life.py / server.py / gss.py / meta_plugins.py
│   ├── handler.py / bot.py / sv.py / trigger.py / models.py / config.py
│   ├── utils/                # database / image / plugins_config / …
│   ├── webconsole/           # 网页控制台 API + dist（禁止编译覆盖 dist，见 §1.12）
│   ├── plugins/ / buildin_plugins/
│   └── ai_core/              # startup / handle_ai / ai_router / gs_agent / agent_run /
│                             # register / output_gate / context_assembly / control/lines.py /
│                             # memory / rag / persona / heartbeat / planning
├── tests/                    # pytest（离线、无 LLM、总数 ≤ 200）
├── eval/                     # 实机评测（打运行中的 core）
├── changelogs/               # CHANGELOG.md 索引 + <版本>.md
└── data/                     # 运行时生成（配置 / DB / 日志 / AI 状态，不进 git）
```

> **运行时 Skill**（模型 `list_skills` / `run_skill_script`）在 `data/ai_core/skills/` 或插件 `skills/`，不是 `.agents/skills/`。

## Skills

| 任务 | Skill |
|------|--------|
| 改框架核心（handler / ai_core / 启动 / 配置 / 数据库基类 / webconsole） | [gscore-development](.agents/skills/gscore-development/SKILL.md) |
| 写业务插件、触发器、`to_ai`、帮助、配置、库表 | [gscore-plugin-development](.agents/skills/gscore-plugin-development/SKILL.md) |
| 查 AI Core 给插件的 API 签名 | [gscore-ai-core-api](.agents/skills/gscore-ai-core-api/SKILL.md) |
| 写平台适配器（WS 协议 / MessageReceive） | [gscore-adapter-development](.agents/skills/gscore-adapter-development/SKILL.md) |
| 部署 / Docker / WebConsole / WS_TOKEN | [gscore-deploy](.agents/skills/gscore-deploy/SKILL.md) |
| 写用户可见文字（更新日志 / 文档 / 报错 / 注释） | [human-talk](.agents/skills/human-talk/SKILL.md) |
| 编写、审查或清理单测 | [test-audit](.agents/skills/test-audit/SKILL.md) |

## Setup & 交付闸（改完必须全绿）

一律在仓库根目录执行。`dev` 组不是默认组，开发者必须带 `--dev`（生产环境用 `uv sync --no-dev`）：

```sh
# 安装与启动
uv sync --python 3.12 --dev
uv run core                                # --dev 只加载 -dev 插件；--host / --port 临时覆盖

# 交付闸（向用户宣告完成前必须全绿，缺一不可）
uv run --dev ruff check gsuid_core tests eval
uv run --dev ruff format --check gsuid_core tests eval
uv run --dev pytest tests -q
uv run --dev basedpyright
```

- **实机评测**：碰装配 / 闸门 / 每轮注入 / 启动顺序时，除单测外须对照 `eval/agent` 群聊基准（打已启动且加载全部插件的 core，不带 `--dev`）。
- **锁文件**：以 `uv.lock` 为准；重解 `poetry.lock` 固定用 `uv tool run --from "poetry==2.5.1" poetry lock` 并跑 `poetry check --lock`。
- **安全**：公网必配 `WS_TOKEN` 或 `TRUSTED_IPS`；`REGISTER_CODE` 仅限单管理员（ECDH + AES-GCM）；密钥不进仓库或 `data/`。

## Testing & CHANGELOG

**Testing**：
- `tests/test_*.py` 必须离线、无 LLM；需要运行中 core 的脚本放 `eval/manual/`（无 `test_` 前缀）或 `eval/agent/`。
- **规模与防线**：`tests/` 总用例数**严格 ≤ 200**；单次 bug 排查的临时脚本验证后**即删**，禁止提交 `test_issueXXX.py`；核心防线断言**就近合并**到现有模块。
- **专项必跑**：改交互脚手架跑 `tests/test_interaction_scaffold.py`；改工具 docstring / covers 跑 `tests/test_ai_tool_docstrings.py`；改日志或 locale 跑 `tests/test_logger_i18n.py`（`t()` / `i18n_t()` 首参必须是静态 key `log.<module>.<semantic>`，禁三元/拼接/变量）。

**CHANGELOG（`changelogs/<版本>.md`）**：
- **准入**：只记用户可感知变化（新能力/命令/配置、对外行为变化、破坏性变更、可量化性能变化）；修错字、内部重构、类型收窄、补测试/注释等**不记**。
- **就近合并**：同一功能多次修改必须合并进已有条目并追加 commit 短号，单次任务最多新增/修改 1~2 条。
- **写法**：按 `human-talk` 受控中文，每条正文 ≤ 80 字（严禁超 120 字），只写影响与用法，禁列代码细节。
- **链接与回填**：格式 ``[`短号`](https://github.com/Genshin-bots/gsuid_core/commit/短号)``；未提交可先不挂链接，**禁止写 `[待补]` 等占位符**；每次动 `changelogs/` 时回填已提交条目的短号。

---

## 一、绝对红线（Strict Red Lines）

### 1.1 禁止使用 try-except 兜底
遇到类型或属性问题必须从类型标注和逻辑解决，禁止吞异常。唯一例外：解析 LLM 自由文本 / Qdrant payload / 加密报文等不可信外部输入，及插件观测性的 `_ai_return_xxx()`。

### 1.2 禁止使用 `cast()` 类型强制转换
`cast` 是类型欺骗。遇到类型冲突应使用 `A | B` + `isinstance` 守卫或调整函数签名。

### 1.3 禁止使用 `type: ignore` 抑制类型检查
仅在第三方库类型标注错误且无法绕过时作最后手段，**严禁掩盖自身代码类型问题**。

### 1.4 禁止使用 `getattr` / `dict.get` 等兜底语法
明确定义 `TypedDict` / `@dataclass` / 泛型，或用 `isinstance` / `in` 守卫后直接访问属性与键。

### 1.5 遇到类型标红的解决思路
`标红 → 分析原因 → 检查类型定义与逻辑漏洞 → Union + isinstance → 最后才考虑 assert`。

### 1.6 禁止冗长注释（`#` 注释最多 2 行，每行 ≤ 88 字符）
代码能自解释的不写注释；要写只写「为什么 / 有什么坑 / 边界条件」，不复述代码。两行讲不清说明应拆函数或改命名。

### 1.7 前缀缓存红线（AI 会话）
改 `ai_core` 会话 / Agent loop / history 时严禁破坏 provider 前缀缓存：
1. **禁止中途改写 `system_prompt`**。当轮系统提醒只追加到当前 request 的 `UserPromptPart`，落盘前剥除，正文只在 `control/lines.py` 定义（§1.11）。
2. **禁止砍 history 头部**（如 `history[-n:]`），须用 `compact_session_history` 保头裁中段。
3. **动态 per-turn 内容**（mood / 关系 / 记忆 / 精确时间）一律进 **user 侧**。

### 1.8 禁止 `Any`：运行时变量必须有完整、可追踪的类型
禁止引入或传播 `Any`（含 `list[Any]`、`dict[str, Any]`、`Callable[..., Any]`、裸 `dict`/`list`/`object` 万能袋）。结构化字典用 `TypedDict`，状态用 `@dataclass`，接口用 `Protocol`；第三方返回 `Any`（如 SQLAlchemy `Result[Any]`）必须在调用点立刻用 `isinstance` 收窄。测试假对象用 `Protocol` 或显式 stub 类，禁止 `MagicMock` + `Any`。

### 1.9 禁止人格锁定 / 能力锁定（通用框架）
- **人格锁定（禁止）**：框架代码、闸门、脚手架、分类器、提示词**不得**写死特定角色姓名、口癖、自称或梗；口癖从当前人格卡动态解析。**框架不替人格说话**：命中闸门一律让当前人格重说，**严禁任何预设罐头文案**；两次仍不净时人格/一致性类原样发送，`fund_claim` / `machine_dump` 与主动沉默才发 `<SILENCE>`。
- **能力锁定（禁止）**：框架路由 / 规划 / 提示词**不得**内置业务垂直词表（如游戏圣遗物/命座、股票模拟盘等）。垂直能力只由插件通过 `@ai_tools(covers=..., aliases=..., capability_domain=...)` / `ai_entity` / `ai_alias` 自声明。

### 1.10 未过交付闸禁止宣告完成
ruff check / ruff format / pytest / basedpyright（含 i18n 守门）全绿前禁止宣告完成。

### 1.11 当轮控制句只定义一次（`control/lines.py`）
插入当前 request user 侧、落盘前剥除的控制句与寻址记号（`（系统：`、`⛔`、`GATE_NUDGE_MARKERS`、`--- 消息 ---` 等）只写在叶子模块 `gsuid_core/ai_core/control/lines.py`，调用处 `import` 引用，禁止散落硬编码字面量。

### 1.12 禁止将前端产物编译复制至 dist 目录（构建隔离红线）
**绝对禁止**从前端仓库（如 `gsuid_hub`）执行 `build` / `build:core` 并将产物输出或复制到 `gsuid_core/webconsole/dist/`。修改前端时只维护前端工程源码（`src/`），严禁向 `gsuid_core` 写入或提交任何编译生成的 minified js/css/br/gz 产物。

### 1.13 测试用例与数据全面脱敏（隐私安全红线）
**绝对禁止**在 `tests/`、`eval/` 或 fixture / 断言中混入真实群友昵称、真实用户 ID、真实群号或线上群聊原文。统一使用虚拟数据（如「测试用户A」、`100000001`、`group_1`、「测试项目提问」）。

---

## 二、类型提示规范

- **2.1 完全类型提示**：所有函数参数与返回值必须有具体可追踪的类型注解（写 `: Any` 视为未写）。
- **2.2 Union 与 Optional**：多类型用 `str | int`，可空用 `str | None`。
- **2.3 复杂结构**：字典结构用 `TypedDict`，配置与状态用 `@dataclass`。
- **2.4 多类型冲突**：用 `Union` + `isinstance` 分支守卫（如 `if isinstance(result, dict): ...`），仅在运行时确定不变量时谨慎用 `assert isinstance(...)`。

---

## 三、数据库操作规范

- **3.1 基类与表名**：继承 `gsuid_core/utils/database/base_models.py` 中的 `BaseIDModel` → `BaseBotIDModel` → `BaseModel`。类名用 PascalCase，表名自动推导为全小写无下划线（如 `AIMemEpisode` → `aimemepisode`），**严禁定义 `__tablename__`**（索引约束用 `__table_args__`）。
- **3.2 会话装饰器**：写/改/删挂 `@with_session`，纯查询挂 `@with_read_session`，方法第二参数固定为 `session: AsyncSession`。
- **3.3 手动事务**：类外复杂事务用 `async with async_maker() as session:`。
- **3.4 方法内聚**：表相关的数据库操作必须封装在对应模型类的 `@classmethod` 中。
- **3.5 SQLModel / SQLAlchemy 类型安全写法**：
  - **3.5.1 `col()` 包裹列**：`where` / `order_by` / `group_by` 中的字段比较一律用 `col(cls.field) >= val`，避免被推导为 `bool`。
  - **3.5.2 `CursorResult` 守卫**：取 `rowcount` 一律写 `deleted = result.rowcount if isinstance(result, CursorResult) else 0`。
  - **3.5.3 禁止变长 splat**：禁止 `select(*dynamic_cols)`，按分支写出列数确定的 `select(col(cls.a), col(cls.b))`。

---

## 四、异步编程规范

- **4.1 全异步**：所有 I/O 或可能阻塞的方法必须定义为 `async def`（如用 `httpx.AsyncClient` 而非 `requests`）。
- **4.2 同步桥接**：CPU 密集或无法异步的同步函数用 `from gsuid_core.pool import to_thread` 的 `@to_thread` 包装后 `await` 调用。

---

## 五、项目核心模块概览

- **5.1 `gsuid_core/ai_core/`**：含 `memory/`（记忆图谱与偏好）、`rag/`（知识库）、`scheduled_task/`（定时任务）、`persona/`（人设）、`control/`（控制面，`lines.py` 见 §1.11）、`history/`（会话历史）、`web_search/` 与 `web_fetch/`。
- **5.2 `gsuid_core/webconsole/`**：FastAPI 路由，鉴权依赖 `Depends(require_auth)`（来自 `gsuid_core.webconsole.auth_api`）。
- **5.3 `gsuid_core/utils/`**：含 `database/`、`image/`、`api/`、`plugins_config/`、`upload/`。

---

## 六、配置管理

- **6.1 插件配置**：通过 `gsuid_core/utils/plugins_config/gs_config.py`（`GsStrConfig` / `GsIntConfig` / `GsBoolConfig` / `GsListStrConfig` 等）声明配置项。
- **6.2 资源路径**：统一用 `from gsuid_core.utils.resource_manager import get_res_path` 获取 `data/` 下路径。

---

## 七、日志规范

使用 `from gsuid_core.logger import logger`；框架核心日志必须走 `t()` / `i18n_t()` 且第一参数为静态 key（`log.<module>.<semantic>`），过 `tests/test_logger_i18n.py`。

---

## 八、`Bot` 与 `_Bot` 类区分

- **8.1 `_Bot(_id: str, ws: Optional[WebSocket] = None)`**：底层 WebSocket 连接与发送队列实现，不绑定 `Event`，供框架内部连接管理与 ` target_send()` 使用。
- **8.2 `Bot(bot: _Bot, ev: Event)`**：高层业务包装器，强依赖 `Event`，提供 `send()`、`receive_resp()`，供插件触发器、AI Agent 和 `MockBot` 使用。
- **8.3 / 8.4 禁止混用**：在需要 `Bot` 的地方严禁直接传 `_Bot("...")`，必须构造 `Bot(_Bot("..."), ev)`。

---

## 九、总结（编码优先级）

1. **红线先行**：无兜底（§1.1–§1.4）、无 `Any`（§1.8）、无人格/能力锁定（§1.9）、**不编译覆盖 `webconsole/dist`（§1.12）**、测试全脱敏（§1.13）。
2. **缓存与控制句**：不改 `system_prompt`、不砍 history 头（§1.7），当轮控制句只写 `control/lines.py`（§1.11）。
3. **数据库与异步**：无 `__tablename__`、`@with_session` / `@with_read_session`、`col()` + `CursorResult`（§3），全链路 `async def`（§4）。
4. **注释与文档**：`#` 注释 ≤ 2 行 × 88 字（§1.6）；新设计写 `plans/`；按标准更新 `changelogs/`。
5. **交付闸必过**：`ruff check` / `ruff format --check` / `pytest tests -q` / `basedpyright` 全绿后才宣告完成（§1.10）。
