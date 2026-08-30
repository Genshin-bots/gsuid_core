# 十、RAG 知识库与嵌入

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[九、记忆系统](./09-memory-system.md) · **下一章**：[十一、统计 / 控制台 / 数据库 / 帮助](./11-statistics-webconsole-database.md)

本章讲 RAG 知识库（`ai_core/rag/`）的存储与检索、知识 SQL 真值源 + 对账（2026-06-15）、
检索过滤下推修复，以及嵌入模型提供方抽象层。

## 10.1 RAG 不再前置强制检索（设计基线）

历史缺陷 D-11：原 `handle_ai_chat` 在所有意图下强制 RAG 检索拼进上下文——用户只说"你好"也要
向量化 + 查 Qdrant + 塞无关结果，多 1~2 秒延迟 + 无谓 Token。

**现状**：主人格回想工具是 `search_cognition`，由 LLM 自主判断是否调。
`handle_ai_chat` 的 `rag_context` 只含历史对话上下文，**不再强制检索**。命中公共枢纽时，
工具回执带路径卡；问到某一栏且能唯一选定则同一次附全文（≤6000 字，仅 `kb_plugin` / `kb_kbdoc`；
`to_` 不展开正文）。
⑧ 闲聊不灌路径卡。

> 改 handle_ai 上下文拼装时不要把 RAG 检索改回前置强制。不要改冻结门面
> `cognition.facade.search_cognition(query, *, kinds, scope) -> List[CognitiveHit]` 的签名。

> 改 handle_ai 上下文拼装时不要把 RAG 检索改回前置强制。

## 10.2 知识库 SQL 真值源 + 批量导入 + 对账（2026-06-15）

**背景三痛点**：① 数十万字长文整段嵌入被模型 512 token 上限**静默截断**；② 手动知识仅存
Qdrant，换模型/目录损坏即**永久丢失**；③ Qdrant local 不支持 offset，列表 O(n) scroll。

**方案**：

- `AIKnowledgeChunk` 表（`ai_core/database/models.py`）= **手动知识的 SQL 真值源**（1 行 = 1 个
  Qdrant point）。用 `__table_args__` 定义索引，无 `__tablename__`。
- `rag/chunking.py` 长文分片（段落 → 句子 → 定长+重叠兜底），解决 512 token 截断。
- `rag/knowledge.py` 文档级导入/删除/导出/导入 + **两级对账**：
  - **启动期数量对账** `reconcile_manual_knowledge()`：回填"仅 Qdrant"旧知识到 SQL、从 SQL
    重嵌缺失分片（**数量一致则跳过逐条扫描**，轻量）。在启动链路自动跑。
  - **深度对账** `deep_reconcile_manual_knowledge()`：**逐条**按 `content_hash` 比对 SQL 与
    Qdrant，覆盖"数量相等但内容分叉"盲区（Qdrant 有/SQL 无→回填；SQL 有/Qdrant 无→重嵌；hash
    不一致→以 SQL 为真值源重嵌覆盖）。**仅运维手动触发，不在启动链路自动跑**。

WebConsole 鉴权接口（`knowledge_base_api.py`）：`/api/ai/knowledge/bulk`、`/doc/{doc_id}`、
`/backup/export`、`/backup/import`、`/reconcile`（深度对账）。文档见
`webconsole/docs/16-ai-knowledge.md`。

> ⚠️ **深度对账成本**：`/api/ai/knowledge/reconcile` 需全量 scroll Qdrant + 全表读 SQL + 必要时
> 批量重嵌，大知识库耗时较长，**仅作运维手动入口（非自动）**。

启动期 `reconcile_manual_knowledge()` 覆盖 `source=manual` **和** `source=agent`（Agent 用
`attach_article` 新建的文必须能换模型重嵌）。运维深度对账 `deep_reconcile_manual_knowledge`
同样扫这两源。启动挂载扫描本身**不**触发全库重嵌。

