"""认知节点表 + 跨 kind 边：**索引与关系层，不是第二份正文**。

红线（否则两份正文立刻不一致）：

- 节点只存 **身份 / kind / ref / 摘要 / scope / 时间 / decay**。正文仍住在原库里
  （知识分片在 ``AIKnowledgeChunk``、Episode 在 ``AIMemEpisode``、落盘在 FileOS…）。
- **不把**知识正文搬进 ``aimementity``，**不把** Episode 塞进 ``knowledge``。
  知识要分片/对账/插件同步，落盘要 TTL/ACL，偏好要精确 SQL——三套生命周期合成一张会搅死。
- 边只表达关系（RELATED / SUPPORTS / SUPERSEDES / DERIVED_FROM），不带正文。

「记忆的节点也可以是知识库节点」在这里落地：``entity:用户A --RELATED--> knowledge:出图规范#3``
是一条边，而不是把知识复制成一个 Entity。
"""

import time
from enum import Enum
from typing import List, Tuple, Optional, TypedDict

from sqlmodel import Field, SQLModel, col, or_, and_, delete, select
from sqlalchemy import Text, Column, UniqueConstraint, case, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.cognition.types import CogKind
from gsuid_core.utils.database.base_models import with_session

# 必须带属主才可见的 kind：它们在联邦里的兄弟后端（FileOS `owner_user_id` 行级过滤、
# Artifact `search_recent_for_owner`）都是行级 ACL。节点层只按 scope_key 过滤会把这层
# ACL 悄悄降成 group 级——同群成员就能搜到别人的任务结论与产物摘要。
OWNER_REQUIRED_KINDS = frozenset({CogKind.TOOL_OUTPUT.value, CogKind.ARTIFACT.value})


class CogEdgeKind(str, Enum):
    """跨 kind 边的最小集。语义不可扩散——新增边种要先想清楚它怎么参与检索。"""

    RELATED = "related"
    SUPPORTS = "supports"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"


