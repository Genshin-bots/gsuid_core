from sqlmodel import SQLModel
from sqlalchemy import text, inspect
from sqlalchemy.exc import SQLAlchemyError

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import engine

# 需要清理的网页控制台相关表名（兼容多种大小写）
_TARGET_TABLE_KEYS = (
    "auth_casbin_rule",
    "auth_login_history",
    "auth_role",
    "auth_role_permission",
    "auth_token",
    "auth_user",
    "webuser",
)


async def _resolve_existing_tables(conn, candidates):
    """从数据库中找出实际存在的候选表名（兼容大小写）

    注意：SQLAlchemy 2.x 的 ``inspect`` 不能再直接作用于 AsyncConnection，
    必须通过 ``conn.run_sync`` 传入一个同步 callable。
    """
    try:
        existing: set[str] = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    except SQLAlchemyError as e:
        logger.warning(i18n_t("[core清除网页控制台账号] 获取表列表失败: {e}", e=e))
        return []

    logger.debug(i18n_t("[core清除网页控制台账号] 数据库中全部表: {p0}", p0=sorted(existing)))

    found = []
    for name in candidates:
        if name in existing:
            found.append(name)
            continue
        # 兼容可能的大小写变体（如 webuser / WebUser / WEBUSER）
        lower = name.lower()
        for e_name in existing:
            if e_name.lower() == lower:
                found.append(e_name)
                break
    return found


async def _recreate_webuser_table(conn) -> bool:
    """重建 webuser 表，确保前端能够继续注册账号。

    只重建 ``webuser``，因为只有它在 gsuid_core 进程内的
    ``SQLModel.metadata`` 中注册（见 ``auth_models.WebUser``）。
    webconsole 后端独立管理的 ``auth_*`` 表由后端服务在重启时自行重建。
    """
    try:
        # 确保模型已注册到 SQLModel.metadata（防御性，正常启动时已注册）
        import gsuid_core.utils.database.auth_models  # noqa: F401

        webuser_model = getattr(
            __import__(
                "gsuid_core.utils.database.auth_models",
                fromlist=["WebUser"],
            ),
            "WebUser",
        )
        await conn.run_sync(
            SQLModel.metadata.create_all,
            tables=[webuser_model.__table__],
        )
        logger.success(i18n_t("[core清除网页控制台账号] webuser 表重建成功!"))
        return True
    except SQLAlchemyError as e:
        logger.exception(i18n_t("[core清除网页控制台账号] webuser 表重建失败: {e}", e=e))
        return False


async def drop_web_table():
    """删除网页控制台相关的所有表，并重建 webuser 表。"""
    async with engine.begin() as conn:
        existing_tables = await _resolve_existing_tables(conn, _TARGET_TABLE_KEYS)

        if not existing_tables:
            logger.info(i18n_t("[core清除网页控制台密码] 未找到表..."))
            return "💫 [网页控制台] 账户密码清除失败...未找到表..."

        logger.info(i18n_t("🚚 [core清除网页控制台账号] 检测到表: {existing_tables}", existing_tables=existing_tables))
        try:
            for table_name in existing_tables:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        except SQLAlchemyError as e:
            logger.exception(i18n_t("[core清除网页控制台账号] 删除失败: {e}", e=e))
            return f"❌ [网页控制台] 账户清除失败: {e}"

        logger.success(i18n_t("[core清除网页控制台账号] 表删除完成.."))

        # 立刻重建 webuser 表，避免前端找不到表无法注册
        await _recreate_webuser_table(conn)

    return "✅ [网页控制台] 账户已全部清除, 请立即登陆网页控制台注册账户！"


async def drop_old_table():
    """删除网页控制台相关的旧版表（不含 webuser）。"""
    async with engine.begin() as conn:
        candidates = [t for t in _TARGET_TABLE_KEYS if t != "webuser"]
        existing_tables = await _resolve_existing_tables(conn, candidates)

        if not existing_tables:
            logger.info(i18n_t("[core清除控制台旧表] 未找到表..."))
            return "💫 [网页控制台] 账户密码清除失败...未找到表..."

        logger.info(i18n_t("🚚 [core清除控制台旧表] 检测到表: {existing_tables}", existing_tables=existing_tables))
        try:
            for table_name in existing_tables:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        except SQLAlchemyError as e:
            logger.exception(i18n_t("[core清除控制台旧表] 删除失败: {e}", e=e))
            return f"❌ [网页控制台] 账户清除失败: {e}"

        logger.success(i18n_t("[core清除控制台旧表] 操作完成.."))
    return "✅ [网页控制台] 账户已全部清除, 请立即登陆网页控制台注册账户！"
