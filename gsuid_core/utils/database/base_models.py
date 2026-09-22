import asyncio
import sqlite3
import contextlib
import contextvars
from typing import (
    Any,
    Dict,
    List,
    Type,
    TypeVar,
    Callable,
    Optional,
    Sequence,
    Awaitable,
)
from functools import wraps
from collections.abc import AsyncIterator
from typing_extensions import ParamSpec, Concatenate

import aiosqlite
from sqlmodel import Field, SQLModel, col, and_, delete, select, update
from sqlalchemy import MetaData, exc, text, event, inspect, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.schema import CreateTable
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,  # type: ignore
    create_async_engine,
)
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.expression import func, null, true

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.utils.plugins_config.gs_config import database_config

T_BaseModel = TypeVar("T_BaseModel", bound="BaseModel")
T_BaseIDModel = TypeVar("T_BaseIDModel", bound="BaseIDModel")
T_User = TypeVar("T_User", bound="User")
T_Bind = TypeVar("T_Bind", bound="Bind")
T_Push = TypeVar("T_Push", bound="Push")
T_Cache = TypeVar("T_Cache", bound="Cache")
T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")


db_host: str = database_config.get_config("db_host").data
db_port: str = database_config.get_config("db_port").data
db_user: str = database_config.get_config("db_user").data
db_password: str = database_config.get_config("db_password").data
db_name: str = database_config.get_config("db_name").data

db_pool_size: Optional[int] = database_config.get_config("db_pool_size").data
db_echo: bool = database_config.get_config("db_echo").data
db_pool_recycle: int = database_config.get_config("db_pool_recycle").data

db_custom_url = database_config.get_config("db_custom_url").data
db_type: str = database_config.get_config("db_type").data

db_driver: str = database_config.get_config("db_driver").data

_db_type = db_type.lower()
db_config = {
    "pool_recycle": db_pool_recycle,
    "echo": db_echo,
}

DB_PATH = get_res_path() / "GsData.db"

sync_url, engine, finally_url = "", "", ""
async_maker: async_sessionmaker[AsyncSession] = None  # type: ignore
server_engine = None
_db_init_lock = asyncio.Lock()
_db_initialized = False
sqlite_semaphore = None
sqlite_read_semaphore = None

# 建连超时。journal_mode 只在启动时设一次，避免每条连接在别人持写锁时立刻失败。
_SQLITE_BUSY_MS = 5000
_SQLITE_TIMEOUT_S = 5.0
_UPSERT_CHUNK = 400


class _WriteBinding:
    __slots__ = ("session", "owner")

    def __init__(self, session: AsyncSession, owner: object) -> None:
        self.session = session
        self.owner = owner


_write_session: contextvars.ContextVar[_WriteBinding | None] = contextvars.ContextVar(
    "gsuid_sqlite_write_session",
    default=None,
)


def _owned_write_session() -> AsyncSession | None:
    active = _write_session.get()
    if active is None:
        return None
    # create_task 会拷贝 context，子任务不能和父任务共用一条 AsyncSession。
    if asyncio.current_task() is not active.owner:
        return None
    return active.session


if _db_type == "sqlite":
    sync_url = "sqlite:///"
    base_url = "sqlite+aiosqlite:///"
    db_url = str(DB_PATH)
    # del db_config['pool_size']
elif _db_type == "mysql":
    sync_url = "mysql+pymysql://"
    base_url = f"mysql+{db_driver}://"
    db_hp = f"{db_host}:{db_port}" if db_port else db_host
    db_url = f"{db_user}:{db_password}@{db_hp}/"
elif _db_type == "postgresql":
    sync_url = "postgresql+psycopg://"
    base_url = "postgresql+asyncpg://"
    db_hp = f"{db_host}:{db_port}" if db_port else db_host
    db_url = f"{db_user}:{db_password}@{db_hp}/"
elif _db_type == "自定义":
    base_url = ""
    db_url = db_custom_url
else:
    base_url = db_type
    db_url = db_custom_url


def _writer_is_core(func: Callable[..., object]) -> bool:
    module = func.__module__
    return isinstance(module, str) and module.startswith("gsuid_core.")


@contextlib.asynccontextmanager
async def sqlite_gated_write() -> AsyncIterator[None]:
    """SQLite 占住单写者闸门。其它后端不加这把锁。"""
    if _db_type != "sqlite":
        yield
        return
    async with sqlite_write_gate.hold(core=True):
        yield


_ModelT = TypeVar("_ModelT", bound=SQLModel)


def _mapped_column(model: type[_ModelT], name: str) -> InstrumentedAttribute[object]:
    mapper = inspect(model)
    if name not in mapper.attrs:
        raise ValueError(f"{model.__name__} has no column {name}")
    attr = mapper.attrs[name].class_attribute
    if isinstance(attr, InstrumentedAttribute):
        return attr
    raise TypeError(f"{model.__name__}.{name} is not a mapped column")


def _enable_sqlite_wal(db_path: str) -> None:
    # 每条连接再设 journal_mode 会在别人持写锁时立刻 database is locked。
    conn = sqlite3.connect(db_path, timeout=_SQLITE_TIMEOUT_S)
    try:
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_MS}")
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
    finally:
        conn.close()
    mode = ""
    if row is not None:
        cell = row[0]
        if isinstance(cell, str):
            mode = cell
    if mode.lower() != "wal":
        raise RuntimeError("sqlite journal_mode did not switch to wal")


def _set_sqlite_connect_pragmas(dbapi_connection: sqlite3.Connection, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_MS}")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


async def _mark_read_only(session: AsyncSession) -> None:
    if _db_type != "sqlite":
        return
    await session.execute(text("PRAGMA query_only=ON"))