class AICogNode(SQLModel, table=True):
    """认知节点（表名 ``aicognode``）。1 行 = 1 个可回想对象的**身份**。

    ``ref`` 指回原库的主键（Episode id / 知识逻辑 ID / FileOS record id / …）。
    删原库行时节点可留（``decay`` 走衰减），但 ``read_handle`` 会取不到正文——
    这正是「结论边还在、句柄已失效」的预期形态。
    """

    __table_args__ = (
        UniqueConstraint("kind", "ref", name="ux_aicognode_kind_ref"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(index=True, max_length=32, title="节点类型")
    ref: str = Field(index=True, max_length=160, title="原库主键")
    scope_key: str = Field(default="", index=True, max_length=128, title="可见范围")
    # 行级属主。只按 scope_key 会把 ACL 从 owner 降成 group，必须与 FileOS 对齐。
    owner_user_id: str = Field(default="", index=True, max_length=64, title="属主")
    title: str = Field(default="", max_length=256, title="标题")
    summary: str = Field(default="", sa_column=Column(Text, nullable=False), title="摘要(非正文)")
    # 数据的时点。落盘 / 产物必须带，否则模型会把过期数字当现在。
    as_of: str = Field(default="", max_length=32, title="时点")
    source: str = Field(default="", max_length=32, title="来源(plugin/manual/tool/self_action)")
    handle: str = Field(default="", max_length=64, title="可读全文的句柄")
    canon: str = Field(default="", index=True, max_length=160, title="世界枢纽ref")
    decay: float = Field(default=1.0, title="时效衰减分")
    created_at: int = Field(default_factory=lambda: int(time.time()), title="创建时间戳")
    updated_at: int = Field(default_factory=lambda: int(time.time()), title="更新时间戳")

    @classmethod
    @with_session
    async def upsert(
        cls,
        session: AsyncSession,
        *,
        kind: CogKind,
        ref: str,
        scope_key: str = "",
        owner_user_id: str = "",
        title: str = "",
        summary: str = "",
        as_of: str = "",
        source: str = "",
        handle: str = "",
        canon: str = "",
    ) -> Optional[int]:
        """按 ``(kind, ref)`` 幂等 upsert，返回节点 id。摘要为空时不覆盖已有摘要。"""
        if not ref:
            return None
        if kind.value in OWNER_REQUIRED_KINDS and not owner_user_id:
            # 执行世界的节点没有属主就无法做行级 ACL，写进去只会变成谁都能搜到的公共行
            logger.debug(i18n_t("log.ai.cognition_node_owner_required", kind=kind.value, ref=ref))
            return None
        stmt = select(cls).where(and_(col(cls.kind) == kind.value, col(cls.ref) == ref))
        existing = (await session.execute(stmt)).scalars().first()
        now = int(time.time())
        if existing is None:
            node = cls(
                kind=kind.value,
                ref=ref,
                scope_key=scope_key,
                owner_user_id=owner_user_id,
                title=title,
                summary=summary,
                as_of=as_of,
                source=source,
                handle=handle,
                canon=canon,
            )
            session.add(node)
            await session.commit()
            await session.refresh(node)
            return node.id
        existing.title = title or existing.title
        existing.summary = summary or existing.summary
        existing.as_of = as_of or existing.as_of
        existing.handle = handle or existing.handle
        existing.scope_key = scope_key or existing.scope_key
        existing.owner_user_id = owner_user_id or existing.owner_user_id
        existing.canon = canon or existing.canon
        existing.updated_at = now
        await session.commit()
        return existing.id

    @classmethod
    @with_session
    async def get(cls, session: AsyncSession, kind: CogKind, ref: str) -> Optional["AICogNode"]:
        stmt = select(cls).where(and_(col(cls.kind) == kind.value, col(cls.ref) == ref))
        return (await session.execute(stmt)).scalars().first()

    @classmethod
    @with_session
    async def search(
        cls,
        session: AsyncSession,
        keyword: str,
        *,
        scope_keys: List[str],
        owner_user_id: str,
        kinds: Optional[List[str]] = None,
        limit: int = 12,
    ) -> List["AICogNode"]:
        """按关键词 + scope + **属主**检索节点（过滤全部下推到 SQL，不做内存筛）。

        ``owner_user_id`` 必填、无内部兜底：它是行级 ACL 的唯一依据，给默认值等于
        给一条「忘了传就全库可见」的捷径（联邦里另外两路后端都是必填 owner）。

        可见性 = scope 命中 **且** 属主命中：
        - scope：``""``（公共，如知识库）或调用方所在的 scope；
        - 属主：本人的行，或「无属主且不属于必须带属主的 kind」的行。
          ``tool_output`` / ``artifact`` 这类执行世界节点若没有属主，一律不可见——
          宁可召回不到，也不能把别人的任务结论摘要发出去。
        """
        from gsuid_core.ai_core.entity_index import _is_indexable, _normalize_surface

        conds: List[ColumnElement[bool]] = []
        kw = keyword.strip()
        exact_rank = case((col(cls.id).is_not(None), 1), else_=1)
        if kw:
            norm = _normalize_surface(kw)
            title_exact = or_(
                col(cls.title) == kw,
                col(cls.title) == norm,
                func.lower(col(cls.title)) == norm,
            )
            text_parts: List[ColumnElement[bool]] = [title_exact, col(cls.summary).contains(kw)]
            if _is_indexable(norm):
                text_parts.append(col(cls.title).contains(kw))
            conds.append(or_(*text_parts))
            exact_rank = case((title_exact, 0), else_=1)
        if kinds:
            conds.append(col(cls.kind).in_(kinds))
        visible = ["", *scope_keys]
        conds.append(col(cls.scope_key).in_(visible))
        public_row = and_(
            col(cls.owner_user_id) == "",
            col(cls.kind).notin_(sorted(OWNER_REQUIRED_KINDS)),
        )
        if owner_user_id:
            conds.append(or_(col(cls.owner_user_id) == owner_user_id, public_row))
        else:
            conds.append(public_row)
        stmt = (
            select(cls)
            .where(and_(*conds))
            .order_by(exact_rank, col(cls.decay).desc(), col(cls.updated_at).desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def get_by_id(cls, session: AsyncSession, node_id: int) -> Optional["AICogNode"]:
        stmt = select(cls).where(col(cls.id) == node_id)
        return (await session.execute(stmt)).scalars().first()

    @classmethod
    @with_session
    async def list_world_hubs_by_title(cls, session: AsyncSession, title: str) -> List["AICogNode"]:
        """公共世界枢纽：``scope_key=""`` 且 ``ref`` 以 ``world:`` 开头，title 归一化相等。"""
        from gsuid_core.ai_core.entity_index import _normalize_surface

        raw = (title or "").strip()
        if not raw:
            return []
        # SQL 侧用 lower 对齐 ASCII 大小写；内存再跑一遍归一化（CJK / 空白）。
        norm = _normalize_surface(raw)
        stmt = select(cls).where(
            and_(
                col(cls.scope_key) == "",
                col(cls.kind) == CogKind.ENTITY.value,
                col(cls.ref).startswith("world:"),
                or_(col(cls.title) == raw, func.lower(col(cls.title)) == norm),
            )
        )
        rows = list((await session.execute(stmt)).scalars().all())
        return [n for n in rows if _normalize_surface(n.title) == norm]

    @classmethod
    @with_session
    async def list_env_nodes_by_canon(
        cls,
        session: AsyncSession,
        canon: str,
        scope_key: str,
    ) -> List["AICogNode"]:
        if not canon or not scope_key:
            return []
        stmt = select(cls).where(
            and_(
                col(cls.canon) == canon,
                col(cls.scope_key) == scope_key,
                col(cls.kind) == CogKind.ENTITY.value,
                col(cls.ref).startswith("ent:"),
            )
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def list_by_ref_prefixes(cls, session: AsyncSession, prefixes: List[str]) -> List["AICogNode"]:
        if not prefixes:
            return []
        prefix_cond = or_(*[col(cls.ref).startswith(p) for p in prefixes])
        stmt = select(cls).where(prefix_cond)
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def delete_by_ids(cls, session: AsyncSession, node_ids: List[int]) -> int:
        if not node_ids:
            return 0
        result = await session.execute(delete(cls).where(col(cls.id).in_(node_ids)))
        await session.commit()
        from sqlalchemy.engine import CursorResult

        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def delete_by_ref(cls, session: AsyncSession, kind: CogKind, ref: str) -> int:
        result = await session.execute(delete(cls).where(and_(col(cls.kind) == kind.value, col(cls.ref) == ref)))
        await session.commit()
        from sqlalchemy.engine import CursorResult

        return result.rowcount if isinstance(result, CursorResult) else 0


class AICogAttachment(SQLModel, table=True):
    """枢纽挂文索引（表名 ``aicogattachment``）。只存元数据，正文仍在原库。"""

    __table_args__ = (
        UniqueConstraint("node_id", "ref", name="ux_aicogattachment_node_ref"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    node_id: int = Field(index=True, title="枢纽节点")
    slot: str = Field(default="资料", max_length=16, title="栏目")
    title: str = Field(default="", max_length=256, title="标题")
    summary: str = Field(default="", max_length=200, title="摘要(非正文)")
    as_of: str = Field(default="", max_length=32, title="时点")
    source: str = Field(default="", max_length=32, title="来源")
    writable: bool = Field(default=False, title="可更新")
    ref: str = Field(default="", index=True, max_length=192, title="原库主键")
    handle: str = Field(default="", max_length=192, title="句柄")
    created_at: int = Field(default_factory=lambda: int(time.time()), title="创建时间戳")
    updated_at: int = Field(default_factory=lambda: int(time.time()), title="更新时间戳")

    @classmethod
    @with_session
    async def upsert(
        cls,
        session: AsyncSession,
        *,
        node_id: int,
        ref: str,
        slot: str,
        title: str,
        summary: str,
        as_of: str,
        source: str,
        writable: bool,
        handle: str,
    ) -> Optional[int]:
        if not node_id or not ref:
            return None
        clipped = (summary or "")[:200]
        stmt = select(cls).where(and_(col(cls.node_id) == node_id, col(cls.ref) == ref))
        existing = (await session.execute(stmt)).scalars().first()
        now = int(time.time())
        if existing is None:
            row = cls(
                node_id=node_id,
                ref=ref,
                slot=slot,
                title=title,
                summary=clipped,
                as_of=as_of,
                source=source,
                writable=writable,
                handle=handle,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id
        existing.slot = slot or existing.slot
        existing.title = title or existing.title
        existing.summary = clipped or existing.summary
        existing.as_of = as_of or existing.as_of
        existing.source = source or existing.source
        existing.writable = writable
        existing.handle = handle or existing.handle
        existing.updated_at = now
        await session.commit()
        return existing.id

    @classmethod
    @with_session
    async def list_for_node(cls, session: AsyncSession, node_id: int) -> List["AICogAttachment"]:
        stmt = select(cls).where(col(cls.node_id) == node_id).order_by(col(cls.slot), col(cls.title))
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def list_for_nodes(cls, session: AsyncSession, node_ids: List[int]) -> List["AICogAttachment"]:
        if not node_ids:
            return []
        stmt = (
            select(cls).where(col(cls.node_id).in_(node_ids)).order_by(col(cls.node_id), col(cls.slot), col(cls.title))
        )
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def find_by_refs(cls, session: AsyncSession, refs: List[str]) -> List["AICogAttachment"]:
        if not refs:
            return []
        stmt = select(cls).where(col(cls.ref).in_(refs))
        return list((await session.execute(stmt)).scalars().all())

    @classmethod
    @with_session
    async def find_writable_by_title(
        cls,
        session: AsyncSession,
        node_id: int,
        title: str,
    ) -> Optional["AICogAttachment"]:
        norm = (title or "").strip().lower()
        stmt = select(cls).where(
            and_(
                col(cls.node_id) == node_id,
                func.lower(col(cls.title)) == norm,
                col(cls.writable).is_(True),
            )
        )
        return (await session.execute(stmt)).scalars().first()

    @classmethod
    @with_session
    async def find_by_node_and_title(
        cls,
        session: AsyncSession,
        node_id: int,
        title: str,
    ) -> Optional["AICogAttachment"]:
        norm = (title or "").strip().lower()
        stmt = select(cls).where(and_(col(cls.node_id) == node_id, func.lower(col(cls.title)) == norm))
        return (await session.execute(stmt)).scalars().first()

    @classmethod
    @with_session
    async def delete_by_ids(cls, session: AsyncSession, att_ids: List[int]) -> int:
        if not att_ids:
            return 0
        result = await session.execute(delete(cls).where(col(cls.id).in_(att_ids)))
        await session.commit()
        from sqlalchemy.engine import CursorResult

        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def delete_all(cls, session: AsyncSession) -> int:
        result = await session.execute(delete(cls))
        await session.commit()
        from sqlalchemy.engine import CursorResult

        return result.rowcount if isinstance(result, CursorResult) else 0

    @classmethod
    @with_session
    async def list_plugin_refs(cls, session: AsyncSession) -> List["AICogAttachment"]:
        stmt = select(cls).where(col(cls.source) == "plugin")
        return list((await session.execute(stmt)).scalars().all())


class AICogEdge(SQLModel, table=True):
    """跨 kind 边（表名 ``aicogedge``）。只表达关系，不带正文。"""

    __table_args__ = (
        UniqueConstraint("src_id", "dst_id", "edge_kind", name="ux_aicogedge_triple"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    src_id: int = Field(index=True, title="源节点")
    dst_id: int = Field(index=True, title="目标节点")
    edge_kind: str = Field(default=CogEdgeKind.RELATED.value, max_length=24, title="边类型")
    confidence: float = Field(default=0.7, title="置信度")
    created_at: int = Field(default_factory=lambda: int(time.time()), title="创建时间戳")

    @classmethod
    @with_session
    async def link(
        cls,
        session: AsyncSession,
        src_id: int,
        dst_id: int,
        edge_kind: CogEdgeKind = CogEdgeKind.RELATED,
        confidence: float = 0.7,
    ) -> bool:
        """幂等连边。自环与缺 id 直接拒绝。"""
        if not src_id or not dst_id or src_id == dst_id:
            return False
        stmt = select(cls).where(
            and_(
                col(cls.src_id) == src_id,
                col(cls.dst_id) == dst_id,
                col(cls.edge_kind) == edge_kind.value,
            )
        )
        if (await session.execute(stmt)).scalars().first() is not None:
            return False
        session.add(cls(src_id=src_id, dst_id=dst_id, edge_kind=edge_kind.value, confidence=confidence))
        await session.commit()
        return True

    @classmethod
    @with_session
    async def neighbors(
        cls,
        session: AsyncSession,
        node_id: int,
        limit: int = 12,
    ) -> List[Tuple[int, str]]:
        """取邻居 ``(node_id, edge_kind)``（双向）。给 WebConsole「从用户A点到知识分片」用。"""
        out = await session.execute(select(cls).where(col(cls.src_id) == node_id).limit(limit))
        inb = await session.execute(select(cls).where(col(cls.dst_id) == node_id).limit(limit))
        pairs: List[Tuple[int, str]] = [(e.dst_id, e.edge_kind) for e in out.scalars().all()]
        pairs.extend((e.src_id, e.edge_kind) for e in inb.scalars().all())
        return pairs[:limit]

    @classmethod
    @with_session
    async def delete_involving(cls, session: AsyncSession, node_ids: List[int]) -> int:
        if not node_ids:
            return 0
        result = await session.execute(
            delete(cls).where(or_(col(cls.src_id).in_(node_ids), col(cls.dst_id).in_(node_ids)))
        )
        await session.commit()
        from sqlalchemy.engine import CursorResult

        return result.rowcount if isinstance(result, CursorResult) else 0


async def sync_node(
    kind: CogKind,
    ref: str,
    *,
    scope_key: str = "",
    owner_user_id: str = "",
    title: str = "",
    summary: str = "",
    as_of: str = "",
    source: str = "",
    handle: str = "",
    canon: str = "",
) -> Optional[int]:
    """写入钩子的统一入口（best-effort：失败只丢节点，绝不影响原库写入）。

    实际调用点只有 ``cognition/distill.py`` 的三条蒸馏路径（工具落盘 / Kanban 终态 /
    自我笔记）。``OWNER_REQUIRED_KINDS`` 的节点缺 ``owner_user_id`` 时会被 upsert 拒绝。
    """
    try:
        return await AICogNode.upsert(
            kind=kind,
            ref=ref,
            scope_key=scope_key,
            owner_user_id=owner_user_id,
            title=title,
            summary=summary,
            as_of=as_of,
            source=source,
            handle=handle,
            canon=canon,
        )
    except Exception as e:
        logger.debug(i18n_t("log.ai.cognition_node_sync_fail", kind=kind.value, ref=ref, e=e))
        return None


async def link_nodes(
    src: Tuple[CogKind, str],
    dst: Tuple[CogKind, str],
    edge_kind: CogEdgeKind = CogEdgeKind.RELATED,
    confidence: float = 0.7,
) -> bool:
    """按 ``(kind, ref)`` 连边（两端不存在则先不连，等各自的 sync 钩子把节点建起来）。"""
    try:
        left = await AICogNode.get(src[0], src[1])
        right = await AICogNode.get(dst[0], dst[1])
        if left is None or right is None or left.id is None or right.id is None:
            return False
        return await AICogEdge.link(left.id, right.id, edge_kind, confidence)
    except Exception as e:
        logger.debug(i18n_t("log.ai.cognition_edge_link_fail", e=e))
        return False


class CogNodeDict(TypedDict):
    id: int | None
    kind: str
    ref: str
    scope_key: str
    owner_user_id: str
    title: str
    summary: str
    as_of: str
    source: str
    handle: str
    canon: str
    decay: float


class CogAttachmentDict(TypedDict):
    id: int | None
    node_id: int
    slot: str
    title: str
    summary: str
    as_of: str
    source: str
    writable: bool
    ref: str
    handle: str


def node_visible_to(node: AICogNode, *, owner_user_id: str, scope_keys: List[str]) -> bool:
    """与 ``AICogNode.search`` 同一套属主 / scope 可见性；对不上就当不存在。"""
    if node.scope_key not in ("", *scope_keys):
        return False
    public_row = node.owner_user_id == "" and node.kind not in OWNER_REQUIRED_KINDS
    if owner_user_id:
        return node.owner_user_id == owner_user_id or public_row
    return public_row


def node_to_dict(node: AICogNode) -> CogNodeDict:
    """WebConsole / 诊断输出形状。"""
    return {
        "id": node.id,
        "kind": node.kind,
        "ref": node.ref,
        "scope_key": node.scope_key,
        "owner_user_id": node.owner_user_id,
        "title": node.title,
        "summary": node.summary,
        "as_of": node.as_of,
        "source": node.source,
        "handle": node.handle,
        "canon": node.canon,
        "decay": node.decay,
    }


def attachment_to_dict(row: AICogAttachment) -> CogAttachmentDict:
    return {
        "id": row.id,
        "node_id": row.node_id,
        "slot": row.slot,
        "title": row.title,
        "summary": row.summary,
        "as_of": row.as_of,
        "source": row.source,
        "writable": row.writable,
        "ref": row.ref,
        "handle": row.handle,
    }
