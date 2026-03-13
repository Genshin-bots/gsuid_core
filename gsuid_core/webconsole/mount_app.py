import sys
from typing import Any, Dict, List, Type
from pathlib import Path

from pydantic import BaseModel

from gsuid_core.utils.database.models import (
    GsBind,
    GsPush,
    GsUser,
    GsCache,
    CoreUser,
    CoreGroup,
    Subscribe,
)
from gsuid_core.utils.database.global_val_models import CoreTraffic, CoreDataSummary, CoreDataAnalysis


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


class PageSchema(BaseModel):
    label: str
    icon: str


class GsAdminModel:
    # 类型注解，用于静态分析工具识别 model 属性
    model: Any = None
    pk_name: str = "id"
    page_schema: Any = None


class Site:
    def __init__(
        self,
    ):
        self.plugins_page: Dict[str, List] = {}
        self._registered: Dict = {}
        self.is_start = False

    def register_admin(self, *admin_cls: Type[GsAdminModel], _ADD: bool = False) -> Type[GsAdminModel]:
        plugin_name = get_caller_plugin_name()
        if plugin_name and not _ADD:
            if plugin_name not in self.plugins_page:
                self.plugins_page[plugin_name] = []
            self.plugins_page[plugin_name].extend(admin_cls)
        else:
            [self._registered.update({cls: None}) for cls in admin_cls if cls]
            if hasattr(self, "plugins_page"):
                keys_to_move_last_set = set(self.plugins_page)  # 转换为集合加速查找
                front = {k: v for k, v in self._registered.items() if k not in keys_to_move_last_set}
                back = {k: v for k, v in self._registered.items() if k in keys_to_move_last_set}
                self._registered = {**front, **back}

        return admin_cls[0]


class SubscribeAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="订阅管理",
        icon="fa fa-rss",
    )  # type: ignore

    # 配置管理模型
    model = Subscribe


class CoreTrafficAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="流量数据管理",
        icon="fa fa-chart-line",
    )  # type: ignore

    # 配置管理模型
    model = CoreTraffic


class CoreDataSummaryAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="数据摘要管理",
        icon="fa fa-chart-line",
    )  # type: ignore

    # 配置管理模型
    model = CoreDataSummary


class CoreDataAnalysisAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="数据分析管理",
        icon="fa fa-chart-line",
    )  # type: ignore

    # 配置管理模型
    model = CoreDataAnalysis


class UserDatabase(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="用户数据库",
        icon="fa fa-user",
    )  # type: ignore

    # 配置管理模型
    model = CoreUser


class GroupDatabase(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="群组数据库",
        icon="fa fa-group",
    )  # type: ignore

    # 配置管理模型
    model = CoreGroup


class CKAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="CK管理",
        icon="fa fa-database",
    )  # type: ignore

    # 配置管理模型
    model = GsUser


class PushAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="推送管理",
        icon="fa fa-bullhorn",
    )  # type: ignore

    # 配置管理模型
    model = GsPush


class CacheAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="缓存管理",
        icon="fa fa-recycle",
    )  # type: ignore

    # 配置管理模型
    model = GsCache


class BindAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(
        label="绑定管理",
        icon="fa fa-users",
    )  # type: ignore

    # 配置管理模型
    model = GsBind


# 创建site实例 - 用于插件注册管理类
site = Site()