async def init_database():
    global _db_initialized, engine, finally_url, async_maker, sqlite_read_semaphore

    if _db_initialized:
        return

    async with _db_init_lock:
        if _db_initialized:
            return

        logger.info(i18n_t("log.database.init_start"))

        try:
            if _db_type == "sqlite":
                # 默认 QueuePool 会在等写锁时占满槽（2026-07 雪崩）。NullPool + 单写者闸门。
                # sqlite3.connect 会堵满 busy_timeout，不能占着事件循环。
                await asyncio.to_thread(_enable_sqlite_wal, db_url)
                db_config.update(
                    {
                        "connect_args": {
                            "check_same_thread": False,
                            "timeout": _SQLITE_TIMEOUT_S,
                        },
                        "poolclass": NullPool,
                    }
                )
                engine = create_async_engine(f"{base_url}{db_url}", **db_config)
                finally_url = f"{base_url}{db_url}"

                event.listens_for(engine.sync_engine, "connect")(_set_sqlite_connect_pragmas)

                # 读不占写闸门。写不再用信号量，避免 8 条连接同时去抢 SQLite。
                sqlite_read_semaphore = asyncio.Semaphore(24)
            else:
                db_config.update(
                    {
                        "pool_size": db_pool_size,
                        "max_overflow": 10,
                        "pool_timeout": 30,
                        "isolation_level": "AUTOCOMMIT",
                    }
                )
                server_engine: Engine | None = None
                try:
                    if _db_type == "mysql":
                        server_engine = create_engine(f"{sync_url}{db_url}", **db_config)

                        with server_engine.connect() as conn:
                            t1 = f"CREATE DATABASE IF NOT EXISTS {db_name} "
                            t2 = "CHARACTER SET utf8mb4 COLLATE "
                            t3 = "utf8mb4_unicode_ci"
                            conn.execute(text(t1 + t2 + t3))
                            logger.success(i18n_t("log.database.mysql_ok_create_database_db_name", db_name=db_name))
                    elif _db_type == "postgresql":
                        server_engine = create_engine(f"{sync_url}{db_url}", **db_config)
                        try:
                            with server_engine.connect() as conn:
                                t = f"CREATE DATABASE {db_name} WITH ENCODING "
                                t2 = "'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE "
                                t3 = "'en_US.UTF-8' TEMPLATE template0"
                                conn.execute(text(t + t2 + t3))
                        except exc.ProgrammingError as e:
                            if "already exists" in str(e) or "已经存在" in str(e):
                                pass
                            else:
                                raise
                        logger.success(i18n_t("log.database.postgresql_db_name_created", db_name=db_name))
                finally:
                    if server_engine:
                        server_engine.dispose()
                        logger.info(i18n_t("log.database.temporary_connection"))

                # db_config['poolclass'] = NullPool
                finally_url = f"{base_url}{db_url}{db_name}"
                engine = create_async_engine(finally_url, **db_config)

            async_maker = async_sessionmaker(
                engine,
                expire_on_commit=False,
                close_resets_only=False,
                class_=AsyncSession,
            )

            _db_initialized = True
        except Exception as e:  # noqa: E722
            logger.exception(i18n_t("log.database.gscore_connection", e=e))
            raise ValueError(i18n_t("[GsCore] [数据库] [{base_url}] 连接失败, 请检查配置文件!", base_url=base_url))


def _is_pool_timeout(err: BaseException) -> bool:
    """连接池耗尽 / checkout 超时：重试只会让等待雪崩，必须立即失败。"""
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
        return True
    # sqlalchemy.exc.TimeoutError 在多数版本是 builtins.TimeoutError 子类；
    # 保险起见再按文案识别 QueuePool 报错。
    msg = str(err)
    return "QueuePool limit" in msg or "connection timed out" in msg


def _is_transient_db_error(err: BaseException) -> bool:
    """仅对 SQLite 锁竞争 / 瞬时 I/O 做有限重试。"""
    if not isinstance(err, OperationalError):
        return False
    msg = str(err).lower()
    if "unable to open database file" in msg:
        return False
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg or "disk i/o error" in msg


# 从拿到写槽开始计。超时后取消并关连接，避免一个插件占住 SQLite 写锁。
_WRITE_BUDGET_S = 10.0
_WRITE_ABORT_GRACE_S = 1.0


class DatabaseWriteTimeout(Exception):
    """一次 ``@with_session`` 超过写预算。写槽已释放，这次写入失败。"""


class _SqliteDriverLease:
    __slots__ = ("connection",)

    def __init__(self) -> None:
        self.connection: aiosqlite.Connection | None = None


def _budget_label(seconds: float) -> str:
    if seconds.is_integer():
        return str(int(seconds))
    return str(seconds)


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    task.exception()


async def _capture_sqlite_driver(session: AsyncSession, lease: _SqliteDriverLease) -> None:
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    driver = raw.driver_connection
    if isinstance(driver, aiosqlite.Connection):
        lease.connection = driver


async def _interrupt_driver(driver: aiosqlite.Connection) -> None:
    # 会话退出路径可能已经关掉连接。
    try:
        await driver.interrupt()
    except ValueError:
        return


async def _close_driver(driver: aiosqlite.Connection) -> None:
    # 取消未完成的 close 会在闸门交出后仍占着 SQLite 写锁。
    try:
        await driver.close()
    except ValueError:
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(i18n_t("log.database.write_timeout_close_fail", e=exc))


async def _stop_write_task(task: asyncio.Task[object], lease: _SqliteDriverLease) -> bool:
    """取消写任务。返回协程是否在宽限后仍活着。"""
    if task.done():
        _consume_task_exception(task)
        return False
    task.cancel()
    driver = lease.connection
    if driver is not None:
        await _interrupt_driver(driver)
    _done, _pending = await asyncio.wait({task}, timeout=_WRITE_ABORT_GRACE_S)
    if task.done():
        _consume_task_exception(task)
        return False
    if driver is not None:
        await _close_driver(driver)
    task.add_done_callback(_consume_task_exception)
    return True


async def _retry_db(call: Callable[[], Awaitable[R]]) -> R:
    max_retries = 3
    last_err: BaseException | None = None
    for attempt in range(max_retries):
        try:
            return await call()
        except DatabaseWriteTimeout:
            raise
        except Exception as e:
            last_err = e
            if _is_pool_timeout(e):
                logger.error(i18n_t("log.database.connect_timeout_stop", e=e))
                raise
            if isinstance(e, OperationalError) and "unable to open database file" in str(e):
                logger.error(i18n_t("log.database.stop_retry"))
                raise
            if _is_transient_db_error(e) and attempt < max_retries - 1:
                logger.warning(i18n_t("log.database.retry_fail_2", p0=attempt + 1, e=e))
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if attempt >= max_retries - 1 and _is_transient_db_error(e):
                logger.error(i18n_t("log.database.retry_fail", e=e), exc_info=True)
            raise
    if last_err is not None:
        raise last_err
    raise RuntimeError(i18n_t("[数据库] with_session 未知失败"))


def _session_wrapper(
    func: Callable[Concatenate[Any, AsyncSession, P], Awaitable[R]],
    *,
    read_only: bool,
) -> Callable[Concatenate[Any, P], Awaitable[R]]:
    @wraps(func)
    async def wrapper(self, *args: P.args, **kwargs: P.kwargs):
        operation = func.__qualname__
        # 复用本任务的连接，避免嵌套写再开一条连接去抢锁。
        # savepoint 失败只回滚这一层，外层 commit 带不走半截语句。
        if not read_only:
            active = _owned_write_session()
            if active is not None:
                bound = active

                async def _nested() -> R:
                    async with bound.begin_nested():
                        return await func(self, bound, *args, **kwargs)

                return await _retry_db(_nested)

        async def _run_in_session(lease: _SqliteDriverLease | None) -> R:
            async with async_maker() as session:
                token: contextvars.Token[_WriteBinding | None] | None = None
                marked_read = False
                try:
                    if not read_only:
                        current = asyncio.current_task()
                        if current is not None:
                            token = _write_session.set(_WriteBinding(session, current))
                    if lease is not None:
                        await _capture_sqlite_driver(session, lease)
                    if read_only:
                        await _mark_read_only(session)
                        marked_read = _db_type == "sqlite"
                    data = await func(self, session, *args, **kwargs)
                    await session.commit()
                    return data
                finally:
                    if token is not None:
                        _write_session.reset(token)
                    # StaticPool 会把连接还回去；query_only 留在连接上会让下一笔写失败。
                    if marked_read:
                        await session.execute(text("PRAGMA query_only=OFF"))

        async def _budgeted_write() -> R:
            # 预算不含等槽时间，只限制已经占住写槽的这一次调用。
            lease = _SqliteDriverLease()
            task: asyncio.Task[R] = asyncio.create_task(
                _run_in_session(lease),
                name=f"db-write:{operation}",
            )
            try:
                done, _pending = await asyncio.wait({task}, timeout=_WRITE_BUDGET_S)
            except asyncio.CancelledError:
                await _stop_write_task(task, lease)
                raise
            if task in done:
                if task.cancelled():
                    raise asyncio.CancelledError
                return task.result()
            lingered = await _stop_write_task(task, lease)
            # 宽限内已经正常返回（含 commit）时，调用方应看到成功。
            if task.done() and not task.cancelled():
                return task.result()
            message = i18n_t(
                "log.database.write_timeout",
                budget=_budget_label(_WRITE_BUDGET_S),
                operation=operation,
            )
            logger.error(message)
            if lingered:
                logger.error(i18n_t("log.database.write_timeout_linger", operation=operation))
            raise DatabaseWriteTimeout(message)

        async def _once() -> R:
            if read_only:
                sem = sqlite_read_semaphore
                if _db_type == "sqlite" and sem is not None:
                    async with sem:
                        return await _run_in_session(None)
                return await _run_in_session(None)
            if _db_type == "sqlite":
                async with sqlite_write_gate.hold(core=_writer_is_core(func)):
                    return await _budgeted_write()
            return await _run_in_session(None)

        return await _retry_db(_once)

    return wrapper  # type: ignore


