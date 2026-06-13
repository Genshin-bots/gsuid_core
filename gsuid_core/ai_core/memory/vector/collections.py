# 记忆系统 Qdrant Collection 名称常量
# 在现有 knowledge、image、bot_tools Collection 之外，
# 新增三个记忆专用 Collection，以 memory_ 前缀区分。


# 三个新 Collection 名称
MEMORY_EPISODES_COLLECTION = "memory_episodes"  # Episode 内容向量（热集：System-1 只查此集合）
MEMORY_ENTITIES_COLLECTION = "memory_entities"  # Entity name+summary 向量
MEMORY_EDGES_COLLECTION = "memory_edges"  # Edge fact 向量

# §3.2① 冷热分集合：降级后的冷 Episode 向量迁入此集合，使热集合 memory_episodes
# 规模可控（缓解 P0-1 本地向量库暴力扫描）。冷集合不参与 System-1 在线检索，
# 仅作可审计 / 可按需检索的归档；其向量为派生数据，真值始终在 SQL（AIMemEpisode）。
MEMORY_EPISODES_COLD_COLLECTION = "memory_episodes_cold"  # 冷 Episode 归档向量
