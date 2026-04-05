"""
定时巡检模块

负责定时读取历史记录，由 Agent 判断是否主动发言。
当 ai_mode 包含 "定时巡检" 时启用，每隔半小时执行一次巡检。
"""

from .decision import should_ai_speak, generate_proactive_message
from .inspector import is_inspector_running, stop_heartbeat_inspector, start_heartbeat_inspector

__all__ = [
    "start_heartbeat_inspector",
    "stop_heartbeat_inspector",
    "is_inspector_running",
    "should_ai_speak",
    "generate_proactive_message",
]
