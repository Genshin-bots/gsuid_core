from fastapi_amis_admin import admin

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.models import (
    GsBind,
    GsPush,
    GsUser,
    GsCache,
    Subscribe,
)


class CKAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(
        label='CK管理',
        icon='fa fa-database',
    )  # type: ignore

    # 配置管理模型
    model = GsUser


class PushAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(
        label='推送管理',
        icon='fa fa-bullhorn',
    )  # type: ignore

    # 配置管理模型
    model = GsPush


class CacheAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(
        label='缓存管理',
        icon='fa fa-recycle',
    )  # type: ignore

    # 配置管理模型
    model = GsCache


class BindAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(
        label='绑定管理',
        icon='fa fa-users',
    )  # type: ignore

    # 配置管理模型
    model = GsBind


@site.register_admin
class MiHoYoDatabase(admin.AdminApp):
    page_schema = PageSchema(
        label="米游数据库",
        icon="fa fa-database",
    )  # type: ignore

    def __init__(self, app: "admin.AdminApp"):
        super().__init__(app)
        self.register_admin(
            CKAdmin,
            PushAdmin,
            CacheAdmin,
            BindAdmin,
        )


@site.register_admin
class SubscribeAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(
        label='订阅管理',
        icon='fa fa-rss',
    )  # type: ignore

    # 配置管理模型
    model = Subscribe
