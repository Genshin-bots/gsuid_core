from typing import Dict

from .models import GSC, GsIntConfig, GsStrConfig, GsBoolConfig

DATABASE_CONIFG: Dict[str, GSC] = {
    "db_type": GsStrConfig(
        "数据库类型",
        "设置喜欢的数据库类型",
        "SQLite",
        ["SQLite", "MySql", "PostgreSQL", "自定义"],
    ),
    "db_driver": GsStrConfig(
        "MySQL驱动",
        "设置喜欢的MySQL驱动",
        "aiomysql",
        ["aiomysql", "asyncmy"],
    ),
    "db_custom_url": GsStrConfig(
        "自定义数据库连接地址 (一般无需填写)",
        "设置自定义数据库连接",
        "",
        ["sqlite+aiosqlite:///E://GenshinUID//gsdata.db"],
    ),
    "db_host": GsStrConfig(
        "数据库地址",
        "设置数据库地址",
        "localhost",
        ["localhost"],
    ),
    "db_port": GsStrConfig(
        "数据库端口",
        "设置数据库端口",
        "3306",
        ["3306"],
    ),
    "db_user": GsStrConfig(
        "数据库用户名",
        "设置数据库用户名",
        "root",
        ["root", "admin", "postgres"],
    ),
    "db_password": GsStrConfig(
        "数据库密码",
        "设置数据库密码",
        "root",
        ["root", "123456"],
    ),
    "db_name": GsStrConfig(
        "数据库名称",
        "设置数据库名称",
        "GsData",
        ["GsData"],
    ),
    "db_pool_size": GsIntConfig(
        "数据库连接池大小",
        "设置数据库连接池大小",
        5,
        options=[5, 10, 20, 30, 40, 50],
    ),
    "db_echo": GsBoolConfig(
        "数据库调试模式",
        "设置数据库调试模式",
        False,
    ),
    "db_pool_recycle": GsIntConfig(
        "数据库连接池回收时间",
        "设置数据库连接池回收时间",
        1500,
        options=[1500, 3600, 7200, 14400, 28800],
    ),
}
