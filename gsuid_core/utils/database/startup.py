from shutil import move

from sqlalchemy import MetaData
from sqlalchemy.sql import text
from sqlalchemy.schema import DropTable
from sqlalchemy.exc import NoSuchTableError

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start
from gsuid_core.data_store import get_res_path
from gsuid_core.global_val import global_val_path

from .base_models import DB_PATH, db_url, engine, async_maker

exec_list = []


@on_core_start
async def move_database():
    old_path = get_res_path().parent / 'GsData.db'
    if old_path.exists() and not DB_PATH.exists():
        logger.warning(
            '检测到主目录存在旧版数据库, 迁移中...该log只会看到一次...'
        )
        move(old_path, db_url)
        logger.warning('迁移完成！')

    for i in global_val_path.glob('*.json'):
        i.unlink()
        logger.warning('删除历史统计记录...')


# @on_core_start
async def trans_adapter():
    async with engine.begin() as conn:
        metadata = MetaData()
        try:
            await conn.run_sync(metadata.reflect)
        except NoSuchTableError:
            logger.info('[迁移WebConsole数据表] 无需操作..')
        if 'auth_role' in metadata.tables:
            async with async_maker() as session:
                async with session.begin():
                    # 检查 auth_role 表中是否存在 delete_time 列
                    table = metadata.tables['auth_role']
                    column_exists = 'delete_time' in table.columns.keys()

                    if not column_exists:
                        tables_to_delete = [
                            'auth_group',
                            'auth_group_roles',
                            'auth_permission',
                            'auth_role',
                            'auth_role_permission',
                            'auth_token',
                            'auth_user',
                            'auth_user_groups',
                            'auth_user_roles',
                        ]
                        logger.info('[迁移WebConsole数据表] 正在执行..')
                        for table_name in tables_to_delete:
                            try:
                                table = metadata.tables[table_name]
                                await conn.execute(
                                    DropTable(table, if_exists=True)
                                )
                            except:  # noqa: E722
                                pass
                        logger.info('[迁移WebConsole数据表] 操作完成..')
        else:
            logger.info('[迁移WebConsole数据表] 无需操作...')

    async with async_maker() as session:
        for _t in exec_list:
            try:
                await session.execute(text(_t))
                await session.commit()
            except:  # noqa: E722
                pass
