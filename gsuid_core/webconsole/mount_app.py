# flake8: noqa
import platform
from typing import Any, Callable

from starlette import status
from pydantic import BaseModel
from starlette.requests import Request
from fastapi_user_auth.auth import Auth
from starlette.responses import Response
from fastapi_amis_admin import amis, admin
from fastapi_amis_admin.crud import BaseApiOut
from fastapi_user_auth.auth.models import User
from fastapi_amis_admin.models.fields import Field
from fastapi import Depends, Request, HTTPException
from fastapi_user_auth.admin.app import UserAuthApp
from fastapi_amis_admin.admin.settings import Settings
from fastapi_user_auth.admin.site import AuthAdminSite
from fastapi_amis_admin.utils.translation import i18n as _
from fastapi_amis_admin.admin import Settings, PageSchemaAdmin
from fastapi_amis_admin.admin.site import FileAdmin, APIDocsApp
from fastapi_amis_admin.amis.constants import LevelEnum, DisplayModeEnum
from fastapi_user_auth.admin.admin import (
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

from gsuid_core.logger import logger, handle_exceptions
from gsuid_core.utils.database.base_models import db_url
from gsuid_core.utils.cookie_manager.add_ck import _deal_ck
from gsuid_core.version import __version__ as gscore_version
from gsuid_core.webconsole.html import gsuid_webconsole_help
from gsuid_core.webconsole.create_sv_panel import get_sv_page
from gsuid_core.webconsole.create_log_panel import create_log_page
from gsuid_core.webconsole.create_task_panel import get_tasks_panel
from gsuid_core.webconsole.create_config_panel import get_config_page
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
from gsuid_core.webconsole.create_analysis_panel import get_analysis_page
from gsuid_core.webconsole.create_history_log import get_history_logs_page
from gsuid_core.webconsole.create_batch_push_panel import get_batch_push_panel
from gsuid_core.webconsole.create_core_config_panel import get_core_config_page
from gsuid_core.utils.database.models import (
    GsBind,
    GsPush,
    GsUser,
    GsCache,
    Subscribe,
)
from gsuid_core.webconsole.login_page import (  # noqa  # 不要删
    AuthRouter,
    amis_admin,
    user_auth_admin,
)

WebConsoleCDN = core_plugins_config.get_config('WebConsoleCDN').data


class GsLoginFormAdmin(UserLoginFormAdmin):
    page = Page()

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
        form = await super().get_form(request)
        form.redirect = request.query_params.get('redirect') or '/genshinuid'
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
                        Action(
                            actionType='submit',
                            label=_('Sign in'),
                            level=LevelEnum.primary,
                        ),
                    ]
                )
            ],
        )
        return form


class GsUserRegFormAdmin(UserRegFormAdmin):
    @property
    def route_submit(self):
        async def route(
            response: Response,
            result: BaseApiOut = Depends(super().route_submit),  # type: ignore
        ):
            if (
                result.status == 0 and result.code == 0
            ):  # 登录成功,设置用户信息
                response.set_cookie(
                    'Authorization', f'bearer {result.data.access_token}'  # type: ignore
                )
            return result

        return route

    async def get_form(self, request: Request) -> Form:
        form = await super().get_form(request)
        form.redirect = request.query_params.get('redirect') or '/genshinuid'
        form.update_from_kwargs(
            title='',
            mode=DisplayModeEnum.horizontal,
            submitText=_('Sign up'),
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
                            label=_('Sign in'),
                        ),
                        Action(
                            actionType='submit',
                            label=_('Sign up'),
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


settings = Settings(
    database_url_async=f'sqlite+aiosqlite:///{db_url}',
    site_path='/genshinuid',
    site_icon='https://s2.loli.net/2022/01/31/kwCIl3cF1Z2GxnR.png',
    site_title='GsCore - 网页控制台',
    language='zh_CN',
    amis_theme='ang',
    amis_cdn=WebConsoleCDN,
    amis_pkg="amis@6.0.0",
)


# 自定义后台管理站点
class GsAdminSite(GsAuthAdminSite):
    def __init__(
        self,
        settings: Settings,
    ):
        super().__init__(settings)
        self.auth = self.auth or Auth(db=self.db)
        self.register_admin(self.UserAuthApp)

    async def get_page(self, request: Request) -> App:
        app = await super().get_page(request)
        app.brandName = 'GsCore网页控制台'
        app.logo = 'https://s2.loli.net/2022/01/31/kwCIl3cF1Z2GxnR.png'
        return app


site = GsAdminSite(settings)
site.auth.user_model = User


class GsNormalPage(admin.PageAdmin):
    async def has_page_permission(
        self,
        request: Request,
        obj: PageSchemaAdmin = None,  # type: ignore
        action: str = None,  # type: ignore
    ) -> bool:
        return True


class GsNormalForm(admin.FormAdmin):
    async def has_page_permission(
        self,
        request: Request,
        obj: PageSchemaAdmin = None,  # type: ignore
        action: str = None,  # type: ignore
    ) -> bool:
        return True


@site.register_admin
class AmisPageAdmin(admin.PageAdmin):
    page_schema = '入门使用'

    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(
            {
                'type': 'page',
                'body': {
                    'type': 'markdown',
                    'value': f'{gsuid_webconsole_help}',
                },
            }
        )


@site.register_admin
class UserBindFormAdmin(GsNormalForm):
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
            return BaseApiOut(status=-1, msg='你输入的CK可能已经失效/或者该用户ID未绑定UID')  # type: ignore
        ok_num = im.count('成功')
        if ok_num < 1:
            return BaseApiOut(status=-1, msg=im)  # type: ignore
        else:
            return BaseApiOut(msg=im)  # type: ignore


class GsAdminModel(admin.ModelAdmin):
    async def has_page_permission(
        self,
        request: Request,
        obj: PageSchemaAdmin = None,  # type: ignore
        action: str = None,  # type: ignore
    ) -> bool:
        return await super().has_page_permission(
            request, obj, action
        ) and await request.auth.requires(roles='root', response=False)(
            request
        )


class GsAdminPage(admin.PageAdmin):
    async def has_page_permission(
        self,
        request: Request,
        obj: PageSchemaAdmin = None,  # type: ignore
        action: str = None,  # type: ignore
    ) -> bool:
        return await super().has_page_permission(
            request, obj, action
        ) and await request.auth.requires(roles='root', response=False)(
            request
        )


@site.register_admin
class CKAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='CK管理', icon='fa fa-database')  # type: ignore

    # 配置管理模型
    model = GsUser


