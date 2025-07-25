# flake8: noqa
import sys
import platform
from pathlib import Path
from typing import Any, Dict, List, Type, Callable, Optional

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
from sqlalchemy.sql.elements import BinaryExpression
from fastapi_amis_admin.admin.admin import BaseAdminT
from fastapi_amis_admin.admin.settings import Settings
from fastapi_user_auth.admin.site import AuthAdminSite
from fastapi_amis_admin.utils.translation import i18n as _
from fastapi_amis_admin.admin import Settings, PageSchemaAdmin
from fastapi_amis_admin.admin.site import FileAdmin, APIDocsApp
from fastapi_amis_admin.crud.parser import get_python_type_parse
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

from gsuid_core.sv import SL
from gsuid_core.data_store import core_path
from gsuid_core.webconsole.log import render_html
from gsuid_core.logger import logger, handle_exceptions
from gsuid_core.utils.cookie_manager.add_ck import _deal_ck
from gsuid_core.version import __version__ as gscore_version
from gsuid_core.webconsole.html import gsuid_webconsole_help
from gsuid_core.utils.database.base_models import finally_url

# from gsuid_core.webconsole.create_log_panel import create_log_page
from gsuid_core.webconsole.create_task_panel import get_tasks_panel
from gsuid_core.webconsole.create_analysis_panel import get_analysis_page
from gsuid_core.webconsole.create_history_log import get_history_logs_page
from gsuid_core.webconsole.create_sv_panel import get_sv_page, get_ssv_page
from gsuid_core.webconsole.create_batch_push_panel import get_batch_push_panel
from gsuid_core.webconsole.create_core_config_panel import get_core_config_page
from gsuid_core.webconsole.create_config_panel import (
    get_config_page,
    get_sconfig_page,
)
from gsuid_core.utils.plugins_config.gs_config import (
    all_config_list,
    core_plugins_config,
)
from gsuid_core.webconsole.login_page import (  # noqa  # 不要删
    AuthRouter,
    amis_admin,
    user_auth_admin,
)
from gsuid_core.utils.database.models import (
    GsBind,
    GsPush,
    GsUser,
    GsCache,
    CoreUser,
    CoreGroup,
    Subscribe,
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
    database_url_async=finally_url,
    site_path='/genshinuid',
    site_icon='https://s2.loli.net/2022/01/31/kwCIl3cF1Z2GxnR.png',
    site_title='GsCore - 网页控制台',
    language='zh_CN',
    amis_theme='ang',
    amis_cdn=WebConsoleCDN,
    amis_pkg="amis@6.0.0",
)


def get_caller_plugin_name():
    try:
        frame = sys._getframe(2)
        parts = Path(frame.f_code.co_filename).resolve().parts

        # 从后往前查找 gsuid_core/plugins
        for i in range(len(parts) - 2, 0, -1):
            if parts[i - 1] == "gsuid_core" and parts[i] == "plugins":
                # 返回 plugins 的下一级目录名
                return parts[i + 1] if i + 1 < len(parts) else None

    except ValueError:
        # 栈层级不够，getframe(2)失败
        return None


def create_dynamic_page_class(
    class_name: str,
    label: str,
    url: str,
    page_call: Callable,
    args: Optional[List[Any]] = None,
    icon: str = 'fa fa-columns',
    sort: int = 100,
) -> Type["GsAdminPage"]:
    @handle_exceptions
    async def get_page(self, request: Request) -> Page:
        if args:
            return Page.parse_obj(page_call(*args))
        return Page.parse_obj(page_call())

    return type(
        class_name,
        (GsAdminPage,),
        {
            "page_schema": PageSchema(
                label=label,
                icon=icon,
                url=url,
                isDefaultPage=True,
                sort=sort,
            ),  # type: ignore
            "get_page": get_page,
        },
    )


def create_admin_class(class_name: str, label: str, admin_list: List):
    def __init__(self, app: "admin.AdminApp"):
        super(self.__class__, self).__init__(app)
        self.register_admin(*admin_list)

    # 构建类的属性字典
    attrs = {
        "page_schema": PageSchema(label=label, icon='fa fa-plus'),  # type: ignore
        "__init__": __init__,
    }

    NewAdminClass = type(class_name, (admin.AdminApp,), attrs)

    return NewAdminClass


