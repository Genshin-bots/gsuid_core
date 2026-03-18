from sqlalchemy import MetaData
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.schema import DropTable

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import engine, async_maker


async def drop_web_table():
    async with engine.begin() as conn:
        metadata = MetaData()
        try:
            await conn.run_sync(metadata.reflect)
        except NoSuchTableError:
            pass
        if "webuser" in metadata.tables:
            async with async_maker() as session:
                async with session.begin():
                    tables_to_delete = [
                        "auth_casbin_rule",
                        "auth_login_history",
                        "auth_role",
                        "auth_role_permission",
                        "auth_token",
                        "auth_user",
                        "webuser",
                    ]
                    logger.info("🚚 [core清除网页控制台账号] 正在执行..")
                    for table_name in tables_to_delete:
                        try:
                            table = metadata.tables[table_name]
                            await conn.execute(DropTable(table, if_exists=True))
                        except:  # noqa: E722
                            pass
                    logger.success("[core清除网页控制台账号] 操作完成..")
            return "✅ [网页控制台] 账户已全部清除, 请立即登陆网页控制台注册账户！"
        else:
            logger.info("[core清除网页控制台密码] 未找到表...")
            return "💫 [网页控制台] 账户密码清除失败...未找到表..."


async def drop_old_table():
    async with engine.begin() as conn:
        metadata = MetaData()
        try:
            await conn.run_sync(metadata.reflect)
        except NoSuchTableError:
            pass
        if "auth_role" in metadata.tables:
            async with async_maker() as session:
                async with session.begin():
                    tables_to_delete = [
                        "auth_casbin_rule",
                        "auth_login_history",
                        "auth_role",
                        "auth_role_permission",
                        "auth_token",
                        "auth_user",
                    ]
                    logger.info("🚚 [core清除控制台旧表] 正在执行..")
                    for table_name in tables_to_delete:
                        try:
                            table = metadata.tables[table_name]
                            await conn.execute(DropTable(table, if_exists=True))
                        except:  # noqa: E722
                            pass
                    logger.success("[core清除控制台旧表] 操作完成..")
            return "✅ [网页控制台] 账户已全部清除, 请立即登陆网页控制台注册账户！"
