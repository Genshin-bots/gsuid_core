"""
App module - 导出 FastAPI app 对象供其他模块使用
独立处理 app 相关内容，其他文件引用该 app 进行 @app.get 或 @app.post
"""

from gsuid_core.app_life import app

__all__ = ["app"]