# 自定义后台管理站点
class GsAdminSite(GsAuthAdminSite):
    template_name = str(Path(__file__).parent / 'page.html')

    def __init__(
        self,
        settings: Settings,
    ):
        super().__init__(settings)
        self.auth = self.auth or Auth(db=self.db)
        self.register_admin(self.UserAuthApp)
        self.plugins_page: Dict[str, List] = {}
        self.is_start = False

    async def get_page(self, request: Request) -> App:
        app = await super().get_page(request)
        app.brandName = 'GsCore网页控制台'
        app.logo = 'https://s2.loli.net/2022/01/31/kwCIl3cF1Z2GxnR.png'
        return app

    def register_admin(
        self, *admin_cls: Type[BaseAdminT], _ADD: bool = False
    ) -> Type[BaseAdminT]:
        plugin_name = get_caller_plugin_name()
        if plugin_name and not _ADD:
            if plugin_name not in self.plugins_page:
                self.plugins_page[plugin_name] = []
            self.plugins_page[plugin_name].extend(admin_cls)
        else:
            [self._registered.update({cls: None}) for cls in admin_cls if cls]
            if hasattr(self, 'plugins_page'):
                keys_to_move_last_set = set(
                    self.plugins_page
                )  # 转换为集合加速查找
                front = {
                    k: v
                    for k, v in self._registered.items()
                    if k not in keys_to_move_last_set
                }
                back = {
                    k: v
                    for k, v in self._registered.items()
                    if k in keys_to_move_last_set
                }
                self._registered = {**front, **back}

        return admin_cls[0]

    def gen_plugin_page(self):
        if not self.is_start:
            self.is_start = True
            for plugin_name, admin_cls in self.plugins_page.items():
                # icon_path = core_path / 'plugins' / plugin_name / 'ICON.png'
                if plugin_name in all_config_list:
                    admin_cls.append(
                        create_dynamic_page_class(
                            f'{plugin_name}Config',
                            '配置管理',
                            f'/_{plugin_name}Config',
                            get_sconfig_page,
                            [plugin_name, all_config_list[plugin_name]],
                            'fa fa-cogs',
                        )
                    )

                if plugin_name in SL.plugins:
                    plugin = SL.plugins[plugin_name]
                    sv_list = SL.detail_lst[SL.plugins[plugin_name]]

                    admin_cls.append(
                        create_dynamic_page_class(
                            f'{plugin_name}Sv',
                            '功能服务管理',
                            f'/{plugin_name}SvConfig',
                            get_ssv_page,
                            [sv_list, plugin],
                            'fa fa-sliders',
                        )
                    )

                cls = create_admin_class(
                    f'{plugin_name}App',
                    plugin_name.replace('UID', ''),
                    admin_cls,
                )
                self.register_admin(cls, _ADD=True)


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

    def calc_filter_clause(
        self, data: Dict[str, Any]
    ) -> List[BinaryExpression]:
        lst = []
        for k, v in data.items():
            sqlfield = self._filter_entities.get(k)
            v = '[~]' + v
            if sqlfield is not None:
                operator, val = self._parser_query_value(
                    v,
                    python_type_parse=get_python_type_parse(sqlfield),
                )
                if operator:
                    sql = getattr(sqlfield, operator)(*val)  # type: ignore
                    lst.append(sql)
        return lst


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
class PluginsConfig(admin.AdminApp):
    page_schema = PageSchema(label="插件管理", icon="fa fa-cogs")  # type: ignore

    def __init__(self, app: "admin.AdminApp"):
        super().__init__(app)
        self.register_admin(
            SVManagePage,
            ConfigManagePage,
            PluginsManagePage,
        )


class UserDatabase(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(
        label='用户数据库',
        icon='fa fa-user',
    )  # type: ignore

    # 配置管理模型
    model = CoreUser


class GroupDatabase(GsAdminModel):
    pk_name = 'id'
    page_schema = PageSchema(
        label='群组数据库',
        icon='fa fa-group',
    )  # type: ignore

    # 配置管理模型
    model = CoreGroup


@site.register_admin
class UserConfig(admin.AdminApp):
    page_schema = PageSchema(label="用户管理", icon="fa fa-user")  # type: ignore

    def __init__(self, app: "admin.AdminApp"):
        super().__init__(app)
        self.register_admin(
            UserDatabase,
            GroupDatabase,
        )


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
        return Page.parse_obj(render_html())


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


@site.register_admin
class LogAndMessage(admin.AdminApp):
    page_schema = PageSchema(label="日志和消息", icon="fa fa-comments-o")  # type: ignore

    def __init__(self, app: "admin.AdminApp"):
        super().__init__(app)
        self.register_admin(
            LogsPage,
            HistoryLogsPage,
            PushPage,
        )


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


@site.register_admin
class MiHoYoBind(admin.AdminApp):
    page_schema = PageSchema(label="米游账户绑定", icon="fa fa-link")  # type: ignore

    def __init__(self, app: "admin.AdminApp"):
        super().__init__(app)
        self.register_admin(
            UserBindFormAdmin,
            AmisPageAdmin,
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


# 取消注册默认管理类
site.unregister_admin(admin.HomeAdmin, APIDocsApp, FileAdmin)