def with_session(
    func: Callable[Concatenate[Any, AsyncSession, P], Awaitable[R]],
) -> Callable[Concatenate[Any, P], Awaitable[R]]:
    return _session_wrapper(func, read_only=False)


def with_read_session(
    func: Callable[Concatenate[Any, AsyncSession, P], Awaitable[R]],
) -> Callable[Concatenate[Any, P], Awaitable[R]]:
    """SELECT 走独立读槽。SQLite 读连接是 query_only，不进写闸门。"""
    return _session_wrapper(func, read_only=True)


async def get_all_table_ddl(engine: AsyncEngine) -> Dict[str, str]:
    """
    异步获取数据库中所有表的 Create Table 语句 (DDL)。

    :param engine: SQLAlchemy AsyncEngine 实例
    :return: 字典 {表名: CREATE_TABLE_SQL_语句}
    """

    # 定义一个同步函数，用于在 run_sync 中运行
    def _reflect_metadata_sync(conn: Connection) -> MetaData:
        metadata = MetaData()
        metadata.reflect(bind=conn)
        return metadata

    async with engine.connect() as conn:
        metadata: MetaData = await conn.run_sync(_reflect_metadata_sync)
        ddl_map: Dict[str, str] = {}

        # 2. 遍历表对象，生成 DDL
        # 注意：生成 SQL 字符串的操作可以在异步上下文中安全进行，因为内存里已经有了 metadata
        for table_name, table in metadata.tables.items():
            # 使用 CreateTable 构造器将 Table 对象编译成 SQL 字符串
            # compile 即使在 async engine 下也需要传入 engine 或 dialect
            # 这里我们利用 engine.dialect 进行编译
            create_sql = str(CreateTable(table).compile(dialect=engine.dialect))

            ddl_map[table_name] = create_sql.strip()

        return ddl_map


async def get_simple_schema_info(engine: AsyncEngine) -> Dict[str, List[Dict[str, Any]]]:
    def _inspect_sync(conn: Connection):
        inspector = inspect(conn)
        table_names = inspector.get_table_names()
        results = {}
        for t_name in table_names:
            columns_list = inspector.get_columns(t_name)

            serializable_columns: List[Dict[str, Any]] = []

            for column_info in columns_list:
                col_dict = dict(column_info)
                col_dict["type"] = str(column_info["type"])
                serializable_columns.append(col_dict)

            # 将处理后的列表赋值给结果
            results[t_name] = serializable_columns
        return results

    async with engine.connect() as conn:
        return await conn.run_sync(_inspect_sync)


