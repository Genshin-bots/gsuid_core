from functools import wraps
from typing_extensions import ParamSpec, Concatenate
from typing import (
    Any,
    Dict,
    List,
    Type,
    TypeVar,
    Callable,
    Optional,
    Awaitable,
)

from sqlalchemy.future import select
from sqlalchemy import delete, update
from sqlalchemy.orm import sessionmaker
from sqlmodel import Field, SQLModel, col
from sqlalchemy.sql.expression import func, null
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from gsuid_core.data_store import get_res_path

T_BaseModel = TypeVar('T_BaseModel', bound='BaseModel')
T_BaseIDModel = TypeVar('T_BaseIDModel', bound='BaseIDModel')
T_User = TypeVar('T_User', bound='User')
T_Push = TypeVar('T_Push', bound='Push')
P = ParamSpec("P")
R = TypeVar("R")

db_url = str(get_res_path().parent / 'GsData.db')
url = f'sqlite+aiosqlite:///{db_url}'
engine = create_async_engine(url, pool_recycle=1500)
async_maker = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def with_session(
    func: Callable[Concatenate[Any, AsyncSession, P], Awaitable[R]]
) -> Callable[Concatenate[Any, P], Awaitable[R]]:
    @wraps(func)
    async def wrapper(self, *args: P.args, **kwargs: P.kwargs):
        async with async_maker() as session:
            return await func(self, session, *args, **kwargs)

    return wrapper  # type: ignore


class BaseIDModel(SQLModel):
    id: Optional[int] = Field(default=None, primary_key=True, title='序号')

    @classmethod
    def get_gameid_name(cls, game_name: Optional[str] = None) -> str:
        '''📝简单介绍:

            快速获取uid的列名

        🌱参数:

            🔹game_name (`Optional[str]`, 默认是 `None`):
                    假设传入`None`会返回`uid`，而传入`sr`会返回`sr_uid`

        🚀使用范例:

            `await GsUser.get_gameid_name('sr')`

        ✅返回值:

            🔸`str`: 游戏uid对应列名，默认为`uid`
        '''
        if game_name:
            return f'{game_name}_uid'
        else:
            return 'uid'

    @classmethod
    @with_session
    async def full_insert_data(cls, session: AsyncSession, **data) -> int:
        '''📝简单介绍:

            数据库基类基础插入数据方法

        🌱参数:

            🔹`**data`
                    插入的数据, 入参列名等于数据即可

        🚀使用范例:

            `await GsUser.full_insert_data(uid='123',cookie='233', ...)`

        ✅返回值:

            🔸`int`: 恒为0
        '''
        session.add(cls(**data))
        await session.commit()
        return 0

    @classmethod
    @with_session
    async def base_select_data(
        cls: Type[T_BaseIDModel], session: AsyncSession, **data
    ) -> Optional[T_BaseIDModel]:
        '''📝简单介绍:

            数据库基类基础选择数据方法

        🌱参数:

            🔹`**data`
                    插入的数据, 入参列名等于数据即可

        🚀使用范例:

            `await GsUser.base_select_data(uid='100740568')`

        ✅返回值:

            🔸`Optional[T_BaseIDModel]`: 选中符合条件的第一个数据，或者为`None`
        '''
        stmt = select(cls)
        for k, v in data.items():
            stmt = stmt.where(getattr(cls, k) == v)
        result = await session.execute(stmt)
        data = result.scalars().all()
        return data[0] if data else None

    @classmethod
    async def data_exist(cls, **data) -> bool:
        '''📝简单介绍:

            数据库基类基础判定数据是否存在的方法


        🚀使用范例:

            `await GsUser.data_exist(uid='100740568')`

        ✅返回值:

            🔸`bool`: 存在为`True`
        '''
        return bool(await cls.base_select_data(**data))


