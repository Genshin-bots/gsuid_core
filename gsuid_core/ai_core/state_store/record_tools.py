"""
通用结构化集合工具 - 在 state_store 之上构建"具名集合 + 记录"语义

设计意图
--------
扁平的 ``state_*`` KV（一个键一个值）足以表达"账户余额=10万"这种单字段状态，
但当代理需要持久化「持仓表」「交易流水」「报名名单」「投票记录」这类**多条结构化
记录**时，KV 就力不从心：

- 用 ``state_append`` 追加 JSON 块：只能整块读、整块写，无法按 id 更新单条；
- 用 ``state_set`` 自维护字典：竞态下后写覆盖前写，主人格也很容易写歪。

``record_*`` 给代理一组**与领域无关**的集合原语：把一个"具名集合"（collection）
当作"字典 of 记录"，每条记录有一个唯一 ``record_id``，payload 是任意 JSON 对象。
所有写操作走 ``state_mutate`` 的乐观锁，并发安全；读操作支持简单的 where 过滤、
排序、分页与基础聚合。

这一层**不引入任何业务术语**（不知道"账户/持仓/流水/股票/积分"），任何插件或
代理都可以在它之上自由构造"虚拟账本""任务清单""签到记录"等持久化结构。

存储模型
--------
每个集合是 ``AIPersistentState`` 里的一行，``state_key`` 形如
``record:<collection_name>``，``value`` 是 ``{record_id: payload_dict}`` 的 JSON。
适用规模 ≤ 数千条记录；超出后单条写入仍是 O(N)，建议代理在业务上分片
（如按日期拆 ``record:trade_log:202605``）。
"""

import json
import uuid
from typing import Any, Dict, List, Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

from .store import state_mutate, state_get_value

_KEY_PREFIX = "record:"


def _collection_key(collection: str) -> str:
    """把集合名标准化为存储键。空集合名直接报错由调用方捕获。"""
    name = (collection or "").strip()
    if not name:
        raise ValueError("collection 名称不能为空")
    return f"{_KEY_PREFIX}{name}"


def _resolve_scope(ctx: RunContext[ToolContext], scope: Optional[str]) -> str:
    """规则同 state_*：传 auto/空时按当前 ev 推断；显式传入按原值用。"""
    if scope and scope.strip().lower() not in ("auto", ""):
        return scope.strip()
    ev = ctx.deps.ev
    if ev is not None:
        if ev.group_id:
            return f"group:{ev.group_id}"
        if ev.user_id:
            return f"user:{ev.user_id}"
    return "global"


def _parse_payload(payload: str) -> Dict[str, Any]:
    """payload 必须是 JSON 对象。非对象直接抛错，避免代理把字符串塞进去。

    特别地：当代理误把"空流水""空持仓"等当成一条 `record_put(payload="[]" / "{}")`
    去预创建集合时，给出对应纠错指引——record_* 集合是按需创建，不需要先建空容器。
    """
    obj = json.loads(payload)
    if isinstance(obj, list):
        raise ValueError(
            "payload 不能是 JSON 数组——一条 record 必须是 JSON 对象（dict）。\n"
            "如果你想表达「建一个空集合」，**不需要预创建**：record 集合是按需建的，"
            "第一次 record_append / record_put 时框架自动初始化。\n"
            "如果你确实要存一条「含 list 字段的记录」，把它包成 dict："
            '`{"items": [...]}` 再传。'
        )
    if not isinstance(obj, dict):
        raise ValueError(f"payload 必须是 JSON 对象（dict）；收到类型: {type(obj).__name__}")
    if not obj:
        raise ValueError(
            "payload 不能是空对象 {}——空 record 没有持久化意义。\n"
            "如果你想「占位」或「标记集合已建」，至少填一个标志字段（如 "
            '`{"_inited_at": "<ISO 时间>"}`）；但更常见的做法是**根本不预创建**——'
            "集合按需创建，首次 record_append / record_put 时自动初始化。"
        )
    return obj


def _matches_where(record: Dict[str, Any], where_field: str, where_value: str) -> bool:
    """简单 where：字段相等比较；为空时认为匹配所有。"""
    if not where_field:
        return True
    if where_field not in record:
        return False
    return str(record[where_field]) == where_value


