"""反射插件 Bind 表，按 user_id 查出观察对象已经绑定的 UID。"""

from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import Bind


def _uid_fields(cls: type[Bind]) -> list[str]:
    found: list[str] = []
    for name in cls.model_fields:
        if name == "uid" or name.endswith("_uid"):
            found.append(name)
    return found


def iter_bind_tables() -> list[type[Bind]]:
    """只保留真正建了表且含 UID 字段的子类。空子类与非绑定表不会被扫到。"""
    found: list[type[Bind]] = []
    seen: set[type[Bind]] = set()
    stack: list[type[Bind]] = [Bind]
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
        if "__table__" in cls.__dict__ and _uid_fields(cls):
            found.append(cls)
    return found


async def collect_bound_uids(
    user_id: str,
    bot_id: str = "",
    *,
    classes: list[type[Bind]] | None = None,
) -> dict[str, str]:
    """一张插件表失败不影响其余表。没绑定时返回空字典。"""
    chosen = classes if classes is not None else iter_bind_tables()
    found: dict[str, str] = {}
    for cls in chosen:
        for key, uid in (await _uids_of(cls, user_id, bot_id)).items():
            if uid:
                found[key] = uid
    return found


async def _uids_of(cls: type[Bind], user_id: str, bot_id: str) -> dict[str, str]:
    fields = _uid_fields(cls)
    if not fields:
        if "get_uid_by_game" not in cls.__dict__:
            return {}
        try:
            uid = await cls.get_uid_by_game(user_id, bot_id)
        except (SQLAlchemyError, ValueError) as exc:
            logger.warning(t("log.ai.autonomy_bind_skip", plugin=cls.__name__, e=exc))
            return {}
        text = _first_uid(uid)
        return {cls.__name__: text} if text else {}

    try:
        rows = await cls.select_data_list(user_id)
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning(t("log.ai.autonomy_bind_skip", plugin=cls.__name__, e=exc))
        return {}
    if not rows:
        return {}
    ordered = sorted(rows, key=lambda item: 0 if item.bot_id == bot_id else 1) if bot_id else list(rows)
    found: dict[str, str] = {}
    for row in ordered:
        dumped = row.model_dump()
        for field_name in fields:
            key = cls.__name__ if field_name == "uid" else f"{cls.__name__}.{field_name.removesuffix('_uid')}"
            if key in found:
                continue
            raw = dumped[field_name] if field_name in dumped else None
            uid = _first_uid(raw)
            if uid:
                found[key] = uid
    return found


def _first_uid(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    for part in raw.split("_"):
        token = part.strip()
        if token:
            return token
    return ""