class BaseBotIDModel(BaseIDModel):
    bot_id: str = Field(title='平台')

    @classmethod
    @with_session
    async def update_data_by_uid_without_bot_id(
        cls,
        session: AsyncSession,
        uid: str,
        game_name: Optional[str] = None,
        **data,
    ) -> int:
        '''📝简单介绍:

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
        '''
        sql = update(cls).where(
            getattr(cls, cls.get_gameid_name(game_name)) == uid,
        )
        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session='fetch')
            await session.execute(query)
            await session.commit()
            return 0
        return -1

    @classmethod
    @with_session
    async def update_data_by_uid(
        cls,
        session: AsyncSession,
        uid: str,
        bot_id: str,
        game_name: Optional[str] = None,
        **data,
    ) -> int:
        '''📝简单介绍:

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
        '''
        uid_name = cls.get_gameid_name(game_name)
        if not await cls.data_exist(**{uid_name: uid}):
            data[uid_name] = uid
            return await cls.full_insert_data(bot_id=bot_id, **data)

        sql = update(cls).where(
            getattr(cls, uid_name) == uid,
            cls.bot_id == bot_id,
        )
        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session='fetch')
            await session.execute(query)
            await session.commit()
            return 0
        return -1


class BaseModel(BaseBotIDModel):
    user_id: str = Field(title='账号')

    ################################
    # 基本的增删改查 #
    ################################

    @classmethod
    @with_session
    async def select_data(
        cls: Type[T_BaseModel],
        session: AsyncSession,
        user_id: str,
        bot_id: Optional[str] = None,
    ) -> Optional[T_BaseModel]:
        '''📝简单介绍:

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
        '''
        if bot_id is None:
            sql = select(cls).where(cls.user_id == user_id)
        else:
            sql = select(cls).where(
                cls.user_id == user_id, cls.bot_id == bot_id
            )
        result = await session.execute(sql)
        data = result.scalars().all()
        return data[0] if data else None

    @classmethod
    @with_session
    async def insert_data(
        cls, session: AsyncSession, user_id: str, bot_id: str, **data
    ) -> int:
        '''📝简单介绍:

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
        '''
        if await cls.data_exist(user_id=user_id, bot_id=bot_id):
            await cls.update_data(user_id, bot_id, **data)
        else:
            session.add(cls(user_id=user_id, bot_id=bot_id, **data))
            await session.commit()
        return 0

    @classmethod
    @with_session
    async def delete_data(
        cls, session: AsyncSession, user_id: str, bot_id: str, **data
    ) -> int:
        '''📝简单介绍:

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
        '''
        await session.delete(cls(user_id=user_id, bot_id=bot_id, **data))
        await session.commit()
        return 0

    @classmethod
    @with_session
    async def update_data(
        cls, session: AsyncSession, user_id: str, bot_id: str, **data
    ) -> int:
        '''📝简单介绍:

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
        '''
        sql = update(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session='fetch')
            await session.execute(query)
            await session.commit()
            return 0
        return -1