# https://github.com/tiangolo/sqlmodel/issues/264
class BaseIDModel(SQLModel):
    id: int = Field(default=None, primary_key=True, title="序号")

    @classmethod
    @with_read_session
    async def get_distinct_list(
        cls,
        session: AsyncSession,
        column: InstrumentedAttribute[T],
    ):
        result = await session.execute(select(column).distinct())
        r = result.all()
        return r

    @classmethod
    async def batch_insert_data(
        cls,
        datas: Sequence["BaseIDModel"],
    ) -> None:
        if not datas:
            return
        rows = list(datas)
        for start in range(0, len(rows), _UPSERT_CHUNK):
            await cls._add_chunk(rows[start : start + _UPSERT_CHUNK])

    @classmethod
    @with_session
    async def _add_chunk(
        cls,
        session: AsyncSession,
        datas: Sequence["BaseIDModel"],
    ) -> None:
        session.add_all(datas)

    @classmethod
    async def batch_insert_data_with_update(
        cls,
        datas: Sequence["BaseIDModel"],
        update_key: List[str],
        index_elements: List[str],
    ) -> None:
        """
        MySQL需要预先定义约束条件！！
        """
        if not datas:
            return
        rows = list(datas)
        for start in range(0, len(rows), _UPSERT_CHUNK):
            await cls._upsert_chunk(rows[start : start + _UPSERT_CHUNK], update_key, index_elements)

    @classmethod
    @with_session
    async def _upsert_chunk(
        cls,
        session: AsyncSession,
        datas: Sequence["BaseIDModel"],
        update_key: List[str],
        index_elements: List[str],
    ) -> None:
        if not datas:
            return

        values_to_insert = [data.model_dump() for data in datas]
        if _db_type == "sqlite":
            from sqlalchemy.dialects.sqlite import insert

            stmt = insert(cls)
            update_stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={k: stmt.excluded[k] for k in update_key},
            )
        elif _db_type == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            stmt = insert(cls)
            update_stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={k: stmt.excluded[k] for k in update_key},
            )
        elif _db_type == "mysql":
            from sqlalchemy.dialects.mysql import insert

            stmt = insert(cls)
            update_dict = {col: getattr(stmt.inserted, col) for col in update_key}
            update_stmt = stmt.on_duplicate_key_update(**update_dict)
        else:
            raise ValueError(i18n_t("[GsCore] [数据库] 不支持 {_db_type} 数据库!", _db_type=_db_type))

        await session.execute(update_stmt, values_to_insert)

    @classmethod
    @with_session
    async def update_data_by_data(
        cls,
        session: AsyncSession,
        select_data: Dict,
        update_data: Dict,
    ) -> int:
        """📝简单介绍:

            基类的数据更新方法

        🌱参数:

            🔹select_data (`Dict`):
                    寻找数据条件, 例如`{"user_id": `event.bot_id`}`

            🔹`update_data (`Dict`)`:
                    要更新的数据

        🚀使用范例:

            `await GsUser.update_data_by_data(`
                `select_data={"user_id": `event.bot_id`}, `
                `update_data={"bot_id": 'onebot', "uid": '22'}`
            `)`

        ✅返回值:

            🔸`int`: 成功为0, 失败为-1（未找到数据则无法更新）
        """
        sql = update(cls)
        for k, v in select_data.items():
            sql = sql.where(getattr(cls, k) == v)

        if update_data:
            query = sql.values(**update_data)
            query.execution_options(synchronize_session="fetch")
            await session.execute(query)
            return 0
        return -1

    @classmethod
    def get_gameid_name(cls, game_name: Optional[str] = None) -> str:
        """📝简单介绍:

            快速获取uid的列名

        🌱参数:

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    假设传入`None`会返回`uid`，而传入`sr`会返回`sr_uid`
                    特殊的, 传入`gs`也会返回`uid`!

                    注意: 仅接受「模型上真实存在的游戏 UID 列」对应短名。
                    米游社账号维度（``account``）等非游戏 UID 标识不可传入，
                    否则会在校验阶段抛出 ``ValueError``，避免拼出
                    ``account_uid`` 后在 ``getattr`` 处变成难排查的
                    ``AttributeError``。

        🚀使用范例:

            `await GsUser.get_gameid_name('sr')`

        ✅返回值:

            🔸`str`: 游戏uid对应列名，默认为`uid`
        """
        if not game_name or game_name == "gs":
            col_name = "uid"
        else:
            col_name = f"{game_name}_uid"

        # 入口统一校验：所有 getattr(cls, get_gameid_name(...)) 依赖此保证
        if not hasattr(cls, col_name):
            raise ValueError(
                f"{cls.__name__} 不存在字段 {col_name!r} "
                f"(game_name={game_name!r})；"
                f"game_name 只能是模型上真实存在的游戏 UID 列短名"
                f"（如 None/'gs'/'sr'/'zzz'/'bb'/'bbb'/'wd'），"
                f"米游社账号维度请用 mys_id 等字段查询，"
                f"不要传 game_name='account'"
            )
        return col_name

    @classmethod
    @with_session
    async def full_insert_data(cls, session: AsyncSession, **data) -> int:
        """📝简单介绍:

            数据库基类基础插入数据方法

        🌱参数:

            🔹`**data`
                    插入的数据, 入参列名等于数据即可

        🚀使用范例:

            `await GsUser.full_insert_data(uid='123',cookie='233', ...)`

        ✅返回值:

            🔸`int`: 恒为0
        """
        session.add(cls(**data))
        return 0

    @classmethod
    @with_session
    async def delete_row(
        cls: Type[T_BaseIDModel],
        session: AsyncSession,
        **data,
    ) -> int:
        """
        ✅返回值:

            🔸`int`: 如为1则删除成功，否则删除失败(数据不存在)
        """
        row_data = await cls.select_rows(**data)
        logger.trace(i18n_t("log.database.gscore_db_delete_row_data", row_data=row_data))
        if row_data:
            for row in row_data:
                await session.delete(row)
            return 1
        else:
            return 0

    @classmethod
    @with_read_session
    async def select_rows(
        cls: Type[T_BaseIDModel],
        session: AsyncSession,
        distinct: bool = False,
        **data,
    ):
        """📝简单介绍:

            数据库基类基础选择数据方法

        🌱参数:

            🔹`**data`
                    选择的数据, 入参列名等于数据即可

        🚀使用范例:

            `await GsUser.base_select_data(uid='100740568')`

        ✅返回值:

            🔸`Optional[List[T_BaseIDModel]]`: 选中全部符合条件的数据，或者为`None`
        """
        stmt = select(cls)
        for k, v in data.items():
            stmt = stmt.where(getattr(cls, k) == v)
        if distinct:
            stmt = stmt.distinct()
        result = await session.execute(stmt)
        data = result.scalars().all()
        logger.trace(i18n_t("log.database.gscore_data_selected", data=data))
        return data

    @classmethod
    async def base_select_data(cls: Type[T_BaseIDModel], **data) -> Optional[T_BaseIDModel]:
        """📝简单介绍:

            数据库基类基础选择数据方法

        🌱参数:

            🔹`**data`
                    选择的数据, 入参列名等于数据即可

        🚀使用范例:

            `await GsUser.base_select_data(uid='100740568')`

        ✅返回值:

            🔸`Optional[T_BaseIDModel]`: 选中符合条件的第一个数据，或者为`None`
        """
        data = await cls.select_rows(**data)
        return data[0] if data else None

    @classmethod
    async def data_exist(cls, **data) -> bool:
        """📝简单介绍:

            数据库基类基础判定数据是否存在的方法


        🚀使用范例:

            `await GsUser.data_exist(uid='100740568')`

        ✅返回值:

            🔸`bool`: 存在为`True`
        """
        return bool(await cls.base_select_data(**data))


class BaseBotIDModel(BaseIDModel):
    bot_id: str = Field(title="平台")

    @classmethod
    @with_session
    async def update_data_by_uid_without_bot_id(
        cls,
        session: AsyncSession,
        uid: str,
        game_name: Optional[str] = None,
        **data,
    ) -> int:
        """📝简单介绍:

            基类方法，通过传入uid查找并更新数据，无需bot_id

        🌱参数:

            🔹uid (`str`):
                    根据该入参寻找相应数据

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsUser.update_data_by_uid_without_bot_id(uid, cookie='2')`

        ✅返回值:

            🔸`int`: 成功为`0`, 失败为`-1`
        """
        sql = update(cls).where(
            getattr(cls, cls.get_gameid_name(game_name)) == uid,
        )
        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session="fetch")
            await session.execute(query)
            return 0
        return -1

    @classmethod
    @with_session
    async def update_data_by_xx(
        cls,
        session: AsyncSession,
        by: Dict[str, Any],
        **data,
    ) -> int:
        """📝简单介绍:

            基类方法，通过传入`by`和`**data`查找并更新数据

        🌱参数:

            🔹by (`Dict[str, Any]`)
                    根据该入参寻找相应数据

            🔹**data
                    根据该入参修改数据

        🚀使用范例:

            `await GsUser.update_data_by_xx({'uid': '233'}, cookie=ck)`

        ✅返回值:

            🔸`int`: 成功为`0`, 失败为`-1`
        """
        sql = update(cls)
        for i in by:
            sql = sql.where(getattr(cls, i) == by[i])
        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session="fetch")
            await session.execute(query)
            return 0
        return -1

    @classmethod
    @with_session
    async def update_data_by_uid(
        cls,
        session: AsyncSession,
        uid: str,
        bot_id: Optional[str] = None,
        game_name: Optional[str] = None,
        **data,
    ) -> int:
        """📝简单介绍:

            基类方法，通过传入`uid`和`bot_id`查找并更新数据

        🌱参数:

            🔹uid (`str`)
                    根据该入参寻找相应数据

            🔹bot_id (`str`)
                    根据该入参寻找相应数据

            🔹game_name (`Optional[str]`, 默认是 `None`)
                    根据该入参修改寻找列名

            🔹**data
                    根据该入参修改数据

        🚀使用范例:

            `await GsUser.update_data_by_uid(uid, 'onebot', cookie='2')`

        ✅返回值:

            🔸`int`: 成功为`0`, 失败为`-1`
        """
        uid_name = cls.get_gameid_name(game_name)
        uid_col = _mapped_column(cls, uid_name)
        found = await session.execute(select(cls).where(col(uid_col) == uid))
        if found.scalars().first() is None:
            # 同任务复用外层 session，不再另开一条写连接。
            data[uid_name] = uid
            return await cls.full_insert_data(bot_id=bot_id, **data)

        sql = update(cls).where(col(uid_col) == uid)

        if bot_id is not None:
            sql = sql.where(and_(cls.bot_id == bot_id))

        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session="fetch")
            await session.execute(query)
            return 0
        return -1

    @classmethod
    @with_read_session
    async def get_all_data(
        cls: Type[T_BaseIDModel],
        session: AsyncSession,
    ):
        rdata = await session.execute(select(cls))
        data = rdata.scalars().all()
        return data


