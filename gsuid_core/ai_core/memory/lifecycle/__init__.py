"""记忆生命周期管理（C11 / plans/agent_design_review.md 第 7 章 C11）

解决"记忆只增不减、长期运行后 DB 膨胀、低价值旧记忆持续占用检索预算"的问题：

- **时效衰减（Decay）**：长期未被检索的 Edge 周期性下调 ``decay_score``。
- **遗忘（Forgetting）**：``decay_score`` 低于阈值的 Edge 物理删除，释放向量空间。
- **巩固（Consolidation）**：周期性把高频活跃实体上调权重，避免误伤。
- **矛盾处理（Contradiction）**：见 ``ingestion/edge.py`` + ``AIMemConflict`` 表。
"""