class Bind(BaseModel):
    group_id: Optional[str] = Field(title='群号')

    ################################
    # 额外的扩展方法 #
    ################################
    @classmethod
    async def get_uid_list_by_game(
        cls,
        user_id: str,
        bot_id: str,
        game_name: Optional[str] = None,
    ) -> Optional[List[str]]:
        '''📝简单介绍:

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
        '''
        result = await cls.select_data(user_id, bot_id)
        if result is None:
            return None

        uid = getattr(result, cls.get_gameid_name(game_name))
        if uid is None:
            return None
        else:
            uid_list = uid.split('_')

        if uid_list:
            return uid_list
        else:
            return None

    @classmethod
    async def get_uid_by_game(
        cls,
        user_id: str,
        bot_id: str,
        game_name: Optional[str] = None,
    ) -> Optional[str]:
        '''📝简单介绍:

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
        '''
        result = await cls.get_uid_list_by_game(user_id, bot_id, game_name)
        if result is None or not result:
            return None
        return result[0]

    @classmethod
    async def bind_exists(
        cls,
        user_id: str,
        bot_id: str,
    ) -> bool:
        '''
        查询当前user_id是否已有绑定数据
        '''
        return bool(await cls.select_data(user_id, bot_id))

    @classmethod
    async def insert_uid(
        cls,
        user_id: str,
        bot_id: str,
        uid: str,
        group_id: Optional[str] = None,
        lenth_limit: Optional[int] = None,
        is_digit: Optional[bool] = True,
        game_name: Optional[str] = None,
    ) -> int:
        '''📝简单介绍:

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
        '''
        result = await cls.get_uid_list_by_game(user_id, bot_id, game_name)

        if lenth_limit:
            if len(uid) != lenth_limit:
                return -1

        if is_digit is not None:
            if not uid.isdigit():
                return -3
        if not uid:
            return -1

        if result is None and not await cls.bind_exists(user_id, bot_id):
            return await cls.insert_data(
                user_id,
                bot_id,
                **{cls.get_gameid_name(game_name): uid, 'group_id': group_id},
            )
        elif result is None:
            new_uid = uid
        elif uid in result:
            return -2
        else:
            result.append(uid)
            new_uid = '_'.join(result)
        await cls.update_data(
            user_id,
            bot_id,
            **{cls.get_gameid_name(game_name): new_uid},
        )
        return 0

    @classmethod
    async def delete_uid(
        cls,
        user_id: str,
        bot_id: str,
        uid: str,
        game_name: Optional[str] = None,
    ) -> int:
        '''📝简单介绍:

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
        '''
        result = await cls.get_uid_list_by_game(user_id, bot_id, game_name)
        if result is None:
            return -1

        if uid not in result:
            return -1

        result.remove(uid)
        new_uid = '_'.join(result)
        await cls.update_data(
            user_id,
            bot_id,
            **{cls.get_gameid_name(game_name): new_uid},
        )
        return 0

    @classmethod
    @with_session
    async def get_all_uid_list_by_game(
        cls,
        session: AsyncSession,
        bot_id: str,
        game_name: Optional[str] = None,
    ) -> List[str]:
        '''📝简单介绍:

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
        '''
        sql = select(cls).where(cls.bot_id == bot_id)
        result = await session.execute(sql)
        data: List["Bind"] = result.scalars().all()
        uid_list: List[str] = []
        for item in data:
            uid = getattr(item, cls.get_gameid_name(game_name))
            if uid is not None and uid:
                game_uid_list: List[str] = uid.split("_")
                uid_list.extend(game_uid_list)
        return uid_list

    @classmethod
    async def switch_uid_by_game(
        cls,
        user_id: str,
        bot_id: str,
        uid: Optional[str] = None,
        game_name: Optional[str] = None,
    ) -> int:
        '''📝简单介绍:

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
        '''
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
            **{cls.get_gameid_name(game_name): '_'.join(uid_list)},
        )
        return 0

    @classmethod
    async def get_bind_group_list(cls, user_id: str, bot_id: str) -> List[str]:
        '''获取传入`user_id`和`bot_id`对应的绑定群列表'''
        data: Optional["Bind"] = await cls.select_data(user_id, bot_id)
        return data.group_id.split("_") if data and data.group_id else []

    @classmethod
    async def get_bind_group(cls, user_id: str, bot_id: str) -> Optional[str]:
        '''获取传入`user_id`和`bot_id`对应的绑定群（如多个则返回第一个）'''
        data = await cls.get_bind_group_list(user_id, bot_id)
        return data[0] if data else None

    @classmethod
    @with_session
    async def get_group_all_uid(cls, session: AsyncSession, group_id: str):
        '''根据传入`group_id`获取该群号下所有绑定`uid`列表'''
        result = await session.scalars(
            select(cls).where(col(cls.group_id).contains(group_id))
        )
        data = result.all()
        return data[0] if data else None


