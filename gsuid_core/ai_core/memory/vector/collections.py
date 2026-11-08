# 记忆系统专用 Qdrant Collection 名称常量（schema_v2：仅四个活跃集合）。

MEMORY_EPISODES_COLLECTION = "memory_episodes"
MEMORY_ENTITIES_COLLECTION = "memory_entities"
MEMORY_EDGES_COLLECTION = "memory_edges"
MEMORY_THREADS_COLLECTION = "memory_threads"

MEMORY_COLLECTIONS: tuple[str, ...] = (
    MEMORY_EPISODES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EDGES_COLLECTION,
    MEMORY_THREADS_COLLECTION,
)

# 迁移时删除 + 清备份：旧集合 + 四个活跃集合（净室重建）。
LEGACY_MEMORY_COLLECTIONS: tuple[str, ...] = (
    "memory_sessions",
    "memory_gists",
    "memory_events",
    "memory_categories",
    "memory_hiergraph",
    "memory_conflicts",
    "memory_episodes_cold",
    "memory_episodes",
    "memory_entities",
    "memory_edges",
    "memory_threads",
)
