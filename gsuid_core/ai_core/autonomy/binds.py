"""反射插件 Bind 表，查出观察对象已经绑定的 UID。"""

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import Bind


def iter_bind_tables() -> list[type[Bind]]:
    """只保留真正建了表的子类。测试里的空子类不会被扫到。"""
    found: list[type[Bind]] = []
    seen: set[type[Bind]] = set()
    stack: list[type[Bind]] = [Bind]
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
        if "__table__" in cls.__dict__:
            found.append(cls)
    return found


async def collect_bound_uids(
    user_id: str,
    bot_id: str,
    *,
    classes: list[type[Bind]] | None = None,
) -> dict[str, str]:
    """一张插件表失败不影响其余表。没绑定时返回空字典。"""
    chosen = classes if classes is not None else iter_bind_tables()
    found: dict[str, str] = {}
    for cls in chosen:
        uid = await _uid_of(cls, user_id, bot_id)
        if uid:
            found[cls.__name__] = uid
    return found


async def _uid_of(cls: type[Bind], user_id: str, bot_id: str) -> str:
    try:
        uid = await cls.get_uid_by_game(user_id, bot_id)
    except SQLAlchemyError as exc:
        logger.warning(t("log.ai.autonomy_bind_skip", plugin=cls.__name__, e=exc))
        return ""
    if not isinstance(uid, str):
        return ""
    return uid.strip()