class BaseModel(BaseBotIDModel):
    user_id: str = Field(title="账号")

    ################################
    # 基本的增删改查 #
    ################################

    @classmethod
    @with_read_session
    async def select_data_list(
        cls: Type[T_BaseModel],
        session: AsyncSession,
        user_id: str,
        bot_id: Optional[str] = None,
    ):
        """📝简单介绍:

            基类的数据选择方法

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`Optional[str]`, 默认是 `None`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

        🚀使用范例:

            `await GsUser.select_data(user_id='444888', bot_id='onebot')`

        ✅返回值:

            🔸`Optional[Sequence[T_BaseModel]]`: 选中符合条件的全部数据，不存在则为`None`
        """
        if bot_id is None:
            sql = select(cls).where(cls.user_id == user_id)
        else:
            sql = select(cls).where(and_(cls.user_id == user_id, cls.bot_id == bot_id))
        result = await session.execute(sql)
        data = result.scalars().all()
        return data if data else None

    @classmethod
    async def select_data(
        cls: Type[T_BaseModel],
        user_id: str,
        bot_id: Optional[str] = None,
    ) -> Optional[T_BaseModel]:
        """📝简单介绍:

            基类的数据选择方法

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`Optional[str]`, 默认是 `None`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

        🚀使用范例:

            `await GsUser.select_data(user_id='444888', bot_id='onebot')`

        ✅返回值:

            🔸`Optional[T_BaseModel]`: 选中符合条件的第一个数据，不存在则为`None`
        """
        data = await cls.select_data_list(user_id, bot_id)
        return data[0] if data else None

    @classmethod
    @with_session
    async def insert_data(
        cls: Type[T_BaseModel],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        **data,
    ) -> int:
        """📝简单介绍:

            基类的数据插入方法

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹`**data`:
                    要插入的数据

        🚀使用范例:

            `await GsUser.insert_data(user_id='4', bot_id='onebot', uid='22')`

        ✅返回值:

            🔸`int`: 恒为0
        """
        cond = {"user_id": user_id, "bot_id": bot_id}
        if "mys_id" in data:
            cond["mys_id"] = data["mys_id"]
        elif "uid" in data:
            cond["uid"] = data["uid"]
        if await cls.data_exist(**cond):
            await cls.update_data(user_id, bot_id, **data)
        else:
            session.add(cls(user_id=user_id, bot_id=bot_id, **data))
        return 0

    @classmethod
    @with_session
    async def delete_data(
        cls: Type[T_BaseModel],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        **data,
    ) -> int:
        """📝简单介绍:

            基类的数据删除方法

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

        🚀使用范例:

            `await GsUser.delete_data(user_id='4', bot_id='onebot', uid='22')`

        ✅返回值:

            🔸`int`: 恒为0
        """
        await session.delete(cls(user_id=user_id, bot_id=bot_id, **data))
        return 0

    @classmethod
    @with_session
    async def update_data(
        cls: Type[T_BaseModel],
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        **data,
    ) -> int:
        """📝简单介绍:

            基类的数据更新方法

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹`**data`:
                    要更新的数据

        🚀使用范例:

            `await GsUser.update_data(user_id='4', bot_id='onebot', uid='22')`

        ✅返回值:

            🔸`int`: 成功为0, 失败为-1（未找到数据则无法更新）
        """
        sql = update(cls).where(and_(cls.user_id == user_id, cls.bot_id == bot_id))
        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session="fetch")
            await session.execute(query)
            return 0
        return -1