`sync_knowledge()` 把插件 `_ENTITIES` 同步进 Qdrant 之后，`init_ai_core` 在 READY 之后后台
`spawn_cognition_mount()`：插件 + 手动知识建公共枢纽（`writable=false`）；随后把
`source=agent` 文按 `hub:{正式名}` 标签挂回已有枢纽（`writable=true`，启动扫描禁止新建
`world:`）。`attach_article` / 网页搜索 query 在**写入当时**先查已有、过门才建。
插件正文**不复制**进 `aichunk`；全文句柄 `kb_plugin:{id}` 读注册表。手动/agent 文
`kb_kbdoc:{doc_id}` 按 `chunk_index` 拼接 SQL。开关 `cognition_mount_enable`。
控制台 JSONL `import_manual_knowledge` 写入成功后按 `doc_id` 即时挂载，不必等下次启动扫描。
落盘弱挂枢纽用搜索 `query:`（不是正文首行 `<search_results>`）：先查已有 title，
没有且过公共名词门则新建再挂；整页结果规则摘要写在 FileOS / 挂件上供下次回想。
**不要**拿工具名或 SERP 全文去配枢纽 title。
控制台 `rebuild_cognition_mount` 会先拍 Agent/网页挂件，插件回挂后再按 title 回挂；
不要改成只删 `world:` / 挂件，否则运行时自建枢纽会蒸发。

## 10.3 检索过滤下推 + 混合检索（2026-06-15，Bug 修复）

`buildin_tools/rag_search.py` 把 `plugin` / `category` 过滤**下推到 Qdrant 服务端**
（`query_filter`），而非取回 top-k 后客户端筛——修复"匹配项排在 top-k 之外被丢弃致召回偏少甚至
为空"的缺陷。

知识库已升级 **Dense + BM25 混合检索**（score 为 RRF 名次分**非余弦**），同时**移除按余弦阈值
的硬筛**避免误杀。

> 改 RAG 检索时记住：score 是 RRF 名次分，**不要**再按"余弦 ≥ 阈值"硬筛；过滤条件要下推到
> Qdrant `query_filter`，不要在客户端对 top-k 结果二次筛。

## 10.4 知识与别名注册（接口层）

- `ai_entity`：插件声明知识实体，**启动时自动同步**到向量库、`_hash` 检测增量更新。
- `add_manual_knowledge`：手动知识管理，不自动同步，需手动调向量库 API（其 SQL 真值源即
  `AIKnowledgeChunk`，见 §10.2）。
- `ai_alias`：别名注册，已接入记忆摄入链路（C2）——抽取时作为"本群已知别名"注入提取提示词、
  检索期用于查询展开与动态实体链接消歧；`scope="Genshin"` 隔离跨游戏同名别名。

> 详细签名（给插件作者）见 `gscore-ai-core-api` 的知识库与别名章。

## 10.5 嵌入模型提供方抽象层（`rag/embedding.py`）

把嵌入模型调用统一为 `EmbeddingProvider` 接口，支持本地 fastembed 与 OpenAI 兼容远程 API 自由
切换。由 `ai_config` 的 `embedding_provider`（`"local"` / `"openai"`）控制。

```
调用方（rag/tools.py · rag/knowledge.py · rag/image_rag.py · memory/vector/ops.py）
        ▼
EmbeddingProvider (ABC)
  embed_sync / embed_single_sync（同步）· embed / embed_single（async）· dimension
        ├── LocalEmbeddingProvider（fastembed + ONNX；同步线程池包装，异步 run_in_executor）
        └── OpenAIEmbeddingProvider（httpx → /v1/embeddings）
```

**配置**：

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `embedding_provider` | `"local"` | `local` / `openai` |
| `embedding_model_name`（local） | `"BAAI/bge-small-zh-v1.5"` | 本地嵌入模型 |
| `base_url`（openai） | `"https://api.openai.com/v1"` | API URL |
| `api_key`（openai） | `["sk-"]` | 密钥列表 |
| `embedding_model`（openai） | `"text-embedding-3-small"` | 嵌入模型 |

**向后兼容**：`rag/base.py` 的 `embedding_model` 全局变量用 `_EmbeddingModelWrapper` 包
`EmbeddingProvider`，保持原 `fastembed.TextEmbedding` 的 `.embed([text])` 接口，现有调用方无需
改。新增 `embedding_provider` 全局变量暴露底层实例供需要异步接口的模块（`memory/vector/ops.py`）用。

