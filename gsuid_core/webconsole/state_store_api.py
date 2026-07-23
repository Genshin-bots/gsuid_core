"""持久化状态浏览器 WebAPI（``/api/ai/state-store/*``）。

让前端 / 主人能直接查看代理人格通过 `state_*` 和 `record_*` 工具写到
`AIPersistentState` 表里的所有持久化业务数据——账户、持仓、流水、签到名单、
积分日志、学习进度等等。

设计原则：
- **只读 + 删除**：本 API 不提供写入端点；写入由代理人格 / 插件通过工具完成。
  Webconsole 给主人一个"看 + 删（兜底清理）"的能力，避免人工误改导致代理逻辑
  与 UI 出现状态分裂。
- **scope 分组**：所有 keys 按 ``scope`` 归类（``user:xxx`` / ``group:yyy`` /
  ``global``）。同一个代理可能为不同用户 / 不同群分别维护一份状态，前端 UI
  应该按 scope 分页或下拉切换。
- **record_* 集合特殊化展开**：``state_key`` 以 ``record:`` 开头的行（即
  ``record_*`` 工具维护的"具名集合"）有专门端点把内部 ``{record_id: payload}``
  字典分页拍平展示，避免前端需要再次解析 JSON。

端点：
- ``GET /api/ai/state-store/scopes`` ：列出所有 scope（按 key 数排序）。
- ``GET /api/ai/state-store/keys`` ：列出某 scope 下的 key 列表（含元信息）。
- ``GET /api/ai/state-store/get`` ：取单个 (scope, key) 的完整 value。
- ``GET /api/ai/state-store/records`` ：把一个 record_* 集合分页拍平展示。
- ``DELETE /api/ai/state-store/entry`` ：删除单个 (scope, key)——兜底清理用。
- ``POST /api/ai/state-store/entries/batch-delete`` ：批量删除多条
  (scope, key)——支持 entries 列表 / 同 scope 简写两种填法，前端"勾选多行
  一键删除"走它。

权限：所有端点均挂 ``require_auth``，对齐其它 webconsole API 的鉴权基线。
"""

import json
from typing import Any, Dict, List, Tuple, Optional

from fastapi import Query, Depends
from pydantic import Field, BaseModel
from sqlmodel import col, func, select
from sqlalchemy import delete as sa_delete, tuple_ as sa_tuple

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.state_store.store import (
    state_get_value,
    state_list_keys,
    state_delete_value,
)
from gsuid_core.ai_core.state_store.models import AIPersistentState
from gsuid_core.utils.database.base_models import async_maker

from ._api_tags import STATE_STORE

# 批量删除单次请求上限：防止前端误传一个无界列表把整表打空
_BATCH_DELETE_MAX = 500

_RECORD_KEY_PREFIX = "record:"


def _entry_dict(row: AIPersistentState, include_value: bool = False) -> Dict[str, Any]:
    """把一行 AIPersistentState 转为前端可读字典。

    默认不展开 ``value``（可能是大 JSON 块），只给元信息；显式 include_value=True
    时附完整 value 字段（已 json.loads 解析）。
    """
    out: Dict[str, Any] = {
        "scope": row.scope,
        "state_key": row.state_key,
        "version": row.version,
        "size_bytes": len(row.value.encode("utf-8")) if row.value else 0,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "expire_at": row.expire_at.isoformat() if row.expire_at else None,
        # 给前端一个粗略的类型提示：通过解析 value 一层判定 dict/list/scalar
        "value_type": _infer_value_type(row.value),
        # record_* 集合标识：state_key 以 record: 开头时前端展示"集合查看"按钮
        "is_record_collection": row.state_key.startswith(_RECORD_KEY_PREFIX),
        "record_collection_name": (
            row.state_key[len(_RECORD_KEY_PREFIX) :] if row.state_key.startswith(_RECORD_KEY_PREFIX) else None
        ),
    }
    if include_value:
        out["value"] = _safe_json_decode(row.value)
    return out