class Bind(BaseModel):
    group_id: Optional[str] = Field(title="群号")

    ################################
    # 额外的扩展方法 #
    ################################
    @classmethod
    async def get_uid_list_by_game(
        cls: Type[T_Bind],
        user_id: str,
        bot_id: str,
        game_name: Optional[str] = None,
    ) -> Optional[List[str]]:
        """📝简单介绍:

            基础`Bind`类的扩展方法, 根据传入的`bot_id`和`user_id`拿到绑定的uid列表

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsUser.get_uid_list_by_game(user_id='4', bot_id='onebot')`

        ✅返回值:

            🔸`Optional[List[str]]`: 如果有数据则为uid的列表，无则为`None`
        """
        result = await cls.select_data(user_id, bot_id)
        if result is None:
            return None

        uid = getattr(result, cls.get_gameid_name(game_name))
        if uid is None:
            return None
        else:
            uid_list = uid.split("_")

        if uid_list:
            return uid_list
        else:
            return None

    @classmethod
    async def get_uid_by_game(
        cls: Type[T_Bind],
        user_id: str,
        bot_id: str,
        game_name: Optional[str] = None,
    ) -> Optional[str]:
        """📝简单介绍:

            基础`Bind`类的扩展方法, 根据传入的`bot_id`和`user_id`拿到单个绑定的uid

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsUser.get_uid_by_game(user_id='4', bot_id='onebot')`

        ✅返回值:

            🔸`Optional[str]`: 如果有绑定数据则返回当前绑定uid, 没有则为`None`
        """
        result = await cls.get_uid_list_by_game(user_id, bot_id, game_name)
        if result is None or not result:
            return None
        return result[0]

    @classmethod
    async def bind_exists(
        cls: Type[T_Bind],
        user_id: str,
        bot_id: str,
    ) -> bool:
        """
        查询当前user_id是否已有绑定数据
        """
        return bool(await cls.select_data(user_id, bot_id))

    @classmethod
    async def insert_uid(
        cls: Type[T_Bind],
        user_id: str,
        bot_id: str,
        uid: str,
        group_id: Optional[str] = None,
        lenth_limit: Optional[int] = None,
        is_digit: Optional[bool] = True,
        game_name: Optional[str] = None,
    ) -> int:
        """📝简单介绍:

            基础`Bind`类的扩展方法, 为给定的`user_id`和`bot_id`插入一条uid绑定数据

            可支持多uid的绑定, 如果绑定多个uid, 则数据库中uid列将会用`_`分割符相连接

            可以使用`cls.get_uid_list_by_game()`方法获取相应多绑定uid列表

            或者使用`cls.get_uid_by_game()`方法获得当前绑定uid（单个）

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹uid (`str`):
                    将要插入的uid数据

            🔹group_id (`Optional[str]`, 默认是 `None`):
                    将要插入的群组数据，为绑定uid提供群组绑定

            🔹lenth_limit (`Optional[int]`, 默认是 `None`):
                    如果有传该参数, 当uid位数不等于该参数、或uid位数为0的时候, 返回`-1`

            🔹is_digit (`Optional[bool]`, 默认是 `True`):
                    如果有传该参数, 当uid不为全数字的时候, 返回`-3`

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsBind.insert_uid(qid, ev.bot_id, uid, ev.group_id, 9)`

        ✅返回值:

            🔸`int`: 如果该UID已绑定, 则返回`-2`, 成功则为`0`, 合法校验失败为`-3`或`-1`
        """
        result = await cls.get_uid_list_by_game(user_id, bot_id, game_name)

        result = [i for i in result if i] if result else None

        if lenth_limit:
            if len(uid) != lenth_limit:
                return -1

        if is_digit:
            if not uid.isdigit():
                return -3
        if not uid:
            return -1

        if result is None and not await cls.bind_exists(user_id, bot_id):
            return await cls.insert_data(
                user_id,
                bot_id,
                **{cls.get_gameid_name(game_name): uid, "group_id": group_id},
            )
        elif result is None:
            new_uid = uid
        elif uid in result:
            return -2
        else:
            result.append(uid)
            new_uid = "_".join(result)
        await cls.update_data(
            user_id,
            bot_id,
            **{cls.get_gameid_name(game_name): new_uid},
        )
        return 0

    @classmethod
    async def delete_uid(
        cls: Type[T_Bind],
        user_id: str,
        bot_id: str,
        uid: str,
        game_name: Optional[str] = None,
    ) -> int:
        """📝简单介绍:

            基础`Bind`类的扩展方法, 根据给定的`user_id`和`bot_id`和`uid`删除一个uid

            该方法不会删除行，如果只有一个uid会置空，如果同时绑定多个uid只会删除其中一个

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹uid (`str`):
                    将要删除的uid数据

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsBind.delete_uid(qid, ev.bot_id, uid)`

        ✅返回值:

            🔸`int`: 失败为`-1`, 成功为`0`
        """
        result = await cls.get_uid_list_by_game(user_id, bot_id, game_name)
        if result is None:
            return -1

        if uid not in result:
            return -1

        result.remove(uid)

        result = [i for i in result if i] if result else []
        new_uid = "_".join(result)

        if not new_uid:
            new_uid = None

        await cls.update_data(
            user_id,
            bot_id,
            **{cls.get_gameid_name(game_name): new_uid},
        )
        return 0

    @classmethod
    @with_read_session
    async def get_all_uid_list_by_game(
        cls: Type[T_Bind],
        session: AsyncSession,
        bot_id: str,
        game_name: Optional[str] = None,
    ) -> List[str]:
        """📝简单介绍:

            基础`Bind`类的扩展方法, 根据给定的`bot_id`获取全部user绑定的uid列表

        🌱参数:

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsBind.get_all_uid_list_by_game(ev.bot_id)`

        ✅返回值:

            🔸`List[str]`: 一个uid的列表, 如果没有任何用户的绑定信息将返回`[]`
        """
        sql = select(cls).where(cls.bot_id == bot_id)
        result = await session.execute(sql)
        data = result.scalars().all()
        uid_list: List[str] = []
        for item in data:
            uid = getattr(item, cls.get_gameid_name(game_name))
            if uid is not None and uid:
                game_uid_list: List[str] = uid.split("_")
                uid_list.extend(game_uid_list)
        return uid_list

    @classmethod
    async def switch_uid_by_game(
        cls: Type[T_Bind],
        user_id: str,
        bot_id: str,
        uid: Optional[str] = None,
        game_name: Optional[str] = None,
    ) -> int:
        """📝简单介绍:

            基础`Bind`类的扩展方法, 根据给定的`bot_id`和`user_id`定位数据，并切换当前uid

            如果不传uid参数则默认切换下个uid

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹uid (`Optional[str]`, 默认是 `None`):
                    将要切换的uid数据, 可以不传

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsBind.switch_uid_by_game(qid, ev.bot_id, uid)`

        ✅返回值:

            🔸`int`:

                成功返回`0`

                如果不存在绑定记录,则返回`-1`

                如果传了UID但是不存在绑定列表,则返回`-2`

                如果绑定UID列表不足2个,返回`-3`
        """
        uid_list = await cls.get_uid_list_by_game(user_id, bot_id, game_name)
        if not uid_list:
            return -1
        elif len(uid_list) <= 1:
            return -3
        elif uid is None or not uid:
            uid = uid_list[1]
            old_uid = uid_list[0]
            uid_list.remove(uid)
            uid_list.remove(old_uid)
            uid_list.insert(0, uid)
            uid_list.append(old_uid)
        elif uid not in uid_list:
            return -2
        else:
            uid_list.remove(uid)
            uid_list.insert(0, uid)
        await cls.update_data(
            user_id,
            bot_id,
            **{cls.get_gameid_name(game_name): "_".join(uid_list)},
        )
        return 0

    @classmethod
    async def get_bind_group_list(cls: Type[T_Bind], user_id: str, bot_id: str) -> List[str]:
        """获取传入`user_id`和`bot_id`对应的绑定群列表"""
        data: Optional["Bind"] = await cls.select_data(user_id, bot_id)
        return data.group_id.split("_") if data and data.group_id else []

    @classmethod
    async def get_bind_group(cls: Type[T_Bind], user_id: str, bot_id: str) -> Optional[str]:
        """获取传入`user_id`和`bot_id`对应的绑定群（如多个则返回第一个）"""
        data = await cls.get_bind_group_list(user_id, bot_id)
        return data[0] if data else None

    @classmethod
    @with_read_session
    async def get_group_all_uid(cls: Type[T_Bind], session: AsyncSession, group_id: str):
        """根据传入`group_id`获取该群号下所有绑定`uid`列表"""
        result = await session.scalars(select(cls).where(col(cls.group_id).contains(group_id)))
        data = result.all()
        return data[0] if data else None