**插件扩展嵌入后端**：`register_embedding_provider` 注册 `EmbeddingProviderEntry`（懒 import +
工厂模式，重依赖只能在 `factory` 内 import）；配置指向的 provider 不可用时框架**自动降级回
local** 并记 error，不让 AI 核心整体挂掉。详见 `gscore-ai-core-api` 的嵌入 Provider 章。

WebConsole API：`/api/embedding_config/*`（provider / local / openai / summary），见
`webconsole/docs/27-embedding-config.md`。前端建议用 `GET .../summary` 一次取全部、按 `provider`
字段决定显示哪组表单。

### 10.5.1 本地嵌入的 CPU / 内存调优（`rag/embedding/local.py` + `base.py`）

**本地嵌入（fastembed ONNX）是记忆摄入吞吐与 CPU 占用的主瓶颈，不是 LLM。** 实测单条
~491-turn 的 haystack：**嵌入 ~68s（CPU-bound）vs 作答 LLM ~8s**（网络 I/O）——摄入慢/CPU 高
几乎全在嵌入。三个 env 旋钮（都有 CPU-friendly 默认，大机可上调换吞吐）：

| env | 默认 | 作用 |
|-----|------|------|
| `GSUID_EMBED_THREADS` | `max(1, cpu//2)` | fastembed ONNX intra-op 线程。旧默认 `min(cpu,8)` 会吃满全部核（小核机 CPU 常驻 100% 抢事件循环）；`cpu//2` 留一半余量，吞吐仅微降（bge-small 8→2 仅 1.37x） |
| `GSUID_EMBED_BATCH` | `64` | 单次推断 batch_size。fastembed 默认 256 会把驻留内存冲到 ~500MB；64 降到 ~300MB（2C2G 主要省内存点） |
| `GSUID_EMBED_BATCH_WORKERS` | `max(1, cpu//4)` | 批量执行器并行度（并发摄入的并行嵌入路数）。小机退 1 防过订阅 |

**内存真相（排障必读）**：
- **嵌入模型驻留 ~150MB（加载）→ 单批 ~300–500MB（含 onnxruntime arena + 中间张量）**；
  batch_workers=N 时按 N 倍放大。
- ⚠️ **onnxruntime 内存 arena 只增不减**：一次大 batch 后不释放，**峰值即稳态**——所以
  `GSUID_EMBED_BATCH` 降峰值是**永久生效**而非只压瞬时尖峰。这不是内存泄漏。
- **满配 core 空载 ~4.6GB 的大头是游戏插件（~4GB），不是嵌入**（精简 core 无插件仅 ~624MB）。
  跑测中 RSS 随题数稳定不上行（每题 episode/向量/session 逐题释放），排"记忆泄漏"先看是不是插件基线。

**2C2G / 小核机**：本地嵌入会周期性打满核，`embedding_provider=openai`（远程）几乎必选——
既消 CPU-bound 瓶颈又去掉最大动态内存；配 reranker 关（`enable_rerank=false`，本地 reranker ONNX
是另一大内存项）+ `qdrant_provider=remote`。**别在 2 核机设 `GSUID_EMBED_THREADS`**（默认 `cpu//2=1` 即对）。

## 10.6 周边 AI 接口

| 模块 | 文件 | 说明 |
|------|------|------|
| Image Understand | `image_understand/understand.py` | 统一图片理解。模型 `model_support` 含 `image` → 原生多模态转述；否则回退 MCP。**记忆摄入/视频帧/表情包打标等后台路径也调它，不经 `_prepare_user_message` 能力分支，故必须自身优先走原生多模态**，否则未配 MCP 时刷"图片理解失败" |
| Web Search | `web_search/search.py` | `web_search()` 按 `websearch_provider`（默认 **AnySearch**，可匿名）+ `websearch_lb_strategy` 调度 AnySearch/Firecrawl/Tavily/Jina/Exa/MCP；异常或空结果可换源。MiniMax 搜索已迁 MCP |
| Web Fetch | `web_fetch/__init__.py` | `fetch_webpage_as_markdown`：默认 Jina Reader（Key 可选）+ 备用 local；`webfetch_*` 与搜索同构多源策略 |
| Meme 表情包 | `meme/` + `buildin_tools/meme_tools.py` | 采集/打标/检索/发送（`send_meme`/`collect_meme`/`search_meme`） |