@ai_tools(category="planning", capability_domain="结构化记录")
async def record_put(
    ctx: RunContext[ToolContext],
    collection: str,
    payload: str,
    record_id: str = "",
    scope: str = "auto",
    ttl_days: Optional[int] = None,
) -> str:
    """
    向一个具名集合写入一条结构化记录（不存在则创建，存在则覆盖整条 payload）。

    适合用来持久化"账户、持仓、交易流水、签到名单、投票记录"等结构化数据。
    集合本身是按需创建的，无需先声明 schema。

    Args:
        collection: 集合名，建议带业务前缀如 "stock:account" / "stock:trade_log"
        payload: 记录内容，必须是 JSON 对象字符串，如 `{"price": 12.3, "qty": 100}`
        record_id: 记录的唯一 ID；留空时自动生成 UUID，原样返回给调用方
        scope: 数据隔离范围。"auto"=按当前会话自动判断；可显式传 "user:xx"/"group:yy"/"global"
        ttl_days: 整个集合的保留天数（不是单条），不填则永久保留

    Returns:
        成功时返回 "ok rid=<record_id>"；失败返回错误描述
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        key = _collection_key(collection)
        rec = _parse_payload(payload)
    except (ValueError, json.JSONDecodeError) as e:
        return f"参数错误: {e}"

    rid = record_id.strip() or uuid.uuid4().hex[:12]

    def _writer(current: Any) -> Dict[str, Any]:
        coll: Dict[str, Any] = current if isinstance(current, dict) else {}
        coll[rid] = rec
        return coll

    try:
        await state_mutate(real_scope, key, _writer, ttl_days=ttl_days)
    except Exception as e:
        logger.exception(f"📒 [RecordStore] record_put 失败: {e}")
        return f"写入失败: {e}"
    return f"ok rid={rid}"


@ai_tools(category="planning", capability_domain="结构化记录")
async def record_get(
    ctx: RunContext[ToolContext],
    collection: str,
    record_id: str,
    scope: str = "auto",
) -> str:
    """
    按 record_id 取回集合里的一条记录。

    Args:
        collection: 集合名
        record_id: 记录 ID
        scope: 数据隔离范围，规则同 record_put

    Returns:
        JSON 序列化后的 payload；找不到时返回提示字符串
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        key = _collection_key(collection)
    except ValueError as e:
        return f"参数错误: {e}"

    coll = await state_get_value(real_scope, key)
    if not isinstance(coll, dict) or record_id not in coll:
        return f"记录不存在: collection={collection} rid={record_id}"
    return json.dumps(coll[record_id], ensure_ascii=False)


@ai_tools(category="planning", capability_domain="结构化记录")
async def record_list(
    ctx: RunContext[ToolContext],
    collection: str,
    where_field: str = "",
    where_value: str = "",
    order_by: str = "",
    limit: int = 50,
    offset: int = 0,
    scope: str = "auto",
) -> str:
    """
    列出集合里的记录，支持简单字段等值过滤、排序、分页。

    Args:
        collection: 集合名
        where_field: 过滤字段名；为空表示不过滤
        where_value: 过滤的字段值（字符串比较）
        order_by: 排序字段名；为空表示按写入顺序；前缀 "-" 表示倒序，如 "-created_at"
        limit: 返回条数上限（≥1）
        offset: 跳过条数
        scope: 数据隔离范围

    Returns:
        JSON 字符串，形如 `[{"_rid": "...", ...payload}, ...]`；空集合返回 "[]"
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        key = _collection_key(collection)
    except ValueError as e:
        return f"参数错误: {e}"

    coll = await state_get_value(real_scope, key)
    if not isinstance(coll, dict) or not coll:
        return "[]"

    items: List[Dict[str, Any]] = []
    for rid, rec in coll.items():
        if not isinstance(rec, dict):
            continue
        if not _matches_where(rec, where_field.strip(), where_value):
            continue
        items.append({"_rid": rid, **rec})

    if order_by:
        field = order_by.strip()
        reverse = False
        if field.startswith("-"):
            reverse = True
            field = field[1:]
        items.sort(key=lambda r: (field not in r, r.get(field)), reverse=reverse)

    if offset > 0:
        items = items[offset:]
    if limit > 0:
        items = items[:limit]
    return json.dumps(items, ensure_ascii=False)


@ai_tools(category="planning", capability_domain="结构化记录")
async def record_append(
    ctx: RunContext[ToolContext],
    collection: str,
    payload: str,
    scope: str = "auto",
    ttl_days: Optional[int] = None,
) -> str:
    """
    向集合追加一条新记录（自动生成 record_id，**绝不覆盖**已有记录）。

    语义上等同 ``record_put(record_id="")``，但显式表达"流水 / 日志"的追加意图——
    适合写每次决策的流水、签到日志、操作历史等只增不改的集合。如果 LLM 想做
    "upsert"（按 id 覆盖），用 ``record_put(record_id=...)``；想做"字段合并"，
    用 ``record_update``。

    Args:
        collection: 集合名（建议带业务前缀，如 "<scope>:trade_log"）
        payload: 记录内容，必须是 JSON 对象字符串
        scope: 数据隔离范围；规则同 ``record_put``
        ttl_days: 整个集合的保留天数

    Returns:
        成功时返回 "ok rid=<auto-generated>"；参数错误时返回错误描述
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        key = _collection_key(collection)
        rec = _parse_payload(payload)
    except (ValueError, json.JSONDecodeError) as e:
        return f"参数错误: {e}"

    # 闭包通过共享 dict 把"最终选定的 rid"带回外部——与本文件 record_delete /
    # record_update 的 flag 写法一致，避免 type: ignore + getattr 兜底（LLM.md §1.4）。
    chosen: Dict[str, str] = {"rid": uuid.uuid4().hex[:12]}

    def _writer(current: Any) -> Dict[str, Any]:
        coll: Dict[str, Any] = current if isinstance(current, dict) else {}
        # uuid 前 12 位撞 ID 概率极低；撞了也不允许覆盖：重摇直到不冲突
        guard = 0
        local_rid = chosen["rid"]
        while local_rid in coll and guard < 8:
            local_rid = uuid.uuid4().hex[:12]
            guard += 1
        coll[local_rid] = rec
        chosen["rid"] = local_rid
        return coll

    try:
        await state_mutate(real_scope, key, _writer, ttl_days=ttl_days)
    except Exception as e:
        logger.exception(f"📒 [RecordStore] record_append 失败: {e}")
        return f"写入失败: {e}"
    return f"ok rid={chosen['rid']}"


