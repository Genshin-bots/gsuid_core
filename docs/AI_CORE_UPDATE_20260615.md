# GsCore AI 子系统更新说明（2026-06-15）

> 本文档说明 2026-06-15 这一批针对 `ai_core` AI 子系统与网页控制台安全的更新：
> **变更内容、设计目标、关键约束、潜在风险与后续计划**。
>
> 源码为唯一事实源；设计稿位于未入库的 `plans/`
> （`procedural_preference_memory_design_20260614.md`、`rf_mem_dual_process_retrieval_assessment_20260614.md`、
> `knowledge_base_bulk_import_assessment_20260614.md`）。

---

## 目录

1. [变更总览](#1-变更总览)
2. [各特性详解](#2-各特性详解)
3. [代码规范与类型](#3-代码规范与类型)
4. [潜在风险](#4-潜在风险)
5. [后续计划](#5-后续计划)
6. [验证情况](#6-验证情况)

---

## 1. 变更总览

本次更新由 7 块工作组成，分属 **安全加固**、**记忆系统演进**、**RAG 知识库工程化**、**触发链路体验** 四类：

| # | 特性 | 默认状态 | 核心文件 | 影响面 |
|---|------|---------|---------|--------|
| A | 网页控制台认证报文加密（**强制**） | 强制开启、无开关 | `webconsole/auth_crypto.py`、`tests/test_auth_crypto.py` | 登录/注册/改密 |
| B | 程序性 / 偏好记忆 | **开** | `memory/ingestion/tool_trace.py` 等 | 记忆摄入 / 检索注入 |
| C | RF-Mem 双过程检索 | 关 | `memory/retrieval/familiarity.py` | 记忆检索深度路由 |
| D | 免唤醒续聊窗口（软触发） | 开（60s 窗口） | `ai_core/followup_window.py` | 群聊触发链路 |
| E | 知识库批量导入 + SQL 真值源 + 深度对账 | 开 | `ai_core/rag/chunking.py` | RAG 知识库 |
| F | `extract_json_from_text` 重写 | —（行为兼容） | `tests/test_extract_json.py` | 全 AI 链路 JSON 解析 |
| G | RAG 搜索过滤下推修复 | —（Bug 修复） | `buildin_tools/rag_search.py` | 知识检索召回 |

---

## 2. 各特性详解

### A. 网页控制台认证报文加密（强制）

**实现**：`webconsole/auth_crypto.py` 提供 **X25519 ECDH + HKDF-SHA256 + AES-256-GCM** 应用层混合
加密。`GET /api/auth/pubkey` 下发服务端公钥与 `key_id`；登录 / 注册 / 改密接口经
`_decrypt_auth_body()` 统一解密后再走业务逻辑。

**强制、无开关、无明文兼容**：所有认证报文必须为加密形态（`enc=true` + 握手字段），明文报文
（或 `enc!=true`）一律被 `AuthCryptoError` 拒绝。不再有 `REQUIRE_ENCRYPTED_AUTH` 配置——加密是
唯一形态。

**目标**：在纯 HTTP 部署（无 HTTPS 证书运维）下，彻底消除"密码明文上链路"的被动嗅探风险。具备
前向保密（每次握手用前端临时密钥对）与防重放（`ts` 时间戳 120s 窗口）。明确不防主动 MITM 篡改前端
bundle（与自签名 HTTPS 同等 TOFU 局限）。

**两道纵深防护**：
- **解密层 IP 限流**：`_decrypt_auth_body` 解密前先 `check` 该 IP 的解密限流窗口（已封禁直接拒、
  连 ECDH 都不做），解密失败（畸形 / 重放 / 明文）`record_failure` 计入该 IP，连续异常即封禁——
  封堵以畸形报文做 DoS / 探测。
- **业务层限流**：各 handler 在解密后仍执行既有的 `login:` / `register:` / `password:` 限流，作用
  在真实业务结果上。

**密钥轮换**：`register_key_rotation_job()` 把 `AuthKeyStore.rotate()` 接入 APScheduler，默认每
`KEY_ROTATION_INTERVAL_HOURS=12` 小时轮换一次服务端密钥对，旧密钥保留一代以容忍轮换瞬间在途请求，
进一步加固前向保密。

**完整度**：协议实现 + 20 条回归测试（`tests/test_auth_crypto.py`，覆盖加解密往返、明文/`enc!=true`
拒绝、篡改 / 伪造 / 重放、缺字段、非对象载荷）+ 文档（`webconsole/docs/01-auth.md`）。

### B. 程序性 / 偏好记忆（Procedural / Preference Memory，默认开）

与 Episode/Entity/Edge 三层陈述性记忆正交，新增 `AIMemPreference` 表（SQL-only、不写向量），承载
"针对 Agent 未来行为的纠正 / 偏好规则"。链路：

- **门控探测**（`memory/observer.py`）：纯规则零 LLM 的 `detect_correction_intent()` 命中纠错意图 →
  强制 `HIGH` 价值 + 触发即时 flush。
- **蒸馏门控**：由实体抽取 LLM 顺手判的 `pref` 布尔位裁决（`prompts/extraction.py` 的
  `PREFERENCE_FLAG_INSTRUCTION`），命中才跑第二次独立蒸馏 LLM（`worker._extract_and_upsert_preferences`）。
- **轨迹背景**（`memory/ingestion/tool_trace.py`）：有界 ring buffer 记最近工具调用，供蒸馏把
  "参数传错了"蒸成带具体参数的规则。
- **写入**：`AIMemPreference.upsert()`（语义等价强化 / 极性反转软停用 / 新建）。
- **注入**（`memory/retrieval/dual_route.py`）：检索时 SQL 精确取活跃规则，置顶强约束注入。
- **选择性注入（精确能力域过滤）**：`handle_ai` 按**意图门**（纯闲聊不注入）+ **能力域过滤**传参，
  能力域信号 = `_relevant_preference_contexts(query)` 子串近似 **∪
  `session.get_assembled_capability_domains()`**——后者是 gs_agent **上一轮实际装配工具**的能力域
  （精确"装配后回传"，见下）。纠错规则与 `general` 通用规则永远注入。
- **生命周期**（`lifecycle/consolidation_worker.py`）：按 salience 裁剪，纠错类受保护。
- **清空联动**（`clear_ops.py`）：清空用户记忆时一并删除偏好规则。

**能力域精确化（"装配后回传"）**：`GsCoreAIAgent` 在 `run()` 装配工具后，把本轮装配工具的
`capability_domain` 集合回填到 `self._last_assembled_domains`，并经
`get_assembled_capability_domains()` 暴露。`handle_ai` 在下一轮检索时读回该集合，与 query 子串近似
取并集作为 `preference_contexts`——只注入"本轮可用工具"相关的软偏好，避免无关规则挤占预算 / 分散
工具调用注意力。

**目标**：让用户对助手行为的纠正（"以后画图用竖图""按我时区"）转化为可持续遵守的硬约束，解决
"纠正完下一轮又犯"的体验问题。

### C. RF-Mem 双过程检索（默认关）

`memory/retrieval/familiarity.py` 把认知科学"回忆-熟悉度双过程理论"接入：

- **熟悉度探针**（`vector/ops.probe_episode_scores`）：一次纯 dense 查询取真实余弦分，算均分 s̄ +
  列表熵 H(p)，逐查询决定"检索多深"，把 System-2 从全局静态开关降为"按不确定性触发"。
- **回忆环**（零 LLM 的 KMeans + α-mix 多轮向量深检索）：低熟悉且 System-2 未触发时补召回，并把
  召回 Episode 链**关系投影**成精准 Edge 事实。KMeans 走专用线程池，不阻塞事件循环。

**默认关**：阈值（`familiarity_theta_*` / `tau`）需按嵌入模型离线标定后再放量；回忆环强绑
`qdrant_provider=remote`（本地嵌入式 Qdrant 是 O(N) 暴力扫，多轮会成倍放大成本）。

### D. 免唤醒续聊窗口（软触发，默认开）

`ai_core/followup_window.py`（纯进程内存 + TTL 惰性清理）。`handler.py` 在硬触发（@/关键词/私聊）
登记窗口起点；未硬触发时，若用户处于窗口内且为群聊里未 @ 别人的普通发言，按"软触发"放行
（`trigger_type="followup"`）。软触发消息在 `handle_ai` 先过一道**沉默门**
（`heartbeat/decision.run_reactive_gate`，复用 Heartbeat 轻量结构判断"是否仍在跟我说话"），与 AI
无关则直接沉默、不进主链路。三条硬规则：窗口从硬触发起算、续聊不续费、硬天花板。配置
`follow_up_window`（默认 60s）、`follow_up_max_total`（默认 300s）；`statistics` 触发分布新增
`followup` 维度。

### E. 知识库批量导入 + SQL 真值源 + 深度对账

- `AIKnowledgeChunk` 表（`ai_core/database/models.py`）= **手动知识的 SQL 真值源**（1 行 = 1 个 Qdrant
  point）。
- `ai_core/rag/chunking.py` 长文分片（段落→句子→定长+重叠兜底）。
- `rag/knowledge.py` 文档级导入 / 删除 / 导出 / 导入，以及两级对账：
  - **启动期数量对账** `reconcile_manual_knowledge()`：回填"仅 Qdrant"旧知识到 SQL、从 SQL 重嵌缺失
    分片（数量一致则跳过逐条扫描，轻量）。
  - **深度对账** `deep_reconcile_manual_knowledge()`：**逐条**按 `content_hash` 比对 SQL 与 Qdrant，
    覆盖"数量相等但内容分叉"盲区（Qdrant 有/SQL 无 → 回填；SQL 有/Qdrant 无 → 重嵌；hash 不一致 →
    以 SQL 为真值源重嵌覆盖）。仅供运维手动触发，不在启动链路自动跑。
- `knowledge_base_api.py` 新增鉴权接口：`/api/ai/knowledge/bulk`、`/doc/{doc_id}`、`/backup/export`、
  `/backup/import`、`/reconcile`（深度对账），文档见 `webconsole/docs/16-ai-knowledge.md`。

**目标**：① 数十万字长文整段嵌入被模型 512 token 上限静默截断 → 分片；② 手动知识仅存在于 Qdrant、
换模型/目录损坏即永久丢失 → SQL 真值源 + 对账；③ Qdrant local 不支持 offset 致列表 O(n) scroll →
SQL 原生分页。

### F. `extract_json_from_text` 重写

`ai_core/utils.py` 由"正则 + repair"重写为"**括号配平** + repair"：跳过字符串字面量内括号与转义、
正确处理嵌套结构、先严后宽（合法 JSON 直接 `json.loads` 不经 repair）。该函数被记忆抽取、Heartbeat
决策、软触发沉默门、偏好蒸馏多处复用。32 条回归测试（`tests/test_extract_json.py`）。

### G. RAG 搜索过滤下推修复（Bug 修复）

`buildin_tools/rag_search.py` 把 `plugin` / `category` 过滤**下推到 Qdrant 服务端**（`query_filter`），
而非取回 top-k 后客户端筛——修复"匹配项排在 top-k 之外被丢弃致召回偏少甚至为空"的缺陷。知识库已升级
Dense+BM25 混合检索（score 为 RRF 名次分非余弦），同时移除按余弦阈值的硬筛避免误杀。

---

## 3. 代码规范与类型

本批次遵循 `docs/LLM.md` 的红线与类型规范：

- **完全类型提示**：函数签名参数 / 返回值类型注解齐全；结构化数据统一用 `TypedDict` / `@dataclass` /
  `NamedTuple`（`CandidatePoint`、`PreferencePrompt`、`FamiliaritySignal`、`ToolCallRecord`、
  `ExtractedResult`、`_BurstState` 等）。`CandidatePoint` 定义在生产侧 `memory/vector/ops.py`，
  `dense_search_episodes_with_vectors` 精确返回 `list[CandidatePoint]`，回忆环按同一契约消费。
- **§1.3（type: ignore）**：消除新代码中的 `type: ignore`——`_build_named_point` 精确标注命名向量
  映射对齐 Qdrant `VectorStruct`；`_chunk_embed_text` 直接读 `AIKnowledgeChunk` 字段拼接，不再向
  TypedDict 形参传裸 dict。
- **§1.4（getattr / dict.get 兜底）**：新代码统一用 `key in d` / `isinstance` 守卫显式取值替代
  `.get(默认)` / `getattr(默认)` / `.pop(默认)`；认证报文字段经 `_str_field`、外部 payload / 导入记录
  经 `_opt_field` 收窄。
- **SQLModel 规范**：`AIMemPreference` / `AIKnowledgeChunk` 用 `__table_args__` 定义索引，无
  `__tablename__`。
- **异步规范**：全异步；KMeans / BM25 等 CPU 密集运算放入专用 `ThreadPoolExecutor`。
- 边界容错（解析 LLM 自由文本 / Qdrant payload / 加密报文等不可信外部输入）按需保留
  `try/except`（§1.1）与必要的 `cast`（§1.2，如 `auth_crypto` 收敛 `json.loads` 的 `Any`），用于
  保证"绝不打断主链路"。

---

## 4. 潜在风险

1. **前端必须实现加密协议**：加密为强制、无明文兼容，前端登录 / 注册 / 改密必须先取 `/api/auth/pubkey`
   再提交加密报文，否则会被一律拒绝。部署 / 升级时须确保前端 bundle 已落地加密实现；加密协议细节文档
   （`docs/WEBCONSOLE_AUTH_ENCRYPTION.md`，供前端对接）需补齐。
2. **认证加密的安全边界**：仅防被动嗅探，**不防主动 MITM 篡改前端 bundle**。敌意网络面临主动攻击者时
   仍需 HTTPS。
3. **偏好记忆默认开的成本**：开箱即启用工具轨迹记录、纠错探测、第二次蒸馏 LLM 调用与置顶强约束注入。
   首次放量建议观察：第二次蒸馏的 Token 成本、误抽偏好以强约束置顶注入可能过度约束工具调用（已有
   WebConsole 软停用 + salience 裁剪 + 精确能力域过滤兜底）。
4. **续聊软触发的误打扰与成本**：默认 60s 窗口内每条群消息触发一次沉默门 LLM 判定，群活跃时有额外 LLM
   开销。沉默门异常 / 解析失败一律放行交主 Agent 兜底，极端情况下可能多接几句。窗口 / 天花板按群活跃度
   观察调整。
5. **RF-Mem 阈值未标定即放量的召回退化**：阈值为论文英文模型经验值，中文本地模型通常需平移。已默认
   关闭并在配置描述标注"需标定"，风险可控。
6. **深度对账成本**：`/api/ai/knowledge/reconcile` 需全量 scroll Qdrant + 全表读 SQL + 必要时批量重嵌，
   大知识库耗时较长，仅作运维手动入口（非自动）。
7. **进程内存状态的多实例隐患**：`followup_window`、`tool_trace`、`auth_keystore`、偏好即时 flush 去抖
   均为进程内存 / 单进程密钥，多进程 / 多实例水平扩展时状态不共享（当前单进程事件循环模型下符合预期）。

---

## 5. 后续计划

1. **前端加密对接文档与实现**：补齐 `docs/WEBCONSOLE_AUTH_ENCRYPTION.md`（协议 / 字段 / 示例），并完成
   前端加密 bundle，配合强制加密落地。
2. **RF-Mem 阈值离线标定工具**：按嵌入模型采样 s̄ 分布（取 P25/P75）自动建议 `theta_low/high`，降低放量
   门槛；标定后再考虑默认开启熟悉度路由。
3. **续聊沉默门降级**：群极活跃时，评估用更廉价的规则 / 小模型替代每条软触发的 LLM 门判定。

---

## 6. 验证情况

- **回归测试**：`tests/test_auth_crypto.py`（20）+ `tests/test_extract_json.py`（32）= **52 passed**
  （Python 3.13.1 / pytest 8.4.2）。
- **静态编译**：本批次改动 + 新增的 Python 文件全部通过 `python -m py_compile`；关键模块
  （`auth_crypto`、`rag.knowledge`、`database.models`、`followup_window`、`tool_trace`、`familiarity`、
  `vector.ops`）导入与基本行为冒烟通过；密钥轮换 job 已确认按 `interval[12:00:00]` 注册到调度器。
- **未覆盖项（建议补测）**：偏好 `upsert` 的合并/极性反转、`followup_window` 的窗口/天花板时序、
  `chunking.split_text` 的分片边界、`deep_reconcile_manual_knowledge` 的回填/重嵌/hash 修正——目前依赖
  运行期验证，尚无独立单测。