def _infer_value_type(raw: Optional[str]) -> str:
    """粗判 value 的 JSON 类型，仅给前端 UI 决定渲染方式（dict/list/scalar/null）。"""
    if not raw:
        return "null"
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "string"
    if v is None:
        return "null"
    if isinstance(v, dict):
        return "dict"
    if isinstance(v, list):
        return "list"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return "string"


def _safe_json_decode(raw: Optional[str]) -> Any:
    """解析 JSON 字符串失败时退回原值——避免端点因脏数据 500。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


@app.get("/api/ai/state-store/scopes", summary="列出所有 scope", tags=STATE_STORE)
async def list_state_scopes(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """列出所有出现过的 scope，按 key 数倒序排序。

    Returns:
        ``{"status": 0, "data": {"scopes": [{"scope": str, "key_count": int}, ...]}}``
    """
    async with async_maker() as session:
        stmt = select(
            AIPersistentState.scope,
            func.count(col(AIPersistentState.id)).label("key_count"),
        ).group_by(AIPersistentState.scope)
        result = await session.execute(stmt)
        rows = result.all()
    scopes = [{"scope": r[0], "key_count": int(r[1] or 0)} for r in rows]
    scopes.sort(key=lambda x: (-x["key_count"], x["scope"]))
    return {"status": 0, "msg": "ok", "data": {"scopes": scopes, "count": len(scopes)}}


@app.get("/api/ai/state-store/keys", summary="列出某 scope 下的 keys", tags=STATE_STORE)
async def list_state_keys(
    _: Dict[str, Any] = Depends(require_auth),
    scope: str = Query(..., description="scope 字符串，如 user:user_web_01 / group:1779024006344 / global"),
    prefix: str = Query("", description="可选 state_key 前缀过滤（如 'record:' 仅列结构化集合）"),
    include_expired: bool = Query(False, description="是否包含已过期 key（默认排除）"),
) -> Dict[str, Any]:
    """列出某 scope 下的全部 key（含元信息，但不展开 value）。

    Returns:
        ``{"items": [{"scope", "state_key", "version", "value_type", ...}], "count": N}``
    """
    from datetime import datetime as _dt

    async with async_maker() as session:
        stmt = select(AIPersistentState).where(col(AIPersistentState.scope) == scope)
        if prefix:
            stmt = stmt.where(col(AIPersistentState.state_key).startswith(prefix))
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    now = _dt.now()
    items: List[Dict[str, Any]] = []
    for row in rows:
        if not include_expired and row.expire_at is not None and row.expire_at < now:
            continue
        items.append(_entry_dict(row, include_value=False))
    items.sort(key=lambda x: x["state_key"])
    return {"status": 0, "msg": "ok", "data": {"items": items, "count": len(items)}}


@app.get("/api/ai/state-store/get", summary="取单条 (scope, state_key) 的完整 value", tags=STATE_STORE)
async def get_state_entry(
    _: Dict[str, Any] = Depends(require_auth),
    scope: str = Query(..., description="scope 字符串"),
    state_key: str = Query(..., description="state_key 全名（含 record:/state_/_ 等前缀）"),
) -> Dict[str, Any]:
    """读取单个 (scope, state_key) 的完整 value（已 JSON 解析）。

    若 key 不存在 / 已过期返回 ``status=1``。
    """
    async with async_maker() as session:
        stmt = select(AIPersistentState).where(
            col(AIPersistentState.scope) == scope,
            col(AIPersistentState.state_key) == state_key,
        )
        result = await session.execute(stmt)
        row = result.scalars().first()
    if row is None:
        return {"status": 1, "msg": f"key 不存在: {scope}/{state_key}", "data": None}
    return {"status": 0, "msg": "ok", "data": _entry_dict(row, include_value=True)}


@app.get("/api/ai/state-store/records", summary="`record_*` 集合分页展开", tags=STATE_STORE)
async def list_record_collection(
    _: Dict[str, Any] = Depends(require_auth),
    scope: str = Query(..., description="scope 字符串"),
    collection: str = Query(..., description="record 集合名（不含 'record:' 前缀，如 'myplugin:items'）"),
    limit: int = Query(50, ge=1, le=500, description="返回记录数上限"),
    offset: int = Query(0, ge=0, description="偏移量（用于分页）"),
    where_field: str = Query("", description="可选字段名过滤"),
    where_value: str = Query("", description="可选字段值过滤（与 where_field 配合）"),
) -> Dict[str, Any]:
    """把一个 ``record_*`` 集合分页拍平展示。

    内部数据形态：``record:<collection>`` 对应的 value 是 ``{record_id: payload_dict}``
    的 JSON 字典。本端点把字典展开成 ``[{_rid: ..., **payload}, ...]`` 列表给前端，
    类似 ``record_list`` LLM 工具的返回格式，方便前端直接用 table 展示。

    Returns:
        ``{"records": [...], "total": int, "limit": int, "offset": int}``
    """
    full_key = f"{_RECORD_KEY_PREFIX}{collection.strip()}"
    raw = await state_get_value(scope, full_key)
    if raw is None or not isinstance(raw, dict):
        # 集合不存在或不是 dict 结构（被代理直接 state_set 覆盖过的脏数据）
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "records": [],
                "total": 0,
                "limit": limit,
                "offset": offset,
                "collection": collection,
                "scope": scope,
                "warning": ("集合不存在或被非 record_* 写法覆盖（不是 dict 结构）。" if raw is not None else None),
            },
        }

    items: List[Dict[str, Any]] = []
    for rid, payload in raw.items():
        if not isinstance(payload, dict):
            # 历史脏数据兜底：payload 非 dict 时退化为 {"_value": ...}
            items.append({"_rid": rid, "_value": payload})
            continue
        # where 过滤
        if where_field and (where_field not in payload or str(payload[where_field]) != where_value):
            continue
        item = {"_rid": rid}
        item.update(payload)
        items.append(item)

    total = len(items)
    sliced = items[offset : offset + limit]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "records": sliced,
            "total": total,
            "limit": limit,
            "offset": offset,
            "collection": collection,
            "scope": scope,
        },
    }


@app.delete("/api/ai/state-store/entry", summary="删除单条 (scope, state_key)（兜底清理用）", tags=STATE_STORE)
async def delete_state_entry(
    _: Dict[str, Any] = Depends(require_auth),
    scope: str = Query(..., description="scope 字符串"),
    state_key: str = Query(..., description="state_key 全名"),
) -> Dict[str, Any]:
    """删除单个 (scope, state_key)。用于主人在 UI 上手动兜底清理。

    **请谨慎使用**：代理人格依赖某些 key 的存在做业务推进（如虚拟账户初始化标志）；
    删除后可能导致代理重新初始化或报错。建议先在 UI 上看 value 再决定是否删除。
    """
    deleted = await state_delete_value(scope, state_key)
    if not deleted:
        return {"status": 1, "msg": f"key 不存在: {scope}/{state_key}", "data": None}
    logger.info(t("🗄️ [StateStore-API] 删除 entry: {scope}/{state_key}", scope=scope, state_key=state_key))
    return {"status": 0, "msg": "ok", "data": {"scope": scope, "state_key": state_key}}


class BatchDeleteEntry(BaseModel):
    """跨 scope 批量删除时的单条目标——前端 UI 勾选多行时一条对应一个。"""

    scope: str
    state_key: str


class BatchDeleteRequest(BaseModel):
    """批量删除请求体——支持两种互斥的填法。

    模式 A · 跨 scope 列表（``entries``）：UI 在多 scope 表格里勾选多行时使用。
    模式 B · 同 scope 简写（``scope`` + ``state_keys``）：常见的"在某 scope 详情页
    勾选多个 key 删除"场景，比 entries 更省字节。

    两种模式可以混填——最终会合并去重后统一删除。至少需要一种产生非空目标列表。
    """

    entries: List[BatchDeleteEntry] = Field(default_factory=list)
    scope: Optional[str] = None
    state_keys: List[str] = Field(default_factory=list)


@app.post("/api/ai/state-store/entries/batch-delete", summary="批量删除条目", tags=STATE_STORE)
async def batch_delete_state_entries(
    body: BatchDeleteRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """批量删除多条 (scope, state_key)——用于前端"勾选多行 → 一键删除"。

    返回逐条结果，让前端能在表格里把"成功删除"和"key 不存在"分别标识。

    **请谨慎使用**：与单删一样有破坏代理状态的风险，且批量动作影响面更大。
    建议前端在调用前弹窗列出待删 key 让主人复核。

    Body：
        ``{"entries": [{"scope": "...", "state_key": "..."}, ...]}``——跨 scope；
        ``{"scope": "...", "state_keys": ["...", "..."]}``——同 scope 简写；
        两种可混填，最终合并去重。

    单次上限见模块常量 ``_BATCH_DELETE_MAX``（500 条）；超出返回 ``status=2``
    （避免误操作打空整表）。
    """
    # 合并两种填法 → 去重后的 (scope, state_key) 列表
    targets: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for e in body.entries:
        scope_ = e.scope
        sk = e.state_key
        if not scope_ or not sk:
            continue
        pair = (scope_, sk)
        if pair in seen:
            continue
        seen.add(pair)
        targets.append(pair)
    if body.scope and body.state_keys:
        for sk in body.state_keys:
            if not sk:
                continue
            pair = (body.scope, sk)
            if pair in seen:
                continue
            seen.add(pair)
            targets.append(pair)

    if not targets:
        return {
            "status": 1,
            "msg": "目标列表为空：请填 entries 或 (scope + state_keys)",
            "data": None,
        }
    if len(targets) > _BATCH_DELETE_MAX:
        return {
            "status": 2,
            "msg": f"单次批量删除上限 {_BATCH_DELETE_MAX} 条，本次传入 {len(targets)}",
            "data": None,
        }

    # 单条 SQL 查出实际存在的行，未命中的留给逐条结果标 not_found
    async with async_maker() as session:
        stmt = select(AIPersistentState.scope, AIPersistentState.state_key).where(
            sa_tuple(col(AIPersistentState.scope), col(AIPersistentState.state_key)).in_(targets)
        )
        existing_rows = (await session.execute(stmt)).all()
        existing: set[Tuple[str, str]] = {(r[0], r[1]) for r in existing_rows}

        if existing:
            # 单条 SQL 一次性删除全部命中行——避免逐条往返
            del_stmt = sa_delete(AIPersistentState).where(
                sa_tuple(col(AIPersistentState.scope), col(AIPersistentState.state_key)).in_(list(existing))
            )
            await session.execute(del_stmt)
            await session.commit()

    results: List[Dict[str, Any]] = []
    deleted_count = 0
    not_found_count = 0
    for scope_, sk in targets:
        ok = (scope_, sk) in existing
        results.append(
            {
                "scope": scope_,
                "state_key": sk,
                "deleted": ok,
                "reason": None if ok else "not_found",
            }
        )
        if ok:
            deleted_count += 1
        else:
            not_found_count += 1

    logger.info(
        t(
            "🗄️ [StateStore-API] 批量删除: requested={p0} deleted={deleted_count} not_found={not_found_count}",
            p0=len(targets),
            deleted_count=deleted_count,
            not_found_count=not_found_count,
        )
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "requested_count": len(targets),
            "deleted_count": deleted_count,
            "not_found_count": not_found_count,
            "results": results,
        },
    }


# 供 setup_frontend._import_webconsole_apis 显式 import 触发路由注册
__all__ = []  # 路由注册靠 import 副作用，不导出函数
_ = state_list_keys  # 防止未使用警告（state_list_keys 内部仅供 LLM 工具用，本 API 直接 SQL）