@ai_tools(category="planning", capability_domain="结构化记录")
async def record_update(
    ctx: RunContext[ToolContext],
    collection: str,
    record_id: str,
    patch: str,
    scope: str = "auto",
) -> str:
    """
    对集合内某条记录做**字段级合并更新**（保留 patch 未提及的字段）。

    与 ``record_put`` 的区别：
    - ``record_put``：整条 payload 覆盖，patch 没提到的字段会丢。
    - ``record_update``：浅合并；patch 里的字段会覆盖旧字段，其它字段保留。

    适合"持仓数量变了 / 余额变了 / 状态字段切换"这类只改部分字段的场景。
    记录不存在时返回 "not_found"，不会创建新记录——要创建请用 ``record_put``。

    Args:
        collection: 集合名
        record_id: 要更新的记录 ID
        patch: 要合并进 payload 的字段，JSON 对象字符串
        scope: 数据隔离范围

    Returns:
        "updated" / "not_found" / 参数错误描述
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        key = _collection_key(collection)
        patch_rec = _parse_payload(patch)
    except (ValueError, json.JSONDecodeError) as e:
        return f"参数错误: {e}"

    if not record_id.strip():
        return "参数错误: record_id 不能为空"
    rid = record_id.strip()

    flag = {"hit": False}

    def _writer(current: Any) -> Dict[str, Any]:
        coll: Dict[str, Any] = current if isinstance(current, dict) else {}
        if rid not in coll or not isinstance(coll[rid], dict):
            return coll
        merged: Dict[str, Any] = dict(coll[rid])
        merged.update(patch_rec)
        coll[rid] = merged
        flag["hit"] = True
        return coll

    try:
        await state_mutate(real_scope, key, _writer)
    except Exception as e:
        logger.exception(f"📒 [RecordStore] record_update 失败: {e}")
        return f"更新失败: {e}"
    return "updated" if flag["hit"] else "not_found"


@ai_tools(category="planning", capability_domain="结构化记录")
async def record_delete(
    ctx: RunContext[ToolContext],
    collection: str,
    record_id: str,
    scope: str = "auto",
) -> str:
    """
    删除集合里的一条记录。

    Args:
        collection: 集合名
        record_id: 要删除的记录 ID
        scope: 数据隔离范围

    Returns:
        "deleted" / "not_found" / 错误描述
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        key = _collection_key(collection)
    except ValueError as e:
        return f"参数错误: {e}"

    deleted_flag = {"hit": False}

    def _deleter(current: Any) -> Dict[str, Any]:
        coll: Dict[str, Any] = current if isinstance(current, dict) else {}
        if record_id in coll:
            coll.pop(record_id, None)
            deleted_flag["hit"] = True
        return coll

    try:
        await state_mutate(real_scope, key, _deleter)
    except Exception as e:
        logger.exception(f"📒 [RecordStore] record_delete 失败: {e}")
        return f"删除失败: {e}"
    return "deleted" if deleted_flag["hit"] else "not_found"


@ai_tools(category="planning", capability_domain="结构化记录")
async def record_summary(
    ctx: RunContext[ToolContext],
    collection: str,
    sum_field: str = "",
    scope: str = "auto",
) -> str:
    """
    汇总集合：返回总条数、首/末两条 record_id，并可选对一个数值字段求和与求均值。

    用来做最终结算（如"30 天后算累计盈亏"），避免主人格自己把整个集合拉回再算。

    Args:
        collection: 集合名
        sum_field: 可选，要求和/求均值的数值字段名；为空只返回 count / first / last
        scope: 数据隔离范围

    Returns:
        JSON 字符串，含 count / first_rid / last_rid / sum / avg（求和有意义时）
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        key = _collection_key(collection)
    except ValueError as e:
        return f"参数错误: {e}"

    coll = await state_get_value(real_scope, key)
    if not isinstance(coll, dict) or not coll:
        return json.dumps({"count": 0}, ensure_ascii=False)

    rids = list(coll.keys())
    summary: Dict[str, Any] = {
        "count": len(rids),
        "first_rid": rids[0],
        "last_rid": rids[-1],
    }
    field = sum_field.strip()
    if field:
        total = 0.0
        hit = 0
        for rec in coll.values():
            if not isinstance(rec, dict):
                continue
            val = rec.get(field)
            if isinstance(val, (int, float)):
                total += float(val)
                hit += 1
        summary["sum"] = total
        summary["hit"] = hit
        summary["avg"] = (total / hit) if hit else None
    return json.dumps(summary, ensure_ascii=False)