class User(BaseModel):
    cookie: str = Field(default=None, title="Cookie")
    stoken: Optional[str] = Field(default=None, title="Stoken")
    status: Optional[str] = Field(default=None, title="状态")
    push_switch: str = Field(default="off", title="全局推送开关")
    sign_switch: str = Field(default="off", title="自动签到")

    @classmethod
    @with_read_session
    async def select_data_by_uid(
        cls: Type[T_User],
        session: AsyncSession,
        uid: str,
        game_name: Optional[str] = None,
    ):
        """📝简单介绍:

            基础`User`类的数据选择方法

        🌱参数:

            🔹uid (`str`):
                    传入的用户uid, 一般是该游戏的用户唯一识别id

        🚀使用范例:

            `await GsUser.select_data_by_uid(uid='100740568')`

        ✅返回值:

            🔸`Optional[T_BaseModel]`: 选中符合条件的第一个数据，不存在则为`None`
        """
        result = await session.execute(
            select(cls).where(
                getattr(cls, cls.get_gameid_name(game_name)) == uid,
            )
        )
        data = result.scalars().all()
        return data[0] if data else None

    @classmethod
    @with_read_session
    async def get_user_all_data_by_user_id(cls: Type[T_User], session: AsyncSession, user_id: str):
        """📝简单介绍:

            基础`User`类的数据选择方法, 获取该`user_id`绑定的全部数据实例

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

        🚀使用范例:

            `await GsUser.get_user_all_data_by_user_id(user_id='2333')`

        ✅返回值:

            🔸`Optional[T_BaseModel]`: 选中符合条件的数据列表，不存在则为`None`
        """
        result = await session.execute(select(cls).where(cls.user_id == user_id))
        data = result.scalars().all()
        return data if data else None

    @classmethod
    async def get_user_attr(
        cls: Type[T_User],
        user_id: str,
        bot_id: str,
        attr: str,
    ) -> Optional[Any]:
        """📝简单介绍:

            根据传入的`user_id`和`bot_id`选择数据实例，然后返回数据的某个属性的值

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

            🔹bot_id (`str`):
                    传入的bot_id, 例如`onebot`, 一般直接取`event.bot_id`

            🔹attr (`str`):
                    想要获取的该数据的属性

        🚀使用范例:

            `await cls.get_user_attr(user_id, bot_id, 'cookie')`

        ✅返回值:

            🔸`Optional[Any]`: 可能是任何值，如果没获取到数据则为`None`
        """
        result = await cls.select_data(user_id, bot_id)
        return getattr(result, attr) if result else None

    @classmethod
    async def get_user_attr_by_uid(
        cls: Type[T_User],
        uid: str,
        attr: str,
        game_name: Optional[str] = None,
    ) -> Optional[Any]:
        """根据传入的`uid`选择数据实例，然后返回数据的`attr`属性的值

        如果没获取到数据则为`None`
        """
        result = await cls.select_data_by_uid(uid, game_name)
        return getattr(result, attr) if result else None

    @classmethod
    async def get_user_attr_by_user_id(
        cls: Type[T_User],
        user_id: str,
        attr: str,
    ) -> Optional[Any]:
        """根据传入的`user_id`选择数据实例，然后返回数据的`attr`属性的值

        如果没获取到数据则为`None`
        """
        result = await cls.select_data(user_id)
        return getattr(result, attr) if result else None

    @classmethod
    @with_session
    async def mark_invalid(cls: Type[T_User], session: AsyncSession, cookie: str, mark: str):
        """令一个cookie所对应数据的`status`值为传入的mark

        例如：mark值可以是`error`, 标记该Cookie已失效
        """
        sql = update(cls).where(and_(cls.cookie == cookie, true())).values(status=mark)
        await session.execute(sql)
        return True

    @classmethod
    async def get_user_cookie_by_uid(cls: Type[T_User], uid: str, game_name: Optional[str] = None) -> Optional[str]:
        """根据传入的`uid`选择数据实例，然后返回该数据的`cookie`值

        如果没获取到数据则为`None`
        """
        return await cls.get_user_attr_by_uid(uid, "cookie", game_name)

    @classmethod
    async def get_user_cookie_by_user_id(cls: Type[T_User], user_id: str, bot_id: str) -> Optional[str]:
        """根据传入的`user_id`选择数据实例，然后返回该数据的`cookie`值

        如果没获取到数据则为`None`
        """
        return await cls.get_user_attr(user_id, bot_id, "cookie")

    @classmethod
    async def get_user_stoken_by_uid(cls: Type[T_User], uid: str, game_name: Optional[str] = None) -> Optional[str]:
        """根据传入的`uid`选择数据实例，然后返回该数据的`stoken`值

        如果没获取到数据则为`None`
        """
        return await cls.get_user_attr_by_uid(uid, "stoken", game_name)

    @classmethod
    async def get_user_stoken_by_user_id(cls: Type[T_User], user_id: str, bot_id: str) -> Optional[str]:
        """根据传入的`user_id`选择数据实例，然后返回该数据的`stoken`值

        如果没获取到数据则为`None`
        """
        return await cls.get_user_attr(user_id, bot_id, "stoken")

    @classmethod
    async def cookie_validate(cls: Type[T_User], uid: str, game_name: Optional[str] = None) -> bool:
        """根据传入的`uid`选择数据实例, 校验数据中的`cookie`是否有效

        方法是判断数据中的`status`是否为空值, 如果没有异常标记, 则为`True`
        """
        data = await cls.get_user_attr_by_uid(uid, "status", game_name)
        if not data:
            return True
        else:
            return False

    @classmethod
    @with_read_session
    async def get_switch_open_list(cls: Type[T_User], session: AsyncSession, switch_name: str) -> List[T_User]:
        """📝简单介绍:

            根据表定义的结构, 根据传入的`switch_name`, 寻找表数据中的该列

            如果不存在该列，则选中`cls.push_switch`该列

            对所有数据中，该列满足`!= 'off'`的数据，标记为有效数据

            返回有效数据的列表

        🌱参数:

            🔹switch_name (`str`):
                    寻找表数据列名

        🚀使用范例:

            `await GsUser.get_switch_open_list('sign_switch')`

        ✅返回值:

            🔸`List[T_User]`: 有效数据的列表, 如没有则为`[]`
        """
        _switch = getattr(cls, switch_name, cls.push_switch)
        sql = select(cls).filter(and_(_switch != "off", true()))
        data = await session.execute(sql)
        data_list = data.scalars().all()
        return [user for user in data_list]

    @classmethod
    @with_read_session
    async def get_all_user(
        cls: Type[T_User],
        session: AsyncSession,
        without_error: bool = True,
    ) -> Sequence[T_User]:
        """📝简单介绍:

            基础`User`类的扩展方法, 获取到全部的数据列表

            不一定会返回该表中所有的数据, 返回的数据中`cookie`必定存在值

        🌱参数:

            🔹without_error (`bool`, 默认是 `True`):
                    如果为`True`, 则会排除返回列表中存在`status != None`的数据

        🚀使用范例:

            `await GsUser.get_all_user()`

        ✅返回值:

            🔸`List[T_User]`: 有效数据的列表, 如没有则为`[]`
        """
        if without_error:
            sql = select(cls).where(cls.status == null(), cls.cookie != null(), cls.cookie != "")
        else:
            sql = select(cls).where(cls.cookie != null(), cls.cookie != "")
        result = await session.execute(sql)
        data = result.scalars().all()
        return data

    @classmethod
    async def get_all_cookie(cls) -> List[str]:
        """获得表数据中全部的`cookie`列表"""
        data = await cls.get_all_user()
        return [_u.cookie for _u in data if _u.cookie]

    @classmethod
    async def get_all_stoken(cls) -> List[str]:
        """获得表数据中全部的`stoken`列表"""
        data = await cls.get_all_user()
        return [_u.stoken for _u in data if _u.stoken]

    @classmethod
    async def get_all_error_cookie(cls) -> List[str]:
        """获得表数据中，`status != None`情况下的所有`cookie`列表

        也就是全部失效CK的列表
        """
        data = await cls.get_all_user()
        return [_u.cookie for _u in data if _u.cookie and _u.status]

    @classmethod
    async def get_all_push_user_list(cls: Type[T_User]) -> List[T_User]:
        """获得表数据中全部的`push_switch != off`的数据列表"""
        data = await cls.get_all_user()
        return [user for user in data if user.push_switch != "off"]

    @classmethod
    async def get_all_sign_user_list(cls: Type[T_User]) -> List[T_User]:
        """获得表数据中全部的`sign_switch!= off`的数据列表"""
        data = await cls.get_all_user()
        return [user for user in data if user.sign_switch != "off"]

    @classmethod
    async def get_push_user_list(
        cls: Type[T_User],
        push_title: Optional[str] = None,
    ) -> List[T_User]:
        """获得表数据中全部的`{push_title}_push_switch!= off`的数据列表"""
        if push_title is None:
            return await cls.get_all_push_user_list()
        data = await cls.get_all_user()
        return [user for user in data if getattr(user, f"{push_title}_push_switch") != "off"]

    @classmethod
    async def get_sign_user_list(
        cls: Type[T_User],
        sign_title: Optional[str] = None,
    ) -> List[T_User]:
        """获得表数据中全部的`{sign_title}_sign_switch!= off`的数据列表"""
        if sign_title is None:
            return await cls.get_all_sign_user_list()
        data = await cls.get_all_user()
        return [user for user in data if getattr(user, f"{sign_title}_sign_switch") != "off"]

    @classmethod
    async def user_exists(cls, uid: str, game_name: Optional[str] = None) -> bool:
        """根据传入`uid`，判定数据是否存在"""
        data = await cls.select_data_by_uid(uid, game_name)
        return True if data else False

    @classmethod
    @with_session
    async def get_random_cookie(
        cls: Type[T_User],
        session: AsyncSession,
        uid: str,
        cache_model: Optional[Type["Cache"]] = None,
        condition: Optional[Dict[str, str]] = None,
        game_name: Optional[str] = None,
    ) -> Optional[str]:
        """📝简单介绍:

            基础`User`类的扩展方法, 返回一个随机的cookie

            如果该uid存在绑定cookie, 则返回他绑定的cookie

            如果传入了`cache_model`并且该uid存在cache_model定义的表数据中，返回该cookie

            如果定义了condition, 则选取随机cookie时遵照此规则

        🌱参数:

            🔹uid (`str`):
                    传入的用户uid, 一般是该游戏的用户唯一识别id

            🔹cache_model (`Optional[Type["Cache"]]`, 默认是 `None`):
                    继承基础`Cache`缓存表的模型, 例如`GsCache`

            🔹condition (`Optional[Dict[str, str]]`, 默认是 `None`):
                    字典结构, 寻找表中符合`key == value`条件的值

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    根据该入参寻找相应列名

        🚀使用范例:

            `await GsUser.get_random_cookie(
            uid, GsCache, {'region': server}, game_name='sr'
        )`

        ✅返回值:

            🔸`Optional[str]`: 如找到符合条件的cookie则返回，没有则为`None`
        """
        # 有绑定自己CK 并且该CK有效的前提下，优先使用自己CK
        if await cls.user_exists(uid, game_name) and await cls.cookie_validate(uid, game_name):
            return await cls.get_user_cookie_by_uid(uid, game_name)

        # 自动刷新缓存
        # await self.delete_error_cache()
        # 获得缓存库Ck
        if cache_model is not None:
            cache_data = await cache_model.select_cache_cookie(uid, game_name)
            if cache_data is not None:
                return cache_data

        # 随机取CK
        if condition:
            for i in condition:
                sql = select(cls).where(getattr(cls, i) == condition[i]).order_by(func.random())
                data = await session.execute(sql)
                user_list = data.scalars().all()
                break
            else:
                user_list = await cls.get_all_user()
        else:
            user_list = await cls.get_all_user()

        for user in user_list:
            if not user.status and user.cookie:
                if cache_model:
                    # 进入缓存
                    await cache_model.insert_cache_data(
                        user.cookie,
                        **{cls.get_gameid_name(game_name): uid},
                    )
                return user.cookie
            continue
        else:
            return None

    @classmethod
    @with_session
    async def delete_user_data_by_uid(cls, session: AsyncSession, uid: str, game_name: Optional[str] = None) -> bool:
        """根据给定的`uid`获取数据后, 删除整行数据

        如果该数据存在, 删除后返回`True`, 不存在, 则返回`False`
        """
        if await cls.user_exists(uid, game_name):
            sql = delete(cls).where(getattr(cls, cls.get_gameid_name(game_name)) == uid)
            await session.execute(sql)
            return True
        return False


