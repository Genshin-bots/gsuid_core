import httpx
from sqlmodel import SQLModel

from gsuid_core.logger import logger
from gsuid_core.config import core_config
from gsuid_core.webconsole.mount_app import site


async def start_check():
    # 语言本地化
    from fastapi_user_auth import i18n as user_auth_i18n
    from fastapi_amis_admin import i18n as admin_auth_i18n

    HOST = core_config.get_config('HOST')
    PORT = core_config.get_config('PORT')

    admin_auth_i18n.set_language('zh_CN')
    user_auth_i18n.set_language('zh_CN')

    logger.info('尝试挂载WebConsole')
    await site.db.async_run_sync(
        SQLModel.metadata.create_all, is_session=False  # type:ignore
    )  # type:ignore
    # 创建默认测试用户, 请及时修改密码!!!
    auth = site.auth
    await auth.create_role_user('root')
    await auth.create_role_user('test')

    await site.router.startup()

    if not auth.enforcer.enforce("u:admin", site.unique_id, "page", "page"):
        await auth.enforcer.add_policy(
            "u:admin", site.unique_id, "page", "page", "allow"
        )
        await auth.enforcer.add_policy(
            "u:test", site.unique_id, "page", "page", "allow"
        )

    if HOST == 'localhost' or HOST == '127.0.0.1':
        _host = 'localhost'
        logger.warning(
            'WebConsole挂载于本地, 如想外网访问请修改config.json中host为0.0.0.0!'
        )
    else:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    'https://api.ipify.org/?format=json', timeout=4
                )
            _host = r.json()['ip']
        except:  # noqa:E722, B001
            _host = HOST

    logger.success(
        ('WebConsole挂载成功: ' f'http://{_host}:{PORT}/genshinuid')
    )
