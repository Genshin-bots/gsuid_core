# 记忆系统 Qdrant Collection 名称常量
# 在现有 knowledge、image、bot_tools Collection 之外，
# 新增三个记忆专用 Collection，以 memory_ 前缀区分。


# 三个新 Collection 名称
MEMORY_EPISODES_COLLECTION = "memory_episodes"  # Episode 内容向量
MEMORY_ENTITIES_COLLECTION = "memory_entities"  # Entity name+summary 向量
MEMORY_EDGES_COLLECTION = "memory_edges"  # Edge fact 向量
