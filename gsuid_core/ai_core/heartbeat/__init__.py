"""定时巡检模块 —— 主人格主动开口的心跳决策

当用户配置 ``ai_mode`` 包含 "定时巡检" 时启用：APScheduler 每隔半小时跑一次
``run_heartbeat``，让主人格读最近会话上下文 + 自我状态，**自行判断**是否应当
主动发一条消息（关心、提醒、跟进上次承诺等）。不是无脑定时催，而是"角色化
的内在动机"——见 ``decision.py`` 的判定逻辑。

## 模块组成

- ``inspector.py``    : APScheduler 钩子 + 启停接口（``start_heartbeat_inspector``
                        / ``stop_heartbeat_inspector`` / ``is_heartbeat_running``）。
- ``decision.py``     : ``run_heartbeat``——每次 tick 触发的决策入口，跑一次
                        无任务上下文的主人格 Agent，由它决定要不要 ``bot.send``。

## 与其他长任务承载的边界

- ``heartbeat`` 是**纯主人格主动**——没有用户消息，没有任务树，决定权全在
  主人格自己。
- ``scheduled_task`` 是**用户预约**的定时唤醒——到点拿来跑某项**具体**任务。
- ``planning`` (Kanban) 是**事件驱动**的多代理任务编排——靠依赖完成自然推进，
  不依赖定时器。

三者职责互不重叠；要做"每天 8 点发问候"应用 ``scheduled_task``，要做
"有时主动关心一下主人"用 ``heartbeat``，要做"30 天周期运营任务"走 ``planning``。
"""

from .decision import run_heartbeat
from .inspector import is_heartbeat_running, stop_heartbeat_inspector, start_heartbeat_inspector

__all__ = [
    "start_heartbeat_inspector",
    "stop_heartbeat_inspector",
    "is_heartbeat_running",
    "run_heartbeat",
]
