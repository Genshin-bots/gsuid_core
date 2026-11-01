"""启动期一次性数据迁移包。

各迁移以 ``AIPersistentState`` 的 ``system:migrations`` scope 下的标记位保证幂等，
只在标记缺失时执行一次。
"""
