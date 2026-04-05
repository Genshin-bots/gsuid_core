"""
定时巡检核心逻辑

使用 aps.py 中的定时器，每隔半小时读取历史记录，
然后由 agent 判断是否该主动发言。
"""

import asyncio
from typing import Any, List, Optional
from datetime import datetime

from gsuid_core.aps import scheduler
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.ai_core.history import SessionKey, get_history_manager
from gsuid_core.ai_core.ai_config import ai_config
from gsuid_core.ai_core.ai_router import get_ai_session_by_id
from gsuid_core.ai_core.heartbeat.decision import should_ai_speak, generate_proactive_message

# 历史记录时间窗口（分钟）- 只检查最近30分钟的消息
HISTORY_TIME_WINDOW_MINUTES = 30

# 巡检间隔（分钟）
INSPECT_INTERVAL_MINUTES = 30


class HeartbeatInspector:
    """
    定时巡检器

    每隔半小时检查所有活跃的会话，判断 AI 是否应该主动发言。
    """

    def __init__(self):
        self._running = False
        self._history_manager = get_history_manager()

    def is_running(self) -> bool:
        """检查巡检器是否正在运行"""
        return self._running

    def start(self) -> bool:
        """
        启动定时巡检

        Returns:
            是否成功启动
        """
        # 检查是否启用了定时巡检模式
        ai_mode: list[str] = ai_config.get_config("ai_mode").data
        if "定时巡检" not in ai_mode:
            logger.info("🫀 [Heartbeat] 定时巡检模式未启用，跳过启动")
            return False

        if self._running:
            logger.warning("🫀 [Heartbeat] 巡检器已在运行中")
            return True

        try:
            # 使用 scheduler.scheduled_job 装饰器方式添加定时任务
            scheduler.add_job(
                func=self._inspect_all_sessions,
                trigger="interval",
                minutes=INSPECT_INTERVAL_MINUTES,
                id="ai_heartbeat_inspector",
                name="AI 定时巡检任务",
                replace_existing=True,
            )

            self._running = True
            logger.info(f"🫀 [Heartbeat] 定时巡检已启动，每 {INSPECT_INTERVAL_MINUTES} 分钟执行一次")
            return True

        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] 启动巡检器失败: {e}")
            return False

    def stop(self) -> bool:
        """
        停止定时巡检

        Returns:
            是否成功停止
        """
        if not self._running:
            return True

        try:
            scheduler.remove_job("ai_heartbeat_inspector")
            self._running = False
            logger.info("🫀 [Heartbeat] 定时巡检已停止")
            return True

        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] 停止巡检器失败: {e}")
            return False

    async def _inspect_all_sessions(self) -> None:
        """
        巡检所有活跃的会话

        这是定时任务实际执行的函数。
        """
        try:
            logger.info("🫀 [Heartbeat] 开始定时巡检...")

            # 获取所有活跃的 session
            sessions = self._history_manager.list_sessions()
            if not sessions:
                logger.debug("🫀 [Heartbeat] 没有活跃的会话，跳过本次巡检")
                return

            logger.info(f"🫀 [Heartbeat] 发现 {len(sessions)} 个活跃会话")

            # 检查每个会话
            for session_key in sessions:
                try:
                    await self._inspect_session(session_key)
                except Exception as e:
                    logger.exception(f"🫀 [Heartbeat] 巡检会话 {session_key} 时出错: {e}")
                    continue

            logger.info("🫀 [Heartbeat] 定时巡检完成")

        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] 巡检过程出错: {e}")

    async def _inspect_session(self, session_key: SessionKey) -> None:
        """
        检查单个会话，判断 AI 是否应该主动发言

        Args:
            session_key: 会话标识
        """
        # 获取会话的历史记录
        history = self._get_recent_history(session_key)
        if not history:
            return

        # 检查最近是否有 AI 发言（避免过于频繁）
        if self._has_recent_ai_response(history):
            logger.debug(f"🫀 [Heartbeat] 会话 {session_key} 最近已有 AI 发言，跳过")
            return

        # 获取会话信息
        group_id = session_key.group_id
        user_id = self._extract_user_id(session_key, history)

        if not user_id:
            return

        # 判断是否应该发言
        should_speak, reason = await should_ai_speak(history, group_id, user_id)

        if should_speak:
            logger.info(f"🫀 [Heartbeat] 会话 {session_key} 触发主动发言: {reason}")
            await self._trigger_proactive_response(session_key, history, user_id, reason)
        else:
            logger.debug(f"🫀 [Heartbeat] 会话 {session_key} 不触发主动发言: {reason}")

    def _get_recent_history(self, session_key: SessionKey) -> List[Any]:
        """
        获取指定会话最近的历史记录

        Args:
            session_key: 会话标识

        Returns:
            最近的历史记录列表
        """
        # 获取完整历史
        all_history = self._history_manager._histories.get(session_key, [])

        if not all_history:
            return []

        # 过滤出最近时间窗口内的消息
        cutoff_time = datetime.now().timestamp() - (HISTORY_TIME_WINDOW_MINUTES * 60)
        recent_history = [h for h in all_history if h.timestamp > cutoff_time]

        return recent_history

    def _has_recent_ai_response(self, history: List[Any]) -> bool:
        """
        检查最近历史中是否已有 AI 发言

        Args:
            history: 历史记录列表

        Returns:
            如果最近5条中有 AI 发言则返回 True
        """
        # 检查最近5条消息
        recent = history[-5:] if len(history) > 5 else history

        for record in reversed(recent):
            if record.role == "assistant":
                # 检查是否是主动发言（通过元数据判断）
                metadata = record.metadata or {}
                if metadata.get("proactive", False):
                    return True
        return False

    def _extract_user_id(self, session_key: SessionKey, history: List[Any]) -> Optional[str]:
        """
        从会话键或历史记录中提取用户 ID

        Args:
            session_key: 会话标识
            history: 历史记录

        Returns:
            用户 ID 或 None
        """
        # 如果是私聊，group_id 实际上是 user_id
        if session_key.group_id:
            # 检查是否是私聊格式（通过历史记录判断）
            if history:
                return history[-1].user_id
            return None
        return None

    async def _trigger_proactive_response(
        self,
        session_key: SessionKey,
        history: List[Any],
        user_id: str,
        trigger_reason: str,
    ) -> None:
        """
        触发 AI 主动发言

        Args:
            session_key: 会话标识
            history: 历史记录
            user_id: 用户 ID
            trigger_reason: 触发原因
        """
        try:
            # 获取或创建 AI Session
            session_id = str(session_key)
            session = await get_ai_session_by_id(session_id, user_id, session_key.group_id)

            if not session:
                logger.warning(f"🫀 [Heartbeat] 无法获取会话 {session_key} 的 AI Session")
                return

            # 生成主动发言内容
            message = await generate_proactive_message(
                history=history,
                session=session,
                user_id=user_id,
                group_id=session_key.group_id,
                trigger_reason=trigger_reason,
            )

            if not message:
                logger.debug(f"🫀 [Heartbeat] 会话 {session_key} 未生成主动消息")
                return

            # 使用 target_send 发送消息
            await self._send_proactive_message(session_key, user_id, message, trigger_reason)

        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] 触发主动发言失败: {e}")

    async def _send_proactive_message(
        self,
        session_key: SessionKey,
        user_id: str,
        message: str,
        trigger_reason: str,
    ) -> None:
        """
        使用 target_send 发送主动消息

        Args:
            session_key: 会话标识
            user_id: 用户 ID
            message: 消息内容
            trigger_reason: 触发原因
        """
        try:
            # 获取 bot 实例
            bot = await self._get_bot_for_session(session_key)
            if not bot:
                logger.warning(f"🫀 [Heartbeat] 无法获取会话 {session_key} 的 Bot 实例")
                return

            # 确定目标类型和 ID
            if session_key.group_id:
                # 群聊场景
                target_type = "group"
                target_id = session_key.group_id
            else:
                # 私聊场景
                target_type = "direct"
                target_id = user_id

            # 使用 target_send 发送消息
            await bot.target_send(
                message=message,
                target_type=target_type,
                target_id=target_id,
                bot_id=bot.bot_id,
                bot_self_id=bot.bot_self_id,
            )

            logger.info(f"🫀 [Heartbeat] 已发送主动消息到会话 {session_key}")

            # 记录到历史
            self._history_manager.add_message(
                group_id=session_key.group_id,
                user_id=user_id,
                role="assistant",
                content=message,
                metadata={
                    "proactive": True,
                    "trigger_reason": trigger_reason,
                    "bot_id": bot.bot_id,
                },
            )

        except Exception as e:
            logger.exception(f"🫀 [Heartbeat] 发送主动消息失败: {e}")

    async def _get_bot_for_session(self, session_key: SessionKey) -> Optional[Any]:
        """
        获取会话对应的 Bot 实例

        Args:
            session_key: 会话标识

        Returns:
            Bot 实例或 None
        """
        try:
            # 尝试从历史记录中获取 bot_id
            history = self._history_manager._histories.get(session_key, [])
            if history:
                for record in reversed(history):
                    metadata = record.metadata or {}
                    bot_id = metadata.get("bot_id")
                    if bot_id:
                        # 从 gss.active_bot 中查找
                        if bot_id in gss.active_bot:
                            return gss.active_bot[bot_id]

            # 如果没有找到，返回第一个可用的 bot
            if gss.active_bot:
                return list(gss.active_bot.values())[0]

            return None

        except Exception as e:
            logger.debug(f"🫀 [Heartbeat] 获取 Bot 实例失败: {e}")
            return None


# 全局单例
_inspector_instance: Optional[HeartbeatInspector] = None
_inspector_lock = asyncio.Lock()


async def get_inspector() -> HeartbeatInspector:
    """获取全局巡检器实例"""
    global _inspector_instance
    if _inspector_instance is None:
        async with _inspector_lock:
            if _inspector_instance is None:
                _inspector_instance = HeartbeatInspector()
    return _inspector_instance


async def start_heartbeat_inspector() -> bool:
    """启动定时巡检"""
    inspector = await get_inspector()
    return inspector.start()


async def stop_heartbeat_inspector() -> bool:
    """停止定时巡检"""
    inspector = await get_inspector()
    return inspector.stop()


def is_inspector_running() -> bool:
    """检查巡检器是否正在运行"""
    global _inspector_instance
    if _inspector_instance is None:
        return False
    return _inspector_instance.is_running()
