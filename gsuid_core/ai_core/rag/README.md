# RAG 模块文档

基于向量数据库的 RAG（检索增强生成）功能模块，提供工具检索和知识库查询能力。

## 文件结构

```
rag/
├── __init__.py      # 模块导出
├── base.py          # 全局变量、配置、工具函数
├── tools.py         # 工具向量存储
├── knowledge.py      # 知识库同步与查询
├── reranker.py      # 结果重排序
├── init.py          # 模块初始化
└── README.md        # 文档
```

## 架构说明

### 职责划分

| 文件 | 职责 |
|------|------|
| `base.py` | 全局变量、配置、工具函数 |
| `tools.py` | 工具入库（`sync_tools`）和检索（`search_tools`） |
| `knowledge.py` | 知识入库（`sync_knowledge`）和检索（`query_knowledge`） |
| `reranker.py` | 结果重排序 |
| `init.py` | 统一初始化入口（`init_all`） |

### 增量同步机制

启动时自动同步数据到向量库，保证本地数据与向量库一致：

1. **新增**：本地有，向量库无 → 入库
2. **更新**：本地有，向量库有但hash不同 → 更新
3. **删除**：本地无，向量库有 → 清理

### 全局变量（base.py）

- `embedding_model` - TextEmbedding实例
- `client` - AsyncQdrantClient实例
- `enable_ai` - AI功能总开关
- `enable_rerank` - Rerank功能开关

## 模块详情

### base.py

**常量：**
- `DIMENSION` - 向量维度（512）
- `EMBEDDING_MODEL_NAME` - Embedding模型名称
- `MODELS_CACHE` - 模型缓存目录
- `DB_PATH` - Qdrant本地数据库路径
- `RERANK_MODELS_CACHE` - Reranker模型缓存目录
- `RERANKER_MODEL_NAME` - Reranker模型名称
- `TOOLS_COLLECTION_NAME` - 工具集合名称
- `KNOWLEDGE_COLLECTION_NAME` - 知识集合名称

**函数：**
- `init_embedding_model()` - 初始化Embedding模型和Qdrant客户端
- `get_point_id(id_str)` - 生成唯一UUID
- `calculate_hash(content)` - 计算内容MD5哈希

### tools.py

**类型：**
- `ToolBase` - RAG工具基础类型，包含 `name` 和 `description`

**函数：**
- `init_tools_collection()` - 初始化工具向量集合
- `sync_tools(tools_map)` - 同步工具到向量库（增量更新）
- `search_tools(query, limit)` - 根据自然语言检索关联工具

### knowledge.py

**函数：**
- `init_knowledge_collection()` - 初始化知识向量集合
- `build_knowledge_text(kp)` - 构建知识点的向量文本表示
- `sync_knowledge()` - 同步知识到向量库（增量更新）
- `query_knowledge(query, category, plugin, limit, score_threshold, use_rerank, rerank_top_k)` - 查询相关知识

### reranker.py

**函数：**
- `get_reranker()` - 获取Reranker实例（懒加载）
- `rerank_results(query, results, top_k)` - 对检索结果重排序

### init.py

**函数：**
- `init_all()` - 统一初始化所有RAG组件

## 配置项

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `enable` | bool | 是否启用AI功能（总开关） |
| `enable_rerank` | bool | 是否启用Rerank重排序功能 |

## 工具注册

使用 `@ai_tools` 装饰器注册工具：

```python
from gsuid_core.ai_core.register import ai_tools

@ai_tools
async def get_player(ctx, uid: str):
    """获取玩家的基本信息"""
    ...
```

装饰器会自动从被装饰函数的 `__name__` 和 `__doc__` 获取工具信息。

## 使用示例

### 同步工具

```python
from gsuid_core.ai_core.rag import sync_tools

tools_map = {
    "get_player": {
        "name": "get_player",
        "description": "获取玩家的基本信息"
    }
}

await sync_tools(tools_map)
```

### 搜索工具

```python
from gsuid_core.ai_core.rag import search_tools

tools = await search_tools("查询玩家信息", limit=5)
```

### 同步知识库

```python
from gsuid_core.ai_core.rag import sync_knowledge

await sync_knowledge()
```

### 查询知识

```python
from gsuid_core.ai_core.rag import query_knowledge

results = await query_knowledge(
    query="原神圣瞳位置",
    category="攻略",
    limit=10,
    score_threshold=0.5,
    use_rerank=True,
)
```

## 向量模型

- **Embedding模型：** `BAAI/bge-small-zh-v1.5`（中文优化，维度512）
- **Reranker模型：** `BAAI/bge-reranker-base`（可选）
