# flake8: noqa
import platform
from typing import Any, Callable, Optional

from starlette import status
from pydantic import BaseModel
from fastapi_user_auth.auth import Auth
from fastapi_amis_admin import amis, admin
from fastapi_user_auth.app import UserAuthApp
from fastapi_amis_admin.crud import BaseApiOut
from sqlalchemy.ext.asyncio import AsyncEngine
from fastapi_user_auth.site import AuthAdminSite
from fastapi_amis_admin.models.fields import Field
from fastapi_amis_admin.admin.site import APIDocsApp
from fastapi_amis_admin.admin.settings import Settings
from fastapi_user_auth.auth.models import UserRoleLink
from fastapi_amis_admin.utils.translation import i18n as _
from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi_amis_admin.amis.constants import LevelEnum, DisplayModeEnum
from fastapi_user_auth.admin import (
    FormAdmin,
    UserRegFormAdmin,
    UserLoginFormAdmin,
)
from fastapi_amis_admin.amis.components import (
    App,
    Form,
    Grid,
    Page,
    Alert,
    Action,
    Property,
    ActionType,
    Horizontal,
    PageSchema,
    ButtonToolbar,
)

from gsuid_core.logger import logger
from gsuid_core.utils.database.api import db_url
from gsuid_core.webconsole.models import WebUser
from gsuid_core.utils.cookie_manager.add_ck import _deal_ck
from gsuid_core.webconsole.html import gsuid_webconsole_help
from gsuid_core.webconsole.create_sv_panel import get_sv_page
from gsuid_core.version import __version__ as GenshinUID_version
from gsuid_core.webconsole.create_task_panel import get_tasks_panel
from gsuid_core.webconsole.create_config_panel import get_config_page
from gsuid_core.utils.database.models import GsBind, GsPush, GsUser, GsCache
from gsuid_core.webconsole.login_page import (  # noqa  # 不要删
    AuthRouter,
    amis_admin,
    user_auth_admin,
)


class GsLoginFormAdmin(UserLoginFormAdmin):
    @property
    def route_page(self) -> Callable:
        async def route(
            request: Request, result=Depends(super(FormAdmin, self).route_page)
        ):
            if request.user:
                raise HTTPException(
                    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                    detail='已经登陆过啦~',
                    headers={
                        'location': request.query_params.get('redirect')
                        or '/genshinuid'
                    },
                )
            return result

        return route

    async def get_form(self, request: Request) -> Form:
        form = await super(user_auth_admin.UserLoginFormAdmin, self).get_form(
            request
        )
        form.body.sort(
            key=lambda form_item: form_item.type, reverse=True  # type: ignore
        )
        form.update_from_kwargs(
            title='',
            mode=DisplayModeEnum.horizontal,
            submitText=_('登陆'),
            actionsClassName='no-border m-none p-none',
            panelClassName='',
            wrapWithPanel=True,
            horizontal=Horizontal(left=3, right=9),
            actions=[
                ButtonToolbar(
                    buttons=[
                        ActionType.Link(
                            actionType='link',
                            link=f'{self.router_path}/reg',
                            label=_('Sign up'),
                        ),
                        Action(
                            actionType='submit',
                            label=_('Sign in'),
                            level=LevelEnum.primary,
                        ),
                    ]
                )
            ],
        )
        form.redirect = request.query_params.get('redirect') or '/genshinuid'
        return form


class GsUserRegFormAdmin(UserRegFormAdmin):
    async def get_form(self, request: Request) -> Form:
        form = await super().get_form(request)
        form.redirect = request.query_params.get('redirect') or '/genshinuid'
        form.update_from_kwargs(
            title='',
            mode=DisplayModeEnum.horizontal,
            submitText=_('注册'),
            actionsClassName='no-border m-none p-none',
            panelClassName='',
            wrapWithPanel=True,
            horizontal=Horizontal(left=3, right=9),
            actions=[
                ButtonToolbar(
                    buttons=[
                        ActionType.Link(
                            actionType='link',
                            link=f'{self.router_path}/login',
                            label=_('登陆'),
                        ),
                        Action(
                            actionType='submit',
                            label=_('注册'),
                            level=LevelEnum.primary,
                        ),
                    ]
                )
            ],
        )

        return form


class GsUserAuthApp(UserAuthApp):
    UserLoginFormAdmin = GsLoginFormAdmin
    UserRegFormAdmin = GsUserRegFormAdmin


class GsAuthAdminSite(AuthAdminSite):
    UserAuthApp = GsUserAuthApp


# 自定义后台管理站点
class GsAdminSite(GsAuthAdminSite):
    def __init__(
        self,
        settings: Settings,
        fastapi: FastAPI = None,  # type: ignore
        engine: AsyncEngine = None,  # type: ignore
        auth: Auth = None,  # type: ignore
    ):
        super().__init__(settings, fastapi, engine, auth)

    async def get_page(self, request: Request) -> App:
        app = await super().get_page(request)
        app.brandName = 'GsCore网页控制台'
        app.logo = 'https://s2.loli.net/2022/01/31/kwCIl3cF1Z2GxnR.png'
        return app


settings = Settings(
    database_url_async=f'sqlite+aiosqlite:///{db_url}',
    database_url='',
    site_path='/genshinuid',
    site_icon='https://s2.loli.net/2022/01/31/kwCIl3cF1Z2GxnR.png',
    site_title='GsCore - 网页控制台',
    language='zh_CN',
    amis_theme='ang',
)

site = GsAdminSite(settings)
site.auth.user_model = WebUser


@site.register_admin
class AmisPageAdmin(admin.PageAdmin):
    page_schema = '入门使用'
    page = Page.parse_obj(
        {
            'type': 'page',
            'body': {
                'type': 'markdown',
                'value': f'{gsuid_webconsole_help}',
            },
        }
    )


