# 记忆系统专用 Qdrant Collection 名称常量，统一使用 memory_ 前缀。


# 三个新 Collection 名称
MEMORY_EPISODES_COLLECTION = "memory_episodes"  # Episode 内容向量（热集：System-1 只查此集合）
MEMORY_ENTITIES_COLLECTION = "memory_entities"  # Entity name+summary 向量
MEMORY_EDGES_COLLECTION = "memory_edges"  # Edge fact 向量

# 冷热分集合：降级后的冷 Episode 向量迁入此集合，真值保留在 SQL。
MEMORY_EPISODES_COLD_COLLECTION = "memory_episodes_cold"  # 冷 Episode 归档向量
MEMORY_GISTS_COLLECTION = "memory_gists"
MEMORY_THREADS_COLLECTION = "memory_threads"
