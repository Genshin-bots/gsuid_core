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
from sqlalchemy.orm import sessionmaker
from sqlmodel import Field, SQLModel, col
from sqlalchemy.sql.expression import func
from sqlalchemy import and_, delete, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from gsuid_core.data_store import get_res_path

T_BaseModel = TypeVar('T_BaseModel', bound='BaseModel')
T_BaseIDModel = TypeVar('T_BaseIDModel', bound='BaseIDModel')
T_User = TypeVar('T_User', bound='User')
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

    return wrapper


class BaseIDModel(SQLModel):
    id: Optional[int] = Field(default=None, primary_key=True, title='序号')

    @classmethod
    def get_gameid_name(cls, game_name: Optional[str] = None):
        if game_name:
            return f'{game_name}_uid'
        else:
            return 'uid'

    @classmethod
    @with_session
    async def full_insert_data(
        cls, session: AsyncSession, model: Type["BaseIDModel"], **data
    ) -> int:
        session.add(model(**data))
        await session.commit()
        return 0

    @classmethod
    @with_session
    async def base_select_data(
        cls, session: AsyncSession, model: Type[T_BaseIDModel], **data
    ) -> Optional[T_BaseIDModel]:
        conditions = []
        for key, value in data.items():
            conditions.append(getattr(model, key) == value)
        where_clause = and_(*conditions)
        sql = select(model).where(where_clause)
        result = await session.execute(sql)
        data = result.scalars().all()
        return data[0] if data else None

    @classmethod
    async def data_exist(cls, model: Type[T_BaseIDModel], **data) -> bool:
        return bool(await cls.base_select_data(model, **data))


class BaseBotIDModel(BaseIDModel):
    bot_id: str = Field(title='平台')

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
        sql = update(cls).where(
            getattr(cls, cls.get_gameid_name(game_name)) == uid,
            cls.bot_id == bot_id,
        )
        if data is not None:
            query = sql.values(**data)
            query.execution_options(synchronize_session='fetch')
            await session.execute(query)
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
        session.add(cls(user_id=user_id, bot_id=bot_id, **data))
        await session.commit()
        return 0

    @classmethod
    @with_session
    async def delete_data(
        cls, session: AsyncSession, user_id: str, bot_id: str, **data
    ) -> int:
        await session.delete(cls(user_id=user_id, bot_id=bot_id, **data))
        await session.commit()
        return 0

    @classmethod
    @with_session
    async def update_data(
        cls, session: AsyncSession, user_id: str, bot_id: str, **data
    ) -> int:
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
        '''
        为数据库增加绑定UID

        如果有传`lenth_limit`, 当uid位数不等于的时候, 返回`-1`

        如果该UID已绑定, 则返回`-2`

        `is_digit`默认为`True`, 进行合法性校验, 如果不是全数字, 返回`-3`

        成功绑定, 则返回`0`
        '''
        result = await cls.get_uid_list_by_game(user_id, bot_id, game_name)

        if lenth_limit:
            if len(uid) != lenth_limit:
                return -1

        if is_digit is not None:
            if not uid.isdigit():
                return -3

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
    ):
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
        '''
        切换用户UID, 成功返回0

        可传确定的UID

        如果不传UID,则自动切换序列下一个UID

        如果不存在绑定记录,则返回-1

        如果传了UID但是不存在绑定列表,则返回-2

        如果绑定UID列表不足2个,返回-3
        '''
        uid_list = await cls.get_uid_list_by_game(user_id, bot_id, game_name)
        if not uid_list:
            return -1
        elif len(uid_list) <= 1:
            return -3
        elif uid is None:
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
        data: Optional["Bind"] = await cls.select_data(user_id, bot_id)
        return data.group_id.split("_") if data and data.group_id else []

    @classmethod
    async def get_bind_group(cls, user_id: str, bot_id: str) -> Optional[str]:
        data = await cls.get_bind_group_list(user_id, bot_id)
        return data[0] if data else None

    @classmethod
    @with_session
    async def get_group_all_uid(cls, session: AsyncSession, group_id: str):
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
        result = await cls.select_data(user_id, bot_id)
        return getattr(result, attr) if result else None

    @classmethod
    async def get_user_attr_by_uid(
        cls,
        uid: str,
        attr: str,
        game_name: Optional[str] = None,
    ) -> Optional[Any]:
        result = await cls.select_data_by_uid(uid, game_name)
        return getattr(result, attr) if result else None

    @classmethod
    async def get_user_attr_by_user_id(
        cls,
        user_id: str,
        attr: str,
    ) -> Optional[Any]:
        result = await cls.select_data(user_id)
        return getattr(result, attr) if result else None

    @classmethod
    @with_session
    async def mark_invalid(cls, session: AsyncSession, cookie: str, mark: str):
        sql = update(cls).where(cls.cookie == cookie).values(status=mark)
        await session.execute(sql)
        await session.commit()
        return True

    @classmethod
    async def get_user_cookie_by_uid(
        cls, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]:
        return await cls.get_user_attr_by_uid(uid, 'cookie', game_name)

    @classmethod
    async def get_user_cookie_by_user_id(
        cls, user_id: str, bot_id: str
    ) -> Optional[str]:
        return await cls.get_user_attr(user_id, bot_id, 'cookie')

    @classmethod
    async def get_user_stoken_by_uid(
        cls, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]:
        return await cls.get_user_attr_by_uid(uid, 'stoken', game_name)

    @classmethod
    async def get_user_stoken_by_user_id(
        cls, user_id: str, bot_id: str
    ) -> Optional[str]:
        return await cls.get_user_attr(user_id, bot_id, 'stoken')

    @classmethod
    async def cookie_validate(
        cls, uid: str, game_name: Optional[str] = None
    ) -> bool:
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
        _switch = getattr(cls, switch_name, cls.push_switch)
        sql = select(cls).filter(_switch != 'off')
        data = await session.execute(sql)
        data_list: List[T_User] = data.scalars().all()
        return [user for user in data_list]

    @classmethod
    @with_session
    async def get_all_user(
        cls: Type[T_User], session: AsyncSession
    ) -> List[T_User]:
        sql = select(cls).where(cls.cookie is not None, cls.cookie != '')
        result = await session.execute(sql)
        data: List[T_User] = result.scalars().all()
        return data

    @classmethod
    async def get_all_cookie(cls) -> List[str]:
        data = await cls.get_all_user()
        return [_u.cookie for _u in data if _u.cookie]

    @classmethod
    async def get_all_stoken(cls) -> List[str]:
        data = await cls.get_all_user()
        return [_u.stoken for _u in data if _u.stoken]

    @classmethod
    async def get_all_error_cookie(cls) -> List[str]:
        data = await cls.get_all_user()
        return [_u.cookie for _u in data if _u.cookie and _u.status]

    @classmethod
    async def get_all_push_user_list(cls: Type[T_User]) -> List[T_User]:
        data = await cls.get_all_user()
        return [user for user in data if user.push_switch != 'off']

    @classmethod
    async def user_exists(
        cls, uid: str, game_name: Optional[str] = None
    ) -> bool:
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
    ):
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
        new_data = cls(cookie=cookie, **data)
        session.add(new_data)
        await session.commit()
        return True


class Push(BaseBotIDModel):
    pass