class User(BaseModel):
    cookie: str = Field(default=None, title='Cookie')
    stoken: Optional[str] = Field(default=None, title='Stoken')
    status: Optional[str] = Field(default=None, title='状态')
    push_switch: str = Field(default='off', title='全局推送开关')
    sign_switch: str = Field(default='off', title='自动签到')

    @classmethod
    @with_session
    async def select_data_by_uid(
        cls: Type[T_User],
        session: AsyncSession,
        uid: str,
        game_name: Optional[str] = None,
    ) -> Optional[T_User]:
        '''📝简单介绍:

            基础`User`类的数据选择方法

        🌱参数:

            🔹uid (`str`):
                    传入的用户uid, 一般是该游戏的用户唯一识别id

        🚀使用范例:

            `await GsUser.select_data_by_uid(uid='100740568')`

        ✅返回值:

            🔸`Optional[T_BaseModel]`: 选中符合条件的第一个数据，不存在则为`None`
        '''
        result = await session.execute(
            select(cls).where(
                getattr(cls, cls.get_gameid_name(game_name)) == uid,
            )
        )
        data = result.scalars().all()
        return data[0] if data else None

    @classmethod
    @with_session
    async def get_user_all_data_by_user_id(
        cls: Type[T_User], session: AsyncSession, user_id: str
    ) -> Optional[List[T_User]]:
        '''📝简单介绍:

            基础`User`类的数据选择方法, 获取该`user_id`绑定的全部数据实例

        🌱参数:

            🔹user_id (`str`):
                    传入的用户id, 例如QQ号, 一般直接取`event.user_id`

        🚀使用范例:

            `await GsUser.get_user_all_data_by_user_id(user_id='2333')`

        ✅返回值:

            🔸`Optional[T_BaseModel]`: 选中符合条件的数据列表，不存在则为`None`
        '''
        result = await session.execute(
            select(cls).where(cls.user_id == user_id)
        )
        data = result.scalars().all()
        return data if data else None

    @classmethod
    async def get_user_attr(
        cls,
        user_id: str,
        bot_id: str,
        attr: str,
    ) -> Optional[Any]:
        '''📝简单介绍:

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
        '''
        result = await cls.select_data(user_id, bot_id)
        return getattr(result, attr) if result else None

    @classmethod
    async def get_user_attr_by_uid(
        cls,
        uid: str,
        attr: str,
        game_name: Optional[str] = None,
    ) -> Optional[Any]:
        '''根据传入的`uid`选择数据实例，然后返回数据的`attr`属性的值

        如果没获取到数据则为`None`
        '''
        result = await cls.select_data_by_uid(uid, game_name)
        return getattr(result, attr) if result else None

    @classmethod
    async def get_user_attr_by_user_id(
        cls,
        user_id: str,
        attr: str,
    ) -> Optional[Any]:
        '''根据传入的`user_id`选择数据实例，然后返回数据的`attr`属性的值

        如果没获取到数据则为`None`
        '''
        result = await cls.select_data(user_id)
        return getattr(result, attr) if result else None

    @classmethod
    @with_session
    async def mark_invalid(cls, session: AsyncSession, cookie: str, mark: str):
        '''令一个cookie所对应数据的`status`值为传入的mark

        例如：mark值可以是`error`, 标记该Cookie已失效
        '''
        sql = update(cls).where(cls.cookie == cookie).values(status=mark)
        await session.execute(sql)
        await session.commit()
        return True

    @classmethod
    async def get_user_cookie_by_uid(
        cls, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]:
        '''根据传入的`uid`选择数据实例，然后返回该数据的`cookie`值

        如果没获取到数据则为`None`
        '''
        return await cls.get_user_attr_by_uid(uid, 'cookie', game_name)

    @classmethod
    async def get_user_cookie_by_user_id(
        cls, user_id: str, bot_id: str
    ) -> Optional[str]:
        '''根据传入的`user_id`选择数据实例，然后返回该数据的`cookie`值

        如果没获取到数据则为`None`
        '''
        return await cls.get_user_attr(user_id, bot_id, 'cookie')

    @classmethod
    async def get_user_stoken_by_uid(
        cls, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]:
        '''根据传入的`uid`选择数据实例，然后返回该数据的`stoken`值

        如果没获取到数据则为`None`
        '''
        return await cls.get_user_attr_by_uid(uid, 'stoken', game_name)

    @classmethod
    async def get_user_stoken_by_user_id(
        cls, user_id: str, bot_id: str
    ) -> Optional[str]:
        '''根据传入的`user_id`选择数据实例，然后返回该数据的`stoken`值

        如果没获取到数据则为`None`
        '''
        return await cls.get_user_attr(user_id, bot_id, 'stoken')

    @classmethod
    async def cookie_validate(
        cls, uid: str, game_name: Optional[str] = None
    ) -> bool:
        '''根据传入的`uid`选择数据实例, 校验数据中的`cookie`是否有效

        方法是判断数据中的`status`是否为空值, 如果没有异常标记, 则为`True`
        '''
        data = await cls.get_user_attr_by_uid(uid, 'status', game_name)
        if not data:
            return True
        else:
            return False

    @classmethod
    @with_session
    async def get_switch_open_list(
        cls: Type[T_User], session: AsyncSession, switch_name: str
    ) -> List[T_User]:
        '''📝简单介绍:

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
        '''
        _switch = getattr(cls, switch_name, cls.push_switch)
        sql = select(cls).filter(_switch != 'off')
        data = await session.execute(sql)
        data_list: List[T_User] = data.scalars().all()
        return [user for user in data_list]

    @classmethod
    @with_session
    async def get_all_user(
        cls: Type[T_User], session: AsyncSession, without_error: bool = True
    ) -> List[T_User]:
        '''📝简单介绍:

            基础`User`类的扩展方法, 获取到全部的数据列表

            不一定会返回该表中所有的数据, 返回的数据中`cookie`必定存在值

        🌱参数:

            🔹without_error (`bool`, 默认是 `True`):
                    如果为`True`, 则会排除返回列表中存在`status != None`的数据

        🚀使用范例:

            `await GsUser.get_all_user()`

        ✅返回值:

            🔸`List[T_User]`: 有效数据的列表, 如没有则为`[]`
        '''
        if without_error:
            sql = select(cls).where(
                cls.status == null(), cls.cookie != null(), cls.cookie != ''
            )
        else:
            sql = select(cls).where(cls.cookie != null(), cls.cookie != '')
        result = await session.execute(sql)
        data = result.scalars().all()
        return data

    @classmethod
    async def get_all_cookie(cls) -> List[str]:
        '''获得表数据中全部的`cookie`列表'''
        data = await cls.get_all_user()
        return [_u.cookie for _u in data if _u.cookie]

    @classmethod
    async def get_all_stoken(cls) -> List[str]:
        '''获得表数据中全部的`stoken`列表'''
        data = await cls.get_all_user()
        return [_u.stoken for _u in data if _u.stoken]

    @classmethod
    async def get_all_error_cookie(cls) -> List[str]:
        '''获得表数据中，`status != None`情况下的所有`cookie`列表

        也就是全部失效CK的列表
        '''
        data = await cls.get_all_user()
        return [_u.cookie for _u in data if _u.cookie and _u.status]

    @classmethod
    async def get_all_push_user_list(cls: Type[T_User]) -> List[T_User]:
        '''获得表数据中全部的`push_switch != off`的数据列表'''
        data = await cls.get_all_user()
        return [user for user in data if user.push_switch != 'off']

    @classmethod
    async def user_exists(
        cls, uid: str, game_name: Optional[str] = None
    ) -> bool:
        '''根据传入`uid`，判定数据是否存在'''
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
        '''📝简单介绍:

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
            uid, GsCache, {'region': server}, 'sr' if self.is_sr else None
        )`

        ✅返回值:

            🔸`Optional[str]`: 如找到符合条件的cookie则返回，没有则为`None`
        '''
        # 有绑定自己CK 并且该CK有效的前提下，优先使用自己CK
        if await cls.user_exists(uid, game_name) and await cls.cookie_validate(
            uid, game_name
        ):
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
                sql = (
                    select(cls)
                    .where(getattr(cls, i) == condition[i])
                    .order_by(func.random())
                )
                data = await session.execute(sql)
                user_list: List[T_User] = data.scalars().all()
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
    async def delete_user_data_by_uid(
        cls, session: AsyncSession, uid: str, game_name: Optional[str] = None
    ) -> bool:
        '''根据给定的`uid`获取数据后, 删除整行数据

        如果该数据存在, 删除后返回`True`, 不存在, 则返回`False`
        '''
        if await cls.user_exists(uid, game_name):
            sql = delete(cls).where(
                getattr(cls, cls.get_gameid_name(game_name)) == uid
            )
            await session.execute(sql)
            await session.commit()
            return True
        return False


