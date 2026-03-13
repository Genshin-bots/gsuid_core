"""
数据库管理 API
提供数据库表的增删改查功能
"""

import inspect
from typing import Any, Dict, List, Type, Tuple, Optional

from sqlmodel import SQLModel, func, select

from gsuid_core.logger import logger
from gsuid_core.webconsole.mount_app import GsAdminModel, site
from gsuid_core.utils.database.base_models import async_maker


class ColumnInfo:
    """Column information for database table"""

    def __init__(self, name: str, title: str, col_type: str, nullable: bool, default: Any = None):
        self.name = name
        self.title = title
        self.col_type = col_type
        self.nullable = nullable
        self.default = default

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "type": self.col_type,
            "nullable": self.nullable,
            "default": self.default,
        }


class DatabaseTableInfo:
    """Database table information"""

    def __init__(
        self,
        table_name: str,
        table_title: str,
        columns: List[ColumnInfo],
        model_class: Type[SQLModel],
    ):
        self.table_name = table_name
        self.table_title = table_title
        self.columns = columns
        self.model_class = model_class

    def to_dict(self) -> Dict[str, Any]:
        # 尝试获取主键名
        pk_name = "id"
        if hasattr(self.model_class, "id"):
            pk_name = "id"
        elif self.columns:
            pk_name = self.columns[0].name

        return {
            "table_name": self.table_name,
            "label": self.table_title,
            "pk_name": pk_name,
            "columns": [col.to_dict() for col in self.columns],
        }


class PluginDatabaseInfo:
    """Plugin database information"""

    def __init__(self, plugin_id: str, plugin_name: str, tables: List[DatabaseTableInfo], icon: Optional[str] = None):
        self.plugin_id = plugin_id
        self.plugin_name = plugin_name
        self.tables = tables
        self.icon = icon

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "plugin_name": self.plugin_name,
            "tables": [table.to_dict() for table in self.tables],
            "icon": self.icon,
        }


class PaginatedData:
    """Paginated data response"""

    def __init__(self, items: List[Dict], total: int, page: int, per_page: int):
        self.items = items
        self.total = total
        self.page = page
        self.per_page = per_page

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "per_page": self.per_page,
        }


def _get_python_type(col_type: Any) -> str:
    """Convert Python type to string representation"""
    type_name = str(col_type)
    if "str" in type_name or "String" in type_name:
        return "str"
    elif "int" in type_name or "Integer" in type_name:
        return "int"
    elif "float" in type_name or "Float" in type_name or "Decimal" in type_name:
        return "float"
    elif "bool" in type_name or "Boolean" in type_name:
        return "bool"
    elif "datetime" in type_name or "DateTime" in type_name:
        return "datetime"
    elif "date" in type_name or "Date" in type_name:
        return "date"
    elif "time" in type_name or "Time" in type_name:
        return "time"
    elif "JSON" in type_name or "dict" in type_name:
        return "json"
    else:
        return "str"


def _extract_columns_from_model(model_class: Type[SQLModel]) -> List[ColumnInfo]:
    """Extract column information from SQLModel class"""
    columns = []
    try:
        # 遍历模型的字段 - 使用 Pydantic v2 的 model_fields
        if hasattr(model_class, "model_fields"):
            for field_name, field in model_class.model_fields.items():
                # 获取字段标题 - 从 FieldInfo 的 title 获取
                title = field_name

                # Pydantic v2: FieldInfo 有 title 属性
                field_title = getattr(field, "title", None)
                if field_title:
                    title = field_title

                # 获取字段类型
                col_type = "str"
                annotation = getattr(field, "annotation", None)
                if annotation is not None:
                    col_type = _get_python_type(annotation)

                # 获取是否可为空
                nullable = not getattr(field, "required", True)

                # 获取默认值
                default = getattr(field, "default", None)
                # 如果 default 是 ModelField，需要获取其 default
                if default is not None and hasattr(default, "default"):
                    default = getattr(default, "default", None)

                columns.append(ColumnInfo(field_name, title, col_type, nullable, default))
    except Exception as e:
        logger.error(f"Error extracting columns from model {model_class.__name__}: {e}")
    return columns


def _get_plugin_id_from_model(model_class: Type[SQLModel]) -> Tuple[str, str]:
    """Get plugin ID and name from model class module"""
    try:
        module_name = model_class.__module__
        # 尝试从模块路径中提取插件名
        if "gsuid_core" in module_name:
            parts = module_name.split(".")
            for i, part in enumerate(parts):
                if part == "plugins" and i + 1 < len(parts):
                    plugin_id = parts[i + 1]
                    return plugin_id, plugin_id
        return "core", "核心功能"
    except Exception:
        return "core", "核心功能"


