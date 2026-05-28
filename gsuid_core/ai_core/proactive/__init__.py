"""主动消息统一发送闭包。

详细设计：``plans/proactive_message_session_unification_20260529.md``

对外只暴露 ``emit_proactive_message``——所有"框架在 LLM run 之外注入到
用户会话"的输出（Heartbeat / ScheduledTask / Kanban 转译 / 失败播报 等）
都必须走这一个入口，由它统一完成：

1. C8 防撞车（``UnifiedProactiveDispatcher.should_suppress_heartbeat``）；
2. ``bot.send`` 单次落 ``message_history``（``metadata.proactive=True`` 等
   分类字段从这里透传）；
3. 写入用户绑定 ``GsCoreAIAgent`` 的 ``self.history``（一条 assistant-only
   ``ModelMessage``），让下一轮 LLM 看得到自己说过什么；
4. 写入该用户 session 的 ``AISessionLogger``（新增 ``proactive_emission``
   entry，可在 webconsole 主动消息时间线里展示）；
5. C8 网关登记 ``register_send``（保留旧的合并语境策略）。
"""

from gsuid_core.ai_core.proactive.emitter import emit_proactive_message

__all__ = ["emit_proactive_message"]