class Cache(BaseIDModel):
    cookie: str = Field(default=None, title='Cookie')

    @classmethod
    @with_session
    async def select_cache_cookie(
        cls, session: AsyncSession, uid: str, game_name: Optional[str]
    ) -> Optional[str]:
        '''根据给定的`uid`获取表中存在缓存的`cookie`并返回'''
        sql = select(cls).where(
            getattr(cls, cls.get_gameid_name(game_name)) == uid
        )
        result = await session.execute(sql)
        data: List["Cache"] = result.scalars().all()
        return data[0].cookie if len(data) >= 1 else None

    @classmethod
    @with_session
    async def delete_error_cache(
        cls, session: AsyncSession, user: Type["User"]
    ) -> bool:
        '''根据给定的`user`模型中, 查找该模型所有数据的status

        若`status != None`, 则代表该数据cookie有问题

        查找`Cache`表中该cookie对应数据行，并删除

        恒返回`True`
        '''
        data = await user.get_all_error_cookie()
        for cookie in data:
            sql = delete(cls).where(cls.cookie == cookie)
            await session.execute(sql)
        return True

    @classmethod
    @with_session
    async def delete_all_cache(
        cls, session: AsyncSession, user: Type["User"]
    ) -> bool:
        '''删除整个表的数据

        根据给定的`user`模型中, 查找该模型所有数据的status

        若`status == limit30`, 则代表该数据cookie限制可能已回复

        清除`User`表中该类cookie的status, 令其重新为`None`
        '''
        sql = update(user).where(user.status == 'limit30').values(status=None)
        empty_sql = delete(cls)
        await session.execute(sql)
        await session.execute(empty_sql)
        await session.commit()
        return True

    @classmethod
    @with_session
    async def refresh_cache(
        cls, session: AsyncSession, uid: str, game_name: Optional[str] = None
    ) -> bool:
        '''删除指定`uid`的数据行'''
        await session.execute(
            delete(cls).where(
                getattr(cls, cls.get_gameid_name(game_name)) == uid
            )
        )
        return True

    @classmethod
    @with_session
    async def insert_cache_data(
        cls, session: AsyncSession, cookie: str, **data
    ) -> bool:
        '''新增指定`cookie`的数据行, `**data`为数据'''
        new_data = cls(cookie=cookie, **data)
        session.add(new_data)
        await session.commit()
        return True


class Push(BaseBotIDModel):
    @classmethod
    @with_session
    async def select_data_by_uid(
        cls: Type[T_Push],
        session: AsyncSession,
        uid: str,
        game_name: Optional[str] = None,
    ) -> Optional[T_Push]:
        '''📝简单介绍:

            基础`Push`类的数据选择方法

        🌱参数:

            🔹uid (`str`):
                    传入的用户uid, 一般是该游戏的用户唯一识别id

        🚀使用范例:

            `await GsPush.select_data_by_uid(uid='100740568')`

        ✅返回值:

            🔸`Optional[T_BaseModel]`: 选中符合条件的第一个数据，不存在则为`None`
        '''
        result = await session.execute(
            select(cls).where(
                getattr(cls, cls.get_gameid_name(game_name)) == uid,
            )
        )
        data = result.scalars().all()
        return data[0] if data else None