@site.register_admin
class UserBindFormAdmin(admin.FormAdmin):
    page_schema = PageSchema(label='绑定CK或SK', icon='fa fa-link')  # type: ignore

    async def get_form(self, request: Request) -> Form:
        form = await super().get_form(request)
        form.body.sort(key=lambda form_item: form_item.type, reverse=True)  # type: ignore
        form.update_from_kwargs(
            title='',
            mode=DisplayModeEnum.horizontal,
            submitText='绑定',
            actionsClassName='no-border m-none p-none',
            panelClassName='',
            wrapWithPanel=True,
            horizontal=Horizontal(left=3, right=9),
            actions=[
                ButtonToolbar(
                    buttons=[
                        Action(
                            actionType='submit',
                            label='绑定',
                            level=LevelEnum.primary,
                        )
                    ]
                )
            ],
        )
        return form

    async def get_page(self, request: Request) -> Page:
        page = await super().get_page(request)
        page.body = [
            Alert(
                level='warning',
                body='CK获取可查看左侧栏 [入门使用] 相关细则!',
            ),
            amis.Divider(),
            Grid(
                columns=[
                    {
                        'body': [page.body],
                        'lg': 10,
                        'md': 10,
                        'valign': 'middle',
                    }
                ],
                align='center',
                valign='middle',
            ),
        ]
        return page

    # 创建表单数据模型
    class schema(BaseModel):
        bot_id: str = Field(..., title='平台ID')  # type: ignore
        user_id: str = Field(..., title='用户ID', min_length=3, max_length=30)  # type: ignore
        cookie: str = Field(..., title='Cookie或者Login_ticket')  # type: ignore

    # 处理表单提交数据
    async def handle(
        self, request: Request, data: schema, **kwargs
    ) -> BaseApiOut[Any]:
        try:
            im = await _deal_ck(data.bot_id, data.cookie, data.user_id)
        except Exception as e:
            logger.warning(e)
            return BaseApiOut(status=-1, msg='你输入的CK可能已经失效/或者该用户ID未绑定UID')
        ok_num = im.count('成功')
        if ok_num < 1:
            return BaseApiOut(status=-1, msg=im)
        else:
            return BaseApiOut(msg=im)


class GsAdminModel(admin.ModelAdmin):
    async def has_page_permission(
        self,
        request: Request,
        obj: Optional[admin.ModelAdmin] = None,
        action: Optional[str] = None,
    ) -> bool:
        return await super().has_page_permission(
            request
        ) and await request.auth.requires(roles='admin', response=False)(
            request
        )


class GsAdminPage(admin.PageAdmin):
    async def has_page_permission(
        self,
        request: Request,
        obj: Optional[admin.ModelAdmin] = None,
        action: Optional[str] = None,
    ) -> bool:
        return await super().has_page_permission(
            request
        ) and await request.auth.requires(roles='admin', response=False)(
            request
        )


@site.register_admin
class UserAuth(GsAdminModel):
    pk_name = 'user_id'
    page_schema = PageSchema(label='用户授权', icon='fa fa-user-o')

    # 配置管理模型
    model = UserRoleLink


@site.register_admin
class CKadmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='CK管理', icon='fa fa-database')

    # 配置管理模型
    model = GsUser


@site.register_admin
class pushadmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='推送管理', icon='fa fa-bullhorn')

    # 配置管理模型
    model = GsPush


@site.register_admin
class cacheadmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='缓存管理', icon='fa fa-recycle')

    # 配置管理模型
    model = GsCache


@site.register_admin
class bindadmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='绑定管理', icon='fa fa-users')

    # 配置管理模型
    model = GsBind


# 注册自定义首页
@site.register_admin
class MyHomeAdmin(admin.HomeAdmin):
    group_schema = None
    page_schema = PageSchema(
        label=('主页'),
        icon='fa fa-home',
        url='/home',
        isDefaultPage=True,
        sort=100,
    )
    page_path = '/home'

    async def get_page(self, request: Request) -> Page:
        page = await super().get_page(request)
        page.body = [
            Alert(
                level='warning',
                body=' 警告: 初始admin账号请务必前往「用户授权」➡「用户管理」处修改密码!',
            ),
            amis.Divider(),
            Property(
                title='GenshinUID Info',
                column=4,
                items=[
                    Property.Item(label='system', content=platform.system()),
                    Property.Item(
                        label='python', content=platform.python_version()
                    ),
                    Property.Item(label='version', content=GenshinUID_version),
                    Property.Item(label='license', content='GPLv3'),
                ],
            ),
        ]
        return page


@site.register_admin
class SVManagePage(GsAdminPage):
    page_schema = PageSchema(
        label=('功能配置'),
        icon='fa fa-sliders',
        url='/SvManage',
        isDefaultPage=True,
        sort=100,
    )
    page = Page.parse_obj(get_sv_page())


@site.register_admin
class ConfigManagePage(GsAdminPage):
    page_schema = PageSchema(
        label=('修改设定'),
        icon='fa fa-cogs',
        url='/ConfigManage',
        isDefaultPage=True,
        sort=100,
    )
    page = Page.parse_obj(get_config_page())


@site.register_admin
class PluginsManagePage(GsAdminPage):
    page_schema = PageSchema(
        label=('插件管理'),
        icon='fa fa-puzzle-piece',
        url='/ConfigManage',
        isDefaultPage=True,
        sort=100,
    )
    page = Page.parse_obj(get_tasks_panel())


# 取消注册默认管理类
site.unregister_admin(admin.HomeAdmin, APIDocsApp)
