"""
通用持久状态存储 - 核心读写逻辑

对外提供 set / get / delete / list / append 五个原子操作，
所有操作均以 (scope, state_key) 定位，并自动处理 TTL 过期。
"""

import json
from typing import Any, List, Callable, Optional
from datetime import datetime, timedelta

from sqlmodel import col, and_, delete, select, update
from sqlalchemy.exc import IntegrityError

from gsuid_core.logger import logger

from .models import AIPersistentState

# state_append_item 乐观锁的最大重试次数
_APPEND_MAX_RETRY = 5

# 表是否已确保创建（进程级，首次操作时建表，与全局 create_all 的启动时序解耦）
_table_ensured = False


def _now() -> datetime:
    return datetime.now()


async def _ensure_table() -> None:
    """确保 AIPersistentState 表已创建。

    框架的 SQLModel.metadata.create_all 在 webconsole 后台初始化阶段执行，
    其时序与 AI 重依赖（buildin_tools → state_store）的导入是并发的，
    无法保证 create_all 运行时本表的模型已被注册。此处在首次读写前做一次
    针对性建表，使 state_store 不依赖启动时序即可可靠工作。
    """
    global _table_ensured
    if _table_ensured:
        return
    try:
        from gsuid_core.utils.database.base_models import engine

        async with engine.begin() as conn:
            await conn.run_sync(
                AIPersistentState.metadata.create_all,
                tables=[AIPersistentState.__table__],
                checkfirst=True,
            )
        _table_ensured = True
    except Exception as e:
        logger.warning(f"🗄️ [StateStore] 建表检查失败（将沿用既有表）: {e}")
        _table_ensured = True


async def _fetch(scope: str, state_key: str) -> Optional[AIPersistentState]:
    """读取一条状态记录，若已过期则删除并返回 None"""
    from gsuid_core.utils.database.base_models import async_maker

    await _ensure_table()

    async with async_maker() as session:
        stmt = select(AIPersistentState).where(
            and_(
                AIPersistentState.scope == scope,
                AIPersistentState.state_key == state_key,
            )
        )
        result = await session.execute(stmt)
        record = result.scalars().first()

        if record is None:
            return None

        # TTL 过期检查
        if record.expire_at is not None and record.expire_at < _now():
            await session.execute(delete(AIPersistentState).where(col(AIPersistentState.id) == record.id))
            await session.commit()
            logger.debug(f"🗄️ [StateStore] 状态已过期并清理: {scope} / {state_key}")
            return None

        return record


