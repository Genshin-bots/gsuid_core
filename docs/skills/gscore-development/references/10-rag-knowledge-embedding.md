# 十、RAG 知识库与嵌入

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[九、记忆系统](./09-memory-system.md) · **下一章**：[十一、统计 / 控制台 / 数据库 / 帮助](./11-statistics-webconsole-database.md)

本章讲 RAG 知识库（`ai_core/rag/`）的存储与检索、知识 SQL 真值源 + 对账（2026-06-15）、
检索过滤下推修复，以及嵌入模型提供方抽象层。

## 10.1 RAG 不再前置强制检索（设计基线）

历史缺陷 D-11：原 `handle_ai_chat` 在所有意图下强制 RAG 检索拼进上下文——用户只说"你好"也要
向量化 + 查 Qdrant + 塞无关结果，多 1~2 秒延迟 + 无谓 Token。

**现状**：RAG 检索改为主 Agent 的 `buildin` 工具 `search_knowledge`，由 LLM 自主判断是否调。
`handle_ai_chat` 的 `rag_context` 只含历史对话上下文，**不再强制检索**。

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

## 10.6 周边 AI 接口

| 模块 | 文件 | 说明 |
|------|------|------|
| Image Understand | `image_understand/understand.py` | 统一图片理解。模型 `model_support` 含 `image` → 原生多模态转述；否则回退 MCP。**记忆摄入/视频帧/表情包打标等后台路径也调它，不经 `_prepare_user_message` 能力分支，故必须自身优先走原生多模态**，否则未配 MCP 时刷"图片理解失败" |
| Web Search | `web_search/search.py` | `web_search()` 按 `websearch_provider` 选 Tavily/Exa/MCP。MiniMax 搜索已迁移至 MCP（原 `minimax_search.py` 删除） |
| Meme 表情包 | `meme/` + `buildin_tools/meme_tools.py` | 采集/打标/检索/发送（`send_meme`/`collect_meme`/`search_meme`） |