def _collect_admin_models() -> Dict[str, List[DatabaseTableInfo]]:
    """Collect all registered admin models from site"""
    plugin_tables: Dict[str, List[DatabaseTableInfo]] = {}
    table_info_cache: Dict[str, DatabaseTableInfo] = {}

    logger.debug("Collecting admin models...")

    found_admins = []

    # 尝试从 mount_app.py 模块中查找所有 GsAdminModel 子类
    try:
        from gsuid_core.webconsole import mount_app

        # 遍历 mount_app 模块的所有成员
        for name, obj in inspect.getmembers(mount_app):
            if inspect.isclass(obj) and issubclass(obj, GsAdminModel) and obj != GsAdminModel:
                model = getattr(obj, "model", None)
                if model is not None:
                    found_admins.append(obj)
                    logger.debug(f"Found GsAdminModel: {name} with model {model.__name__}")
    except Exception as e:
        logger.error(f"Error importing mount_app: {e}")
        import traceback

        traceback.print_exc()

    logger.debug(f"Total found admins: {len(found_admins)}")

    # 处理找到的每个 admin
    for admin_cls in found_admins:
        model_class = getattr(admin_cls, "model", None)
        if model_class is None:
            continue

        table_name = getattr(model_class, "__tablename__", model_class.__name__)

        # 如果已经处理过，跳过
        if table_name in table_info_cache:
            continue

        # 获取页面标题
        page_title = table_name
        page_schema = getattr(admin_cls, "page_schema", None)
        if page_schema is not None:
            page_title = getattr(page_schema, "label", page_title)

        # 提取列信息
        columns = _extract_columns_from_model(model_class)

        # 创建表信息
        table_info = DatabaseTableInfo(
            table_name=table_name,
            table_title=page_title,
            columns=columns,
            model_class=model_class,
        )

        # 缓存表信息
        table_info_cache[table_name] = table_info

        # 获取插件 ID
        plugin_id, plugin_name = _get_plugin_id_from_model(model_class)

        logger.trace(f"Adding table {table_name} ({page_title}) to plugin {plugin_id}")

        # 添加到插件表列表
        if plugin_id not in plugin_tables:
            plugin_tables[plugin_id] = []
        plugin_tables[plugin_id].append(table_info)

    # 也检查 site.plugins_page（用于插件）
    try:
        logger.trace(f"Processing site.plugins_page: {len(site.plugins_page)} plugins")
        for plugin_name, admin_list in site.plugins_page.items():
            logger.trace(f"Processing plugin {plugin_name} with {len(admin_list)} admins")
            for admin_cls in admin_list:
                model_class = getattr(admin_cls, "model", None)
                if model_class is not None:
                    table_name = getattr(model_class, "__tablename__", model_class.__name__)

                    if table_name in table_info_cache:
                        continue

                    # 获取页面标题
                    page_title = table_name
                    page_schema = getattr(admin_cls, "page_schema", None)
                    if page_schema is not None:
                        page_title = getattr(page_schema, "label", page_title)

                    # 提取列信息
                    columns = _extract_columns_from_model(model_class)

                    # 创建表信息
                    table_info = DatabaseTableInfo(
                        table_name=table_name,
                        table_title=page_title,
                        columns=columns,
                        model_class=model_class,
                    )

                    # 缓存表信息
                    table_info_cache[table_name] = table_info

                    logger.trace(f"Adding table {table_name} ({page_title}) to plugin {plugin_name}")

                    # 添加到插件表列表
                    if plugin_name not in plugin_tables:
                        plugin_tables[plugin_name] = []
                    plugin_tables[plugin_name].append(table_info)
    except Exception as e:
        logger.error(f"Error processing site.plugins_page: {e}")

    """
    logger.debug("\nFinal collection results:")
    logger.debug(f"Collected {len(plugin_tables)} plugins: {list(plugin_tables.keys())}")
    for pid, tables in plugin_tables.items():
        logger.debug(f"  Plugin {pid}: {len(tables)} tables")
        for table in tables:
            logger.debug(f"    - {table.table_title} ({table.table_name})")
    """

    return plugin_tables


def get_all_plugin_databases() -> List[PluginDatabaseInfo]:
    """Get all plugin databases with their tables"""
    plugin_tables = _collect_admin_models()

    result = []
    for plugin_id, tables in plugin_tables.items():
        # 根据插件 ID 设置更友好的名称
        if plugin_id == "core":
            plugin_name = "核心功能"
        else:
            plugin_name = plugin_id
        result.append(PluginDatabaseInfo(plugin_id, plugin_name, tables))

    return result


def get_plugin_databases(plugin_id: str) -> Optional[PluginDatabaseInfo]:
    """Get database info for a specific plugin"""
    all_plugins = get_all_plugin_databases()
    for plugin in all_plugins:
        if plugin.plugin_id == plugin_id:
            return plugin
    return None