@site.register_admin
class PushAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='推送管理', icon='fa fa-bullhorn')  # type: ignore

    # 配置管理模型
    model = GsPush


@site.register_admin
class CacheAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='缓存管理', icon='fa fa-recycle')  # type: ignore

    # 配置管理模型
    model = GsCache


@site.register_admin
class BindAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='绑定管理', icon='fa fa-users')  # type: ignore

    # 配置管理模型
    model = GsBind


@site.register_admin
class SubscribeAdmin(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(label='订阅管理', icon='fa fa-rss')  # type: ignore

    # 配置管理模型
    model = Subscribe


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
    )  # type: ignore
    page_path = '/home'

    async def get_page(self, request: Request) -> Page:
        page = await super().get_page(request)
        page.body = [
            Alert(
                level='warning',
                body=' 警告: 初始root账号请务必前往「用户授权」➡「用户管理」处修改密码!',
            ),
            amis.Divider(),
            Property(
                title='早柚核心 信息',
                column=4,
                items=[
                    Property.Item(label='system', content=platform.system()),
                    Property.Item(
                        label='python', content=platform.python_version()
                    ),
                    Property.Item(label='version', content=gscore_version),
                    Property.Item(label='license', content='GPLv3'),
                ],
            ),
        ]
        return page

    async def has_page_permission(
        self,
        request: Request,
        obj: PageSchemaAdmin = None,  # type: ignore
        action: str = None,  # type: ignore
    ) -> bool:
        return True


@site.register_admin
class AnalysisPage(GsAdminPage):
    page_schema = PageSchema(
        label=('数据统计'),
        icon='fa fa-area-chart',
        url='/Analysis',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(await get_analysis_page())


@site.register_admin
class CoreManagePage(GsAdminPage):
    page_schema = PageSchema(
        label=('Core配置'),
        icon='fa fa-sliders',
        url='/CoreManage',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(get_core_config_page())


@site.register_admin
class SVManagePage(GsAdminPage):
    page_schema = PageSchema(
        label=('功能服务配置'),
        icon='fa fa-sliders',
        url='/SvManage',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(get_sv_page())


@site.register_admin
class ConfigManagePage(GsAdminPage):
    page_schema = PageSchema(
        label=('修改插件设定'),
        icon='fa fa-cogs',
        url='/ConfigManage',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(get_config_page())


@site.register_admin
class PluginsManagePage(GsAdminPage):
    page_schema = PageSchema(
        label=('插件管理'),
        icon='fa fa-puzzle-piece',
        url='/ConfigManage',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(get_tasks_panel())


@site.register_admin
class LogsPage(GsAdminPage):
    page_schema = PageSchema(
        label=('实时日志'),
        icon='fa fa-columns',
        url='/logs',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(create_log_page())


@site.register_admin
class HistoryLogsPage(GsAdminPage):
    page_schema = PageSchema(
        label=('历史日志'),
        icon='fa fa-columns',
        url='/logs',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(get_history_logs_page())


@site.register_admin
class PushPage(GsAdminPage):
    page_schema = PageSchema(
        label=('批量推送消息'),
        icon='fa fa-paper-plane',
        url='/BatchPush',
        isDefaultPage=True,
        sort=100,
    )  # type: ignore

    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        return Page.parse_obj(await get_batch_push_panel())


# 取消注册默认管理类
site.unregister_admin(admin.HomeAdmin, APIDocsApp, FileAdmin)
