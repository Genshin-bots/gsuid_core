"""
定时巡检核心逻辑 (Heartbeat)

使用 aps.py 定时器，根据每个 persona 的 inspect_interval 配置读取历史记录。
先加载 AI Session，利用隐形 Sub-Agent 决策是否发言，若决定发言则生成并发送。

优化:
- 使用信号量限制并发 LLM 调用数量
- 前置轻量级规则过滤，避免不必要的 LLM 调用
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple, Optional
from datetime import datetime, timedelta

# 延迟导入避免循环依赖
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.server import _Bot
from gsuid_core.ai_core.history import get_history_manager
from gsuid_core.ai_core.ai_router import get_ai_session_by_id
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.persona.config import persona_config_manager
from gsuid_core.ai_core.heartbeat.decision import should_ai_speak, generate_proactive_message

# 并发控制：最多同时进行 5 个 LLM 调用
MAX_CONCURRENT_LLM_CALLS = 5
# 冷场阈值：超过 1 小时不活跃的群不再巡检
INACTIVE_THRESHOLD_HOURS = 1


class HeartbeatInspector:
    """AI 定时主动发言巡检器"""

    def __init__(self):
        self._running = False
        self._history_manager = get_history_manager()
        self._scheduled_jobs: dict[str, str] = {}  # persona_name -> job_id
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)

    @property
    def is_running(self) -> bool:
        return self._running

    def start_for_persona(self, persona_name: str) -> bool:
        """为指定 persona 启动巡检任务"""
        if persona_name in self._scheduled_jobs:
            return True

        try:
            # 延迟导入避免循环依赖
            from gsuid_core.aps import scheduler

            # 获取该 persona 的巡检间隔配置
            config = persona_config_manager.get_config(persona_name)
            ai_mode = config.get_config("ai_mode").data
            inspect_interval = config.get_config("inspect_interval").data

            # 检查是否启用了定时巡检模式
            if "定时巡检" not in ai_mode:
                logger.debug(f"🫀 [Heartbeat] {persona_name} 未启用定时巡检模式")
                return False

            job_id = f"ai_heartbeat_inspector_{persona_name}"
            scheduler.add_job(
                func=self._inspect_all_sessions_for_persona,
                trigger="interval",
                minutes=inspect_interval,
                id=job_id,
                name=f"AI 定时巡检任务 - {persona_name}",
                replace_existing=True,
                kwargs={"persona_name": persona_name},
            )
            self._scheduled_jobs[persona_name] = job_id
            logger.info(f"🫀 [Heartbeat] {persona_name} 定时巡检已启动，每 {inspect_interval} 分钟执行一次")
            return True
        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] {persona_name} 启动巡检器失败: {e}")
            return False

    def stop_for_persona(self, persona_name: str) -> bool:
        """为指定 persona 停止巡检任务"""
        if persona_name not in self._scheduled_jobs:
            return True

        try:
            # 延迟导入避免循环依赖
            from gsuid_core.aps import scheduler

            job_id = self._scheduled_jobs[persona_name]
            scheduler.remove_job(job_id)
            del self._scheduled_jobs[persona_name]
            logger.info(f"🫀 [Heartbeat] {persona_name} 定时巡检已停止")
            return True
        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] {persona_name} 停止巡检器失败: {e}")
            return False

    def start_all(self) -> bool:
        """启动所有启用了定时巡检的 persona 的巡检任务"""
        if self._running:
            return True

        # 获取所有启用了定时巡检的 persona
        all_configs = persona_config_manager.get_all_configs()
        for persona_name in all_configs:
            self.start_for_persona(persona_name)

        self._running = True
        return True

    def stop(self) -> bool:
        """停止所有巡检任务"""
        if not self._running:
            return True

        for persona_name in list(self._scheduled_jobs.keys()):
            self.stop_for_persona(persona_name)

        self._running = False
        logger.info("🫀[Heartbeat] 所有定时巡检已停止")
        return True

    async def _inspect_all_sessions_for_persona(self, persona_name: str) -> None:
        """巡检所有与指定 persona 相关的会话"""
        logger.info(f"🫀 [Heartbeat] {persona_name} 开始定时巡检...")

        # 获取该 persona 配置的 target_groups
        config = persona_config_manager.get_config(persona_name)
        scope = config.get_config("scope").data
        target_groups = config.get_config("target_groups").data

        # 获取所有活跃的会话
        sessions = self._history_manager.list_sessions()

        if not sessions:
            logger.debug(f"🫀 [Heartbeat] {persona_name} 无活跃会话，跳过")
            return

        logger.info(f"🫀[Heartbeat] {persona_name} 发现 {len(sessions)} 个活跃会话待检查")

        # 使用信号量控制并发 LLM 调用数量
        tasks = []
        for session_key in sessions:
            # 根据 scope 检查是否应该巡检该会话
            if not self._should_inspect_session(session_key, scope, target_groups, persona_name):
                continue

            # 前置规则过滤：快速判断是否需要巡检
            should_check, skip_reason = self._pre_check_session(session_key)
            if not should_check:
                logger.debug(f"🫀 [Heartbeat] 跳过 {session_key}: {skip_reason}")
                continue

            # 创建带信号量限制的任务
            task = asyncio.create_task(self._inspect_session_with_semaphore(session_key, persona_name))
            tasks.append(task)

        # 等待所有任务完成，带超时保护
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=300,  # 5分钟超时
                )
            except asyncio.TimeoutError:
                logger.warning(f"🫀 [Heartbeat] {persona_name} 巡检超时，已取消剩余任务")

        logger.info(f"🫀 [Heartbeat] {persona_name} 定时巡检完成")

    async def _inspect_session_with_semaphore(self, event: Event, persona_name: str) -> None:
        """带信号量控制的会话巡检"""
        async with self._semaphore:
            try:
                await self._inspect_session(event, persona_name)
                # 防并发风暴：每次检查完一个群，稍微歇息1秒
                await asyncio.sleep(1)
            except Exception as e:
                logger.exception(f"🫀[Heartbeat] {persona_name} 巡检会话 {event} 出错: {e}")

    def _pre_check_session(self, event: Event) -> Tuple[bool, str]:
        """
        前置轻量级规则过滤，快速判断是否需要巡检。
        避免对每个会话都调用 LLM，节省 Token 消耗。

        Returns:
            (是否需要巡检, 跳过原因)
        """
        history = self._get_history(event)
        if not history:
            return False, "无历史记录"

        last_message = history[-1]

        # 检查最后一条消息是否来自 AI（如果是，说明 AI 刚发过言）
        if last_message.role == "assistant":
            return False, "最后消息来自 AI，不继续发言"

        # 检查最后一条消息的时间
        if hasattr(last_message, "timestamp"):
            last_time = last_message.timestamp
            if isinstance(last_time, (int, float)):
                last_time = datetime.fromtimestamp(last_time)

            time_diff = datetime.now() - last_time
            if time_diff > timedelta(hours=INACTIVE_THRESHOLD_HOURS):
                return False, f"群已 {INACTIVE_THRESHOLD_HOURS} 小时不活跃"

        # 检查最后消息是否是 AI 主动发的（防刷屏）
        if self._has_recent_ai_response(history):
            return False, "AI 最近已发言（防刷屏）"

        return True, ""

    def _should_inspect_session(
        self,
        event: Event,
        scope: str,
        target_groups: List[str],
        persona_name: str,
    ) -> bool:
        """检查是否应该巡检该会话"""
        group_id: Optional[str] = event.group_id

        # 检查该会话是否匹配 persona 的配置
        if scope == "disabled":
            return False
        elif scope == "global":
            # 全局启用，所有会话都要巡检
            return True
        elif scope == "specific":
            # 只巡检指定群聊
            return group_id in target_groups if group_id else False

        return False

    async def _inspect_session(self, event: Event, persona_name: str) -> None:
        """处理单个会话的核心逻辑流水线"""
        # 1. 获取历史记录（使用 history 模块的全部消息，不再限制时间窗口）
        history = self._get_history(event)

        last_message = history[-1]
        user_id = last_message.user_id
        group_id: Optional[str] = event.group_id

        if not history or self._has_recent_ai_response(history):
            statistics_manager.record_heartbeat_decision(
                group_id=group_id or "",
                should_speak=False,
            )
            return

        if not user_id:
            return

        # 3. 获取 AI Session（关键前置！必须先有 Session 才能做 LLM 决策）
        session_id = event.session_id
        try:
            ai_session = await get_ai_session_by_id(
                session_id, user_id, group_id, is_group_chat=event.user_type != "direct"
            )
        except ValueError:
            # 没有配置 persona，跳过
            logger.debug(f"🫀 [Heartbeat] 会话 {event} 没有配置 persona")
            return

        if not ai_session:
            logger.debug(f"🫀 [Heartbeat] 无法加载会话 {event} 的 AI Session")
            return

        # 4. 决策阶段 (隐形 Sub-Agent)
        should_speak, reason = await should_ai_speak(history, ai_session)

        # 记录 Heartbeat 触发统计
        try:
            statistics_manager.record_trigger(trigger_type="heartbeat")
            statistics_manager.record_heartbeat_decision(
                group_id=group_id or "",
                should_speak=should_speak,
            )
        except Exception as e:
            logger.warning(f"📊 [Heartbeat] 记录决策统计失败: {e}")

        if not should_speak:
            logger.debug(f"🫀 [Heartbeat] 🤫 保持沉默: {reason} ({event})")
            return

        logger.info(f"🫀 [Heartbeat] 💡 决定插话: {reason} ({event})")

        # 5. 生成阶段 (主 Agent)
        message = await generate_proactive_message(history, ai_session, reason)
        if not message:
            logger.debug(f"🫀 [Heartbeat] 会话 {event} 文本生成为空，放弃发送")
            return

        # 6. 发送阶段
        await self._send_proactive_message(event, user_id, message, reason)

    def _get_history(self, event: Event) -> List[Any]:
        """获取会话的全部历史记录"""
        return list(self._history_manager._histories.get(event, []))

    def _has_recent_ai_response(self, history: List[Any]) -> bool:
        """如果最近 5 条消息里 AI 已经开过口了，就不再发言，防刷屏"""
        for record in reversed(history[-5:]):
            if record.role == "assistant":
                if (record.metadata or {}).get("proactive", False):
                    return True
        return False

    async def _send_proactive_message(self, event: Event, user_id: str, message: str, reason: str) -> None:
        try:
            _bot = await self._get_bot_for_session(event)
            if not _bot:
                logger.warning(f"🫀 [Heartbeat] 找不到可用的 Bot ({event})")
                return

            target_type = "group" if event.group_id else "direct"
            target_id = event.group_id or user_id

            # _Bot.target_send 签名：target_send(message, target_type, target_id, bot_id, bot_self_id, ...)
            await _bot.target_send(
                message=message,
                target_type=target_type,
                target_id=target_id,
                bot_id=event.bot_id,
                bot_self_id=event.bot_self_id,
            )

            logger.info(f"🫀 [Heartbeat] 发送成功 -> {target_id}: {message}")

            # 追加到系统历史记忆，带上特定的 metadata 标记
            self._history_manager.add_message(
                event=event,
                role="assistant",
                content=message,
                metadata={
                    "proactive": True,
                    "trigger_reason": reason,
                    "bot_id": _bot.bot_id,
                    "bot_self_id": "",
                },
            )
        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] 发送主动消息失败: {e}")

    async def _get_bot_for_session(self, event: Event) -> Optional["_Bot"]:
        """获取用于发送消息的 _Bot 实例

        优先使用 event.WS_BOT_ID（WS 连接 ID）直接查找 gss.active_bot，
        这是最准确的方式，因为 WS_BOT_ID 就是 gss.active_bot 的 key。

        Returns:
            _Bot 实例或 None
        """
        from gsuid_core.gss import gss

        # 方式1（最优先）：直接用 WS_BOT_ID 查找 WS 连接
        if event.WS_BOT_ID and event.WS_BOT_ID in gss.active_bot:
            return gss.active_bot[event.WS_BOT_ID]

        # 方式2（兜底）：遍历历史消息的 metadata 尝试找 bot_id
        bot_id: Optional[str] = None
        history = self._history_manager._histories.get(event, [])
        for record in reversed(history):
            metadata = record.metadata or {}
            if _bot_id := metadata.get("bot_id"):
                bot_id = _bot_id
                break

        if bot_id and bot_id in gss.active_bot:
            return gss.active_bot[bot_id]

        # 方式3（最后的兜底）：返回任意一个可用的 _Bot
        if gss.active_bot:
            return list(gss.active_bot.values())[0]
        return None


_inspector = HeartbeatInspector()


def get_inspector() -> HeartbeatInspector:
    return _inspector


def start_heartbeat_inspector() -> bool:
    """启动所有启用了定时巡检的 persona"""
    return _inspector.start_all()


def stop_heartbeat_inspector() -> bool:
    """停止所有巡检任务"""
    return _inspector.stop()


def is_heartbeat_running() -> bool:
    """检查是否有任何巡检任务在运行"""
    return _inspector.is_running
