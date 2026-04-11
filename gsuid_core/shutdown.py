import asyncio

# 全局 shutdown 事件，用于协调取消所有无限循环任务
shutdown_event = asyncio.Event()