class Cache(BaseIDModel):
    cookie: str = Field(default=None, title="Cookie")

    @classmethod
    @with_read_session
    async def select_cache_cookie(
        cls: Type[T_Cache],
        session: AsyncSession,
        uid: str,
        game_name: Optional[str],
    ) -> Optional[str]:
        """根据给定的`uid`获取表中存在缓存的`cookie`并返回"""
        sql = select(cls).where(getattr(cls, cls.get_gameid_name(game_name)) == uid)
        result = await session.execute(sql)
        data = result.scalars().all()
        return data[0].cookie if len(data) >= 1 else None

    @classmethod
    @with_session
    async def delete_error_cache(cls: Type[T_Cache], session: AsyncSession, user: Type["User"]) -> bool:
        """根据给定的`user`模型中, 查找该模型所有数据的status

        若`status != None`, 则代表该数据cookie有问题

        查找`Cache`表中该cookie对应数据行，并删除

        恒返回`True`
        """
        data = await user.get_all_error_cookie()
        for cookie in data:
            sql = delete(cls).where(and_(cls.cookie == cookie, true()))
            await session.execute(sql)
        return True

    @classmethod
    @with_session
    async def delete_all_cache(cls, session: AsyncSession, user: Type["User"]) -> bool:
        """删除整个表的数据

        根据给定的`user`模型中, 查找该模型所有数据的status

        若`status == limit30`, 则代表该数据cookie限制可能已回复

        清除`User`表中该类cookie的status, 令其重新为`None`
        """
        sql = (
            update(user)
            .where(and_(user.status == "limit30"))
            .values(status=None)
            .execution_options(synchronize_session="fetch")
        )
        empty_sql = delete(cls)
        await session.execute(sql)
        await session.execute(empty_sql)
        return True

    @classmethod
    @with_session
    async def refresh_cache(cls, session: AsyncSession, uid: str, game_name: Optional[str] = None) -> bool:
        """删除指定`uid`的数据行"""
        await session.execute(delete(cls).where(getattr(cls, cls.get_gameid_name(game_name)) == uid))
        return True

    @classmethod
    @with_session
    async def insert_cache_data(cls, session: AsyncSession, cookie: str, **data) -> bool:
        """新增指定`cookie`的数据行, `**data`为数据"""
        new_data = cls(cookie=cookie, **data)
        session.add(new_data)
        return True


class Push(BaseBotIDModel):
    @classmethod
    @with_read_session
    async def select_data_by_uid(
        cls: Type[T_Push],
        session: AsyncSession,
        uid: str,
        game_name: Optional[str] = None,
    ) -> Optional[T_Push]:
        """📝简单介绍:

            基础`Push`类的数据选择方法

        🌱参数:

            🔹uid (`str`):
                    传入的用户uid, 一般是该游戏的用户唯一识别id

        🚀使用范例:

            `await GsPush.select_data_by_uid(uid='100740568')`

        ✅返回值:

            🔸`Optional[T_BaseModel]`: 选中符合条件的第一个数据，不存在则为`None`
        """
        result = await session.execute(
            select(cls).where(
                getattr(cls, cls.get_gameid_name(game_name)) == uid,
            )
        )
        data = result.scalars().all()
        return data[0] if data else None
