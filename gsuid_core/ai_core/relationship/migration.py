"""关系温度表的迁移：去重 + 唯一约束。

加列走 ``utils/database/startup.exec_list``（幂等 DDL，与其它 AI 表同一机制）。
去重必须在建唯一索引之前跑，且要有日志——静默失败会让日预算被重复行绕过，
而症状（「好感度偶尔回退」）和「聊天就涨」混在一起，很难被单独发现。

迁移挂 ``on_core_start_before``（阻塞阶段），不是 ``on_core_start``：
后者在 WS 启动后的后台阶段，期间已可能有聊天进来写这张表。
"""

from sqlalchemy.sql import text

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.server import on_core_start_before

_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_userfavorability_user_bot ON userfavorability (user_id, bot_id)"
)


@on_core_start_before(priority=-70)
async def migrate_user_favorability() -> None:
    """合并同 ``(user_id, bot_id)`` 重复行，然后补唯一索引。

    AI 总开关关闭时整条不跑（表可能都不存在）。
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return

    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.utils.database.base_models import async_maker

    try:
        removed = await UserFavorability.dedupe_user_bot_rows()
    except Exception as e:
        logger.warning(t("log.ai.relationship_dedupe_fail", e=e))
        return
    if removed:
        logger.warning(t("log.ai.relationship_dedupe_done", n=removed))

    try:
        async with async_maker() as session:
            await session.execute(text(_UNIQUE_INDEX_SQL))
            await session.commit()
    except Exception as e:
        # 建索引失败不阻断启动，但必须吵出来：没有唯一约束 = 日预算可能被绕过
        logger.warning(t("log.ai.relationship_unique_index_fail", e=e))
        return
    logger.debug(t("log.ai.relationship_unique_index_ok"))