async def state_set_value(
    scope: str,
    state_key: str,
    value: Any,
    ttl_days: Optional[int] = None,
) -> int:
    """写入一个持久化的键值数据（存在则覆盖）。

    Returns:
        写入后的版本号
    """
    from gsuid_core.utils.database.base_models import async_maker

    await _ensure_table()

    value_json = json.dumps(value, ensure_ascii=False)
    expire_at = _now() + timedelta(days=ttl_days) if ttl_days else None

    # set 语义为"存在则覆盖"，并发写入按 last-write-wins 处理；
    # 唯一约束下的并发首次插入会触发 IntegrityError，回退为 UPDATE 重试。
    for _ in range(_APPEND_MAX_RETRY):
        async with async_maker() as session:
            stmt = select(AIPersistentState).where(
                and_(
                    AIPersistentState.scope == scope,
                    AIPersistentState.state_key == state_key,
                )
            )
            result = await session.execute(stmt)
            record = result.scalars().first()

            if record is None:
                session.add(
                    AIPersistentState(
                        scope=scope,
                        state_key=state_key,
                        value=value_json,
                        version=1,
                        expire_at=expire_at,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue  # 已被并发插入，重试走 UPDATE 分支
                new_version = 1
            else:
                record.value = value_json
                record.version += 1
                record.updated_at = _now()
                record.expire_at = expire_at
                session.add(record)
                await session.commit()
                new_version = record.version

        logger.debug(f"🗄️ [StateStore] 写入: {scope} / {state_key} (v{new_version})")
        return new_version

    raise RuntimeError(f"state_set_value 并发写入重试耗尽: {scope} / {state_key}")


async def state_get_value(scope: str, state_key: str) -> Optional[Any]:
    """读取一个持久化的键值数据，不存在或已过期返回 None。"""
    record = await _fetch(scope, state_key)
    if record is None:
        return None
    try:
        return json.loads(record.value)
    except (json.JSONDecodeError, TypeError):
        return record.value


async def state_delete_value(scope: str, state_key: str) -> bool:
    """删除一个键，返回是否确实删除了记录。"""
    from gsuid_core.utils.database.base_models import async_maker

    await _ensure_table()

    async with async_maker() as session:
        stmt = select(AIPersistentState).where(
            and_(
                AIPersistentState.scope == scope,
                AIPersistentState.state_key == state_key,
            )
        )
        result = await session.execute(stmt)
        record = result.scalars().first()
        if record is None:
            return False
        await session.execute(delete(AIPersistentState).where(col(AIPersistentState.id) == record.id))
        await session.commit()

    logger.debug(f"🗄️ [StateStore] 删除: {scope} / {state_key}")
    return True


async def state_list_keys(scope: str, prefix: str = "") -> List[str]:
    """列出某个 scope 下的所有键（可按前缀过滤），自动跳过已过期的键。"""
    from gsuid_core.utils.database.base_models import async_maker

    await _ensure_table()

    async with async_maker() as session:
        stmt = select(AIPersistentState).where(AIPersistentState.scope == scope)
        result = await session.execute(stmt)
        records = result.scalars().all()

    now = _now()
    keys: List[str] = []
    for record in records:
        if record.expire_at is not None and record.expire_at < now:
            continue
        if prefix and not record.state_key.startswith(prefix):
            continue
        keys.append(record.state_key)
    return keys


async def state_mutate(
    scope: str,
    state_key: str,
    mutator: Callable[[Any], Any],
    ttl_days: Optional[int] = None,
) -> Any:
    """以乐观锁安全地"读-改-写"一个状态值。

    流程为"读 version → mutator(当前值) 算出新值 → 条件更新（WHERE version=旧值）"，
    并发冲突时基于最新值重试。适用于 `{tag: count}` 这类需要累加、绝不能丢更新
    的场景——简单的 state_get → 改 → state_set 三步在并发下会"后写覆盖前写"。

    Args:
        mutator: 接收当前值（不存在或已过期时为 None），返回要写入的新值。
                 因冲突会重试，mutator 必须是无副作用的纯函数。

    Returns:
        最终成功写入的新值。
    """
    from gsuid_core.utils.database.base_models import async_maker

    await _ensure_table()

    expire_at = _now() + timedelta(days=ttl_days) if ttl_days else None

    for attempt in range(_APPEND_MAX_RETRY):
        async with async_maker() as session:
            stmt = select(AIPersistentState).where(
                and_(
                    AIPersistentState.scope == scope,
                    AIPersistentState.state_key == state_key,
                )
            )
            record = (await session.execute(stmt)).scalars().first()

            # 已过期的记录视同不存在（但记录行仍在，需走 UPDATE 分支覆盖）
            expired = record is not None and record.expire_at is not None and record.expire_at < _now()
            if record is None or expired:
                current: Any = None
            else:
                try:
                    current = json.loads(record.value)
                except (json.JSONDecodeError, TypeError):
                    current = record.value

            new_value = mutator(current)
            value_json = json.dumps(new_value, ensure_ascii=False)

            if record is None:
                # 首次创建：靠 (scope, state_key) 唯一约束兜底并发插入
                session.add(
                    AIPersistentState(
                        scope=scope,
                        state_key=state_key,
                        value=value_json,
                        version=1,
                        expire_at=expire_at,
                    )
                )
                try:
                    await session.commit()
                    return new_value
                except IntegrityError:
                    await session.rollback()  # 已被并发插入，重试走 UPDATE 分支
            else:
                # 条件更新：仅当 version 仍是读取时的值才写入成功
                old_version = record.version
                upd = (
                    update(AIPersistentState)
                    .where(
                        and_(
                            col(AIPersistentState.id) == record.id,
                            col(AIPersistentState.version) == old_version,
                        )
                    )
                    .values(
                        value=value_json,
                        version=old_version + 1,
                        updated_at=_now(),
                        expire_at=expire_at,
                    )
                )
                result = await session.execute(upd)
                await session.commit()
                if result.rowcount == 1:
                    return new_value
                # version 已被其他并发写入推进，重试

        logger.debug(
            f"🗄️ [StateStore] state_mutate 乐观锁冲突，重试 ({attempt + 1}/{_APPEND_MAX_RETRY}): {scope} / {state_key}"
        )

    raise RuntimeError(f"state_mutate 乐观锁重试 {_APPEND_MAX_RETRY} 次仍失败: {scope} / {state_key}")


async def state_append_item(
    scope: str,
    state_key: str,
    item: Any,
    max_length: Optional[int] = None,
    ttl_days: Optional[int] = None,
) -> int:
    """向一个列表型的值追加元素（值不存在时自动创建为列表）。

    基于 state_mutate 的乐观锁实现：并发追加时只有先提交者成功，后者基于
    最新列表重新追加，避免简单读-改-写下的"后写覆盖前写"静默丢数据。
    支持 max_length 自动裁剪。

    Returns:
        追加后的列表长度
    """

    def _appender(current: Any) -> List[Any]:
        if current is None:
            new_list: List[Any] = []
        elif isinstance(current, list):
            new_list = list(current)
        else:
            new_list = [current]
        new_list.append(item)
        if max_length is not None and max_length > 0 and len(new_list) > max_length:
            new_list = new_list[-max_length:]
        return new_list

    new_list = await state_mutate(scope, state_key, _appender, ttl_days=ttl_days)
    return len(new_list)