def get_table_info(table_name: str) -> Optional[DatabaseTableInfo]:
    """Get table info by table name"""
    plugin_tables = _collect_admin_models()

    # 遍历所有插件的表来查找
    for tables in plugin_tables.values():
        for table in tables:
            if table.table_name == table_name:
                return table

    return None


async def get_table_data(
    table_name: str,
    page: int = 1,
    per_page: int = 20,
) -> PaginatedData:
    """Get paginated data from a table"""
    table_info = get_table_info(table_name)
    if not table_info:
        return PaginatedData([], 0, page, per_page)

    model_class = table_info.model_class

    try:
        async with async_maker() as session:
            # 获取总数
            count_query = select(func.count()).select_from(model_class)
            count_result = await session.execute(count_query)
            total = count_result.scalar() or 0

            # 获取分页数据
            offset = (page - 1) * per_page
            query = select(model_class).offset(offset).limit(per_page)
            result = await session.execute(query)
            items = result.scalars().all()

            # 转换为字典
            dict_items = []
            for item in items:
                item_dict = {}
                for column in table_info.columns:
                    if hasattr(item, column.name):
                        value = getattr(item, column.name)
                        # 处理特殊类型
                        if value is not None:
                            if column.col_type == "datetime":
                                value = value.isoformat()
                            elif column.col_type == "json":
                                import json

                                try:
                                    value = json.dumps(value, ensure_ascii=False)
                                except Exception:
                                    value = str(value)
                        item_dict[column.name] = value
                dict_items.append(item_dict)

            return PaginatedData(dict_items, total, page, per_page)
    except Exception as e:
        logger.error(f"Error getting table data: {e}")
        return PaginatedData([], 0, page, per_page)


async def create_record(table_name: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Create a new record in the table"""
    table_info = get_table_info(table_name)
    if not table_info:
        return None

    model_class = table_info.model_class

    try:
        async with async_maker() as session:
            # 创建新记录
            record = model_class(**data)
            session.add(record)
            await session.commit()
            await session.refresh(record)

            # 转换为字典
            result_dict = {}
            for column in table_info.columns:
                if hasattr(record, column.name):
                    value = getattr(record, column.name)
                    if value is not None:
                        if column.col_type == "datetime":
                            value = value.isoformat()
                        elif column.col_type == "json":
                            import json

                            try:
                                value = json.dumps(value, ensure_ascii=False)
                            except Exception:
                                value = str(value)
                    result_dict[column.name] = value

            return result_dict
    except Exception as e:
        logger.error(f"Error creating record: {e}")
        return None


async def update_record(
    table_name: str,
    record_id: Any,
    data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Update a record in the table"""
    table_info = get_table_info(table_name)
    if not table_info:
        return None

    model_class = table_info.model_class

    try:
        async with async_maker() as session:
            # 获取记录
            # 假设主键是 id 字段
            if hasattr(model_class, "id"):
                id_field = getattr(model_class, "id")
                query = select(model_class).where(id_field == record_id)
            else:
                # 尝试第一个列作为主键
                first_column = table_info.columns[0].name if table_info.columns else "id"
                id_field = getattr(model_class, first_column, None)
                if id_field is None:
                    return None
                query = select(model_class).where(id_field == record_id)

            result = await session.execute(query)
            record = result.scalar_one_or_none()

            if not record:
                return None

            # 更新字段
            for key, value in data.items():
                if hasattr(record, key):
                    setattr(record, key, value)

            await session.commit()
            await session.refresh(record)

            # 转换为字典
            result_dict = {}
            for column in table_info.columns:
                if hasattr(record, column.name):
                    value = getattr(record, column.name)
                    if value is not None:
                        if column.col_type == "datetime":
                            value = value.isoformat()
                        elif column.col_type == "json":
                            import json

                            try:
                                value = json.dumps(value, ensure_ascii=False)
                            except Exception:
                                value = str(value)
                    result_dict[column.name] = value

            return result_dict
    except Exception as e:
        logger.error(f"Error updating record: {e}")
        return None


async def delete_record(table_name: str, record_id: Any) -> bool:
    """Delete a record from the table"""
    table_info = get_table_info(table_name)
    if not table_info:
        return False

    model_class = table_info.model_class

    try:
        async with async_maker() as session:
            # 获取记录
            if hasattr(model_class, "id"):
                id_field = getattr(model_class, "id")
                query = select(model_class).where(id_field == record_id)
            else:
                first_column = table_info.columns[0].name if table_info.columns else "id"
                id_field = getattr(model_class, first_column, None)
                if id_field is None:
                    return False
                query = select(model_class).where(id_field == record_id)

            result = await session.execute(query)
            record = result.scalar_one_or_none()

            if not record:
                return False

            await session.delete(record)
            await session.commit()
            return True
    except Exception as e:
        logger.error(f"Error deleting record: {e}")
        return False
