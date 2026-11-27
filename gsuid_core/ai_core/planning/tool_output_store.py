"""全量工具/子代理落盘 FileOS：SQL 真身、句柄分页协议与 Qdrant 混合索引。"""

from __future__ import annotations

import re
import asyncio
from typing import List, Mapping, Optional, Sequence
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass

from sqlmodel import Field, SQLModel, UniqueConstraint, col, select
from sqlalchemy import Text, Column, or_, func
from sqlalchemy.sql import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.ai_core.rag.qmodels import (
    Filter,
    Vector,
    Distance,
    MatchAny,
    Modifier,
    Condition,
    MatchValue,
    PointStruct,
    VectorParams,
    FieldCondition,
    SparseVectorParams,
)
from gsuid_core.utils.database.base_models import with_session, with_read_session

# ── 1. SQL 真身（FileOS 表）──


# 同步删 payload：async 侧只能经 asyncio.to_thread 调它，文件 IO 不阻塞事件循环
def _unlink_payload(payload_path: str) -> None:
    p = Path(payload_path)
    if p.exists():
        p.unlink(missing_ok=True)


async def _unlink_rows_payloads(rows: Sequence["AIToolOutputRecord"]) -> tuple[list[str], list[str]]:
    """逐行去重删除磁盘 payload，返回 (去重路径列表, 记录 id 列表)。"""
    paths: list[str] = []
    rids: list[str] = []
    seen: set[str] = set()
    for rec in rows:
        rids.append(rec.id)
        if rec.payload_path and rec.payload_path not in seen:
            seen.add(rec.payload_path)
            paths.append(rec.payload_path)
            await asyncio.to_thread(_unlink_payload, rec.payload_path)
    return paths, rids


class _ToolOutputCRUD:
    """UUID 主键 CRUD（与 Kanban ``_PlanCRUD`` 同模式）。"""

    @classmethod
    @with_session
    async def batch_insert_data(cls, session: AsyncSession, rows: List["AIToolOutputRecord"]) -> None:
        if rows:
            session.add_all(rows)


class AIToolOutputRecord(_ToolOutputCRUD, SQLModel, table=True):
    """全量落盘：达标 ToolReturn / 子代理终态原文。"""

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "scope_key",
            "content_hash",
            "tool_name",
            name="uq_tool_output_owner_scope_hash_tool",
        ),
    )

    id: str = Field(primary_key=True, max_length=64)
    root_task_id: str = Field(index=True, max_length=36)
    task_id: str = Field(index=True, max_length=36)
    session_id: str = Field(index=True, max_length=256)
    owner_user_id: str = Field(index=True, max_length=64)
    scope_key: str = Field(index=True, max_length=64)
    tool_name: str = Field(index=True, max_length=64)
    profile: str = Field(default="", max_length=64)
    summary: str = Field(default="", max_length=512)
    date_str: str = Field(index=True, max_length=16)
    res_handle: str = Field(default="", max_length=64)
    # sha256 去重：同 owner+scope+hash 复用旧记录
    content_hash: str = Field(default="", index=True, max_length=64)
    payload_inline: Optional[str] = Field(default=None, sa_column=Column(Text))
    payload_path: str = Field(default="", max_length=512)
    size_bytes: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = Field(default=None, index=True)

    @classmethod
    @with_read_session
    async def get_by_id(
        cls,
        session: AsyncSession,
        rid: str,
    ) -> Optional["AIToolOutputRecord"]:
        result = await session.execute(select(cls).where(col(cls.id) == rid))
        return result.scalar_one_or_none()

    @classmethod
    @with_read_session
    async def get_by_hash(
        cls,
        session: AsyncSession,
        content_hash: str,
        owner_user_id: str = "",
        scope_key: str = "",
        tool_name: str = "",
    ) -> Optional["AIToolOutputRecord"]:
        """同 owner+scope+hash+tool_name 去重。"""
        if not content_hash:
            return None
        stmt = select(cls).where(col(cls.content_hash) == content_hash)
        if owner_user_id:
            stmt = stmt.where(col(cls.owner_user_id) == owner_user_id)
        if scope_key:
            stmt = stmt.where(col(cls.scope_key) == scope_key)
        stmt = stmt.where(col(cls.tool_name) == (tool_name or ""))
        stmt = stmt.order_by(col(cls.created_at).desc()).limit(1)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    @with_read_session
    async def search(
        cls,
        session: AsyncSession,
        owner_user_id: Optional[str] = None,
        scope_key: Optional[str] = None,
        tool_name: Optional[str] = None,
        keyword: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
        limit: int = 20,
    ) -> List["AIToolOutputRecord"]:
        stmt = select(cls)
        if owner_user_id:
            stmt = stmt.where(col(cls.owner_user_id) == owner_user_id)
        if scope_key:
            stmt = stmt.where(col(cls.scope_key) == scope_key)
        if tool_name:
            stmt = stmt.where(col(cls.tool_name) == tool_name)
        if session_id:
            stmt = stmt.where(col(cls.session_id) == session_id)
        if task_id:
            stmt = stmt.where(col(cls.task_id) == task_id)
        if root_task_id:
            stmt = stmt.where(col(cls.root_task_id) == root_task_id)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(col(cls.summary).like(like))
        stmt = stmt.order_by(col(cls.created_at).desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def list_recent(
        cls,
        *,
        owner_user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
        root_task_id: Optional[str] = None,
        limit: int = 20,
    ) -> List["AIToolOutputRecord"]:
        return await cls.search(
            owner_user_id=owner_user_id,
            session_id=session_id,
            task_id=task_id,
            root_task_id=root_task_id,
            limit=limit,
        )

    @classmethod
    @with_session
    async def delete_by_root_task_ids(
        cls,
        session: AsyncSession,
        root_task_ids: Sequence[str],
    ) -> tuple[int, List[str], List[str]]:
        """按 root_task_id 硬删 FileOS 行；返回 (条数, payload 路径, 记录 id)。"""
        from sqlmodel import delete as sql_delete

        ids = [r for r in root_task_ids if r]
        if not ids:
            return 0, [], []
        rows = list((await session.execute(select(cls).where(col(cls.root_task_id).in_(ids)))).scalars().all())
        if not rows:
            return 0, [], []
        paths, rids = await _unlink_rows_payloads(rows)
        await session.execute(sql_delete(cls).where(col(cls.root_task_id).in_(ids)))
        return len(rows), paths, rids

    @classmethod
    @with_session
    async def delete_expired(
        cls,
        session: AsyncSession,
        now: Optional[datetime] = None,
    ) -> tuple[int, List[str]]:
        """删除过期行与 payload 文件；返回 (条数, 记录 id 列表) 供索引清理。"""
        from sqlmodel import delete as sql_delete

        cut = now or datetime.now()
        cond = (col(cls.expires_at).is_not(None)) & (col(cls.expires_at) < cut)
        expired = list((await session.execute(select(cls).where(cond))).scalars().all())
        if not expired:
            return 0, []
        _paths, rids = await _unlink_rows_payloads(expired)
        await session.execute(sql_delete(cls).where(cond))
        return len(expired), rids

    @classmethod
    @with_session
    async def delete_by_ids(
        cls,
        session: AsyncSession,
        record_ids: Sequence[str],
    ) -> tuple[int, List[str]]:
        """按主键批量硬删；返回 (条数, 已删 id) 供 Qdrant 清理。"""
        from sqlmodel import delete as sql_delete

        ids = [r for r in record_ids if r]
        if not ids:
            return 0, []
        rows = list((await session.execute(select(cls).where(col(cls.id).in_(ids)))).scalars().all())
        if not rows:
            return 0, []
        _paths, rids = await _unlink_rows_payloads(rows)
        await session.execute(sql_delete(cls).where(col(cls.id).in_(rids)))
        return len(rids), rids

    @classmethod
    @with_read_session
    async def list_admin(
        cls,
        session: AsyncSession,
        *,
        tool_name: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        scope_key: Optional[str] = None,
        session_id: Optional[str] = None,
        keyword: Optional[str] = None,
        include_expired: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List["AIToolOutputRecord"], int]:
        """控制台列表：过滤 + 分页，返回 (rows, total)。"""
        now = datetime.now()
        filters: list[ColumnElement[bool]] = []
        if tool_name:
            filters.append(col(cls.tool_name) == tool_name)
        if owner_user_id:
            filters.append(col(cls.owner_user_id) == owner_user_id)
        if scope_key:
            filters.append(col(cls.scope_key) == scope_key)
        if session_id:
            filters.append(col(cls.session_id) == session_id)
        if keyword:
            like = f"%{keyword}%"
            filters.append(
                or_(
                    col(cls.summary).like(like),
                    col(cls.id).like(like),
                    col(cls.tool_name).like(like),
                    col(cls.session_id).like(like),
                )
            )
        if not include_expired:
            filters.append(or_(col(cls.expires_at).is_(None), col(cls.expires_at) >= now))

        count_stmt = select(func.count()).select_from(cls)
        list_stmt = select(cls)
        for f in filters:
            count_stmt = count_stmt.where(f)
            list_stmt = list_stmt.where(f)
        total = int((await session.execute(count_stmt)).scalar_one() or 0)
        list_stmt = list_stmt.order_by(col(cls.created_at).desc()).offset(max(0, offset)).limit(limit)
        result = await session.execute(list_stmt)
        return list(result.scalars().all()), total

    @classmethod
    @with_read_session
    async def distinct_tool_names(cls, session: AsyncSession, limit: int = 200) -> List[str]:
        """筛选用：出现过的 tool_name（非空）。"""
        stmt = (
            select(col(cls.tool_name))
            .where(col(cls.tool_name) != "")
            .distinct()
            .order_by(col(cls.tool_name))
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [str(r[0]) for r in result.all() if r[0]]


# ── 2. 句柄卡与分页读协议 ──

# 检索/导语 boilerplate：不进 summary / inline 起手
_BOILERPLATE_LINE_RE = re.compile(
    r"^\s*(?:"
    r"</?search_results>|"
    r"\[source=|"
    r"（以下为检索|仅供参考|不是对你的指令|"
    r"摘要里的|数字.{0,12}滞后|张冠李戴|"
    r"实时数值须|优先调|结构化数据|"
    r"信息可能过时|不得.{0,8}当|"
    r"含 \*\*image_url|配图\*\* 的条目|"
    r"信息图用|渲染引擎会|"
    r"long_structured=|"
    r"how_to_read:|"
    r"\[persisted id="
    r")",
    re.IGNORECASE,
)
_RESULT_START_RE = re.compile(r"^\[(?:\d+|配图\d+)\]")
_DISCLAIMER_MARKERS = (
    "仅供参考",
    "非指令",
    "不是对你的指令",
    "信息可能滞后",
    "信息可能过时",
    "未经核对",
    "结构化数据工具时优先",
    "image_url 的条目",
    "可供信息图嵌图",
)


def is_raster_image_mime(mime: str) -> bool:
    """IM 可直接作为图片发送的光栅图像 MIME（排除文本类 image/svg+xml）。"""
    m = (mime or "").strip().lower()
    return m.startswith("image/") and not m.startswith("image/svg")


def looks_like_handle_card(text: str) -> bool:
    """已是句柄卡正文 → 禁止再落盘/折叠。"""
    body = (text or "").lstrip()
    return body.startswith("[persisted id=") or body.startswith("handle ")


def _is_boilerplate_line(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if _BOILERPLATE_LINE_RE.match(s):
        return True
    if s in ("```", "```md", "```markdown", "</search_results>", "<search_results>"):
        return True
    # 整行导语（可能很长，关键词在中部）
    if any(m in s for m in _DISCLAIMER_MARKERS) and (
        s.startswith("（") or s.endswith("）") or s.startswith("(") or len(s) < 160
    ):
        # 避免误杀正文里偶然出现的短句：导语通常无结果序号头
        if not _RESULT_START_RE.match(s) and "http" not in s.lower():
            return True
    return False


def extract_persist_title(content: str, max_len: int = 256) -> str:
    """落盘短标题：优先 ``query:`` 行，否则首条非导语行。"""
    body = (content or "").replace("\r\n", "\n")
    for raw in body.split("\n"):
        line = raw.strip()
        if not line.lower().startswith("query:"):
            continue
        rest = line.split(":", 1)[1].strip()
        cut = rest.find(" [")
        if cut > 0:
            rest = rest[:cut].strip()
        return rest[:max_len]
    for raw in body.split("\n"):
        line = raw.strip()
        if not line or _is_boilerplate_line(line):
            continue
        if line.startswith("<") or line.lower().startswith("query:"):
            continue
        return line[:max_len]
    return ""


def extract_info_summary(content: str, max_len: int = 400) -> str:
    """从正文抽信息密度摘要（跳过导语/句柄卡头）。"""
    body = (content or "").replace("\r\n", "\n")
    if not body.strip():
        return ""
    if looks_like_handle_card(body):
        for line in body.split("\n"):
            if line.startswith("summary:"):
                return line[len("summary:") :].strip()[:max_len]
        return body[:max_len].replace("\n", " ")

    lines = body.split("\n")
    picked: list[str] = []
    started = False
    for raw in lines:
        line = raw.strip()
        if not line:
            if started:
                picked.append("")
            continue
        if _is_boilerplate_line(line):
            continue
        if not started and _RESULT_START_RE.match(line):
            started = True
        if not started and line.startswith("<"):
            continue
        if line in ("```", "```md", "```markdown"):
            continue
        started = True
        picked.append(line)
        if len(" ".join(picked)) >= max_len:
            break
    text = " ".join(x for x in picked if x).strip()
    if not text:
        text = re.sub(r"<search_results>\s*", "", body)
        text = re.sub(r"（[^）]{0,500}）", "", text, count=2)
        text = text.strip() or body.strip()
    return text[:max_len].replace("\n", " ")


def extract_inline_head(content: str, max_chars: int = 1200) -> str:
    """折叠时内嵌给模型的前几条要点（非全文）。"""
    body = (content or "").replace("\r\n", "\n")
    if not body.strip() or looks_like_handle_card(body):
        return ""
    out: list[str] = []
    size = 0
    started = False
    for raw in body.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if started and out and out[-1] != "":
                out.append("")
                size += 1
            continue
        if _is_boilerplate_line(stripped):
            continue
        if not started and stripped.startswith("<") and not _RESULT_START_RE.match(stripped):
            continue
        if stripped in ("```", "```md", "```markdown"):
            continue
        if not started and not (_RESULT_START_RE.match(stripped) or len(stripped) > 8):
            continue
        started = True
        if size + len(line) + 1 > max_chars:
            remain = max_chars - size - 1
            if remain > 40:
                out.append(line[:remain] + "…")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out).strip()


@dataclass(frozen=True)
class PersistedHandleCard:
    """模型可见的统一句柄卡；可带 inline 要点。"""

    id: str
    kind: str  # tool_output | artifact | image
    mime: str
    summary: str
    size_bytes: int
    read_tool: str = "read_handle"
    long_structured: bool = False
    inline_head: str = ""
    # False：主人格交付卡，禁止把「去展开全文」写成可执行下一步
    speech_expand: bool = True
    # False：当前人格花名册没有 render_agent，长结构改口头交付
    render_allowed: bool = True

    def format(self) -> str:
        how = f"read_handle(handle_id={self.id!r}, offset=0, limit=8000)"
        lines = [
            f"[persisted id={self.id} kind={self.kind} mime={self.mime} size={self.size_bytes}]",
            f"summary: {self.summary[:200]}",
        ]
        if self.speech_expand:
            lines.append(f"how_to_read: {how}  # 看返回文首【读窗口】再续读 offset；禁止全文念给用户")
        else:
            lines.append(f"read_tool={self.read_tool}  # 专职节点读全文；主人格只用 summary，禁止展开念台词")
        if self.long_structured:
            if self.render_allowed:
                lines.append(
                    "long_structured=true → 多维数据对比/长清单/研报攻略且未要求纯文本时，委派 create_subagent("
                    'agent_profile="render_agent", task=本id或版式要求) 出图；'
                    "受阻说明、操作回执或一两句能说清的诉求直接口头简短回应，禁止把对照表整段念进气泡"
                )
            else:
                lines.append(
                    "long_structured=true → 当前人格未启用出图委派：禁止委派 render_agent，"
                    "用一两句概括 summary 交付给用户，禁止把对照表整段念进气泡"
                )
        if self.kind == "image" and is_raster_image_mime(self.mime):
            lines.append("image=true → send_message_by_ai(image_id=本id) 直发，勿 read_handle 当文本")
        head = (self.inline_head or "").strip()
        if head:
            if self.long_structured:
                hint = "委派 render 出图" if self.render_allowed else "按 summary 口头交付"
                lines.append(f"inline_head:  # 对照要点，禁止念成台词；{hint}")
            else:
                lines.append("inline_head:  # 已含要点；需全文再 read_handle")
            lines.append(head)
        return "\n".join(lines)


def format_paginated_body(
    *,
    head: str,
    text: str,
    offset: int = 0,
    limit: int = 8000,
    read_hint: str = "",
) -> str:
    """分页切片 + 统一续读提示（窗口元信息置顶，防尾部截断丢失 offset）。"""
    total = len(text)
    off = max(0, int(offset))
    lim = max(1, int(limit))
    end = min(total, off + lim)
    sliced = text[off:end]
    got = len(sliced)
    more = end < total
    next_off = end if more else None

    window = f"【读窗口】offset={off} limit={lim} got={got} total={total}"
    if more and next_off is not None:
        window += f"；还有后续 → {read_hint} offset={next_off}" if read_hint else f"；还有后续 → offset={next_off}"
    else:
        window += "；已到文末" if off > 0 or got < total else "；全文已覆盖"

    if total <= got and off == 0:
        return head + f"{window}\npayload:\n{sliced}"

    suffix = f"\n…[分页 {off}-{end}/{total}"
    if more and next_off is not None:
        suffix += f"，续读 offset={next_off}"
    suffix += "]"
    return head + f"{window}\npayload:\n{sliced}{suffix}"


def load_payload_text(
    *,
    payload_inline: Optional[str],
    payload_path: str,
) -> tuple[str, Optional[str]]:
    """返回 (text, error)。"""
    if payload_inline:
        return payload_inline, None
    if payload_path:
        try:
            return Path(payload_path).read_text(encoding="utf-8", errors="replace"), None
        except OSError as e:
            return "", f"读取落盘失败: {e}"
    return "", None


def format_payload_paginated(
    *,
    head: str,
    payload_inline: Optional[str],
    payload_path: str,
    offset: int = 0,
    limit: int = 8000,
    read_hint: str = "",
    empty_label: str = "（无内容）",
) -> str:
    """加载 inline/落盘载荷并按统一分页协议格式化。"""
    text, err = load_payload_text(payload_inline=payload_inline, payload_path=payload_path)
    if err:
        return head + f"⚠️ {err}"
    if not text:
        return head + empty_label
    return format_paginated_body(
        head=head,
        text=text,
        offset=offset,
        limit=limit,
        read_hint=read_hint,
    )


def rrf_fuse(
    ranked_lists: list[list[str]],
    *,
    k: int = 60,
    limit: int = 8,
) -> list[str]:
    """Reciprocal Rank Fusion：多路 id 列表融合。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, rid in enumerate(lst):
            if not rid:
                continue
            prev = scores[rid] if rid in scores else 0.0
            scores[rid] = prev + 1.0 / (k + rank + 1)
    ordered = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return ordered[:limit]


# ── 3. Qdrant 混合索引（FileOS + Artifact 共用 dense+sparse 通道）──

TOOL_OUTPUT_COLLECTION = "tool_outputs"
TOOL_OUTPUT_DENSE = "dense"
TOOL_OUTPUT_SPARSE = "sparse"

ARTIFACT_COLLECTION = "artifacts"
ARTIFACT_DENSE = "dense"
ARTIFACT_SPARSE = "sparse"


def _dense_vectors_config(name: str, dim: int) -> dict[str, VectorParams]:
    return {name: VectorParams(size=dim, distance=Distance.COSINE, on_disk=True)}


def _sparse_vectors_config(name: str) -> dict[str, SparseVectorParams]:
    return {name: SparseVectorParams(modifier=Modifier.IDF)}


def tool_output_vectors_config(dim: int) -> dict[str, VectorParams]:
    return _dense_vectors_config(TOOL_OUTPUT_DENSE, dim)


def tool_output_sparse_config() -> dict[str, SparseVectorParams]:
    return _sparse_vectors_config(TOOL_OUTPUT_SPARSE)


def artifact_vectors_config(dim: int) -> dict[str, VectorParams]:
    return _dense_vectors_config(ARTIFACT_DENSE, dim)


def artifact_sparse_config() -> dict[str, SparseVectorParams]:
    return _sparse_vectors_config(ARTIFACT_SPARSE)


async def _ensure_hybrid_collection(
    *,
    collection_name: str,
    dense_name: str,
    sparse_name: str,
    keyword_fields: Sequence[str],
    is_artifact: bool,
) -> None:
    from gsuid_core.i18n import t
    from gsuid_core.logger import logger
    from gsuid_core.ai_core.rag.base import client, get_dimension, get_strict_dimension
    from gsuid_core.ai_core.rag.collection_migration import (
        ensure_payload_indexes,
        force_recreate_collection,
        collection_vector_mismatched,
    )

    if client is None:
        return
    try:
        dim = get_strict_dimension() or get_dimension()
        exists = await client.collection_exists(collection_name)
        vectors = _dense_vectors_config(dense_name, dim)
        sparse = _sparse_vectors_config(sparse_name)
        if exists and await collection_vector_mismatched(collection_name, dim, dense_name):
            if is_artifact:
                logger.warning(t("log.ai.artifact_index_dimension_mismatch_rebuild", p0=dim))
            else:
                logger.warning(t("log.ai.tool_output_index_dimension_mismatch_rebuild", p0=dim))
            await force_recreate_collection(
                collection_name=collection_name,
                vectors_config=vectors,
                sparse_vectors_config=sparse,
                on_disk_payload=True,
            )
        elif not exists:
            await client.create_collection(
                collection_name=collection_name,
                vectors_config=vectors,
                sparse_vectors_config=sparse,
                on_disk_payload=True,
            )
        await ensure_payload_indexes(
            collection_name=collection_name,
            keyword_fields=list(keyword_fields),
        )
    except Exception as e:
        logger.warning(t("log.ai.tool_output_index_ensure_fail", e=e))


async def _search_hybrid_collection(
    *,
    collection_name: str,
    dense_name: str,
    sparse_name: str,
    query: str,
    owner_user_id: Optional[str],
    scope_key: Optional[str],
    limit: int,
) -> List[dict[str, object]]:
    from gsuid_core.ai_core.rag.base import client, embedding_model
    from gsuid_core.ai_core.rag.hybrid import hybrid_query
    from gsuid_core.ai_core.rag.sparse import sparse_embed_single

    # fail-closed：无 owner 不扫全局向量
    if client is None or embedding_model is None or not query.strip() or not owner_user_id:
        return []
    dense = await embedding_model.aembed_query(query)
    sparse = await sparse_embed_single(query)
    must_conditions: list[Condition] = [
        FieldCondition(key="owner_user_id", match=MatchValue(value=owner_user_id)),
    ]
    if scope_key:
        must_conditions.append(FieldCondition(key="scope_key", match=MatchValue(value=scope_key)))
    points = await hybrid_query(
        collection_name=collection_name,
        query_dense=dense,
        query_sparse=sparse,
        limit=limit,
        dense_using=dense_name,
        sparse_using=sparse_name,
        query_filter=Filter(must=must_conditions),
    )
    out: List[dict[str, object]] = []
    for p in points:
        pay: dict[str, object] = dict(p.payload or {})
        pay["_score"] = float(p.score)
        out.append(pay)
    return out


async def ensure_tool_output_collection() -> None:
    await _ensure_hybrid_collection(
        collection_name=TOOL_OUTPUT_COLLECTION,
        dense_name=TOOL_OUTPUT_DENSE,
        sparse_name=TOOL_OUTPUT_SPARSE,
        keyword_fields=("scope_key", "tool_name", "owner_user_id", "date_str", "res_handle"),
        is_artifact=False,
    )


async def index_tool_output_chunks(
    chunks: List[str],
    payload: Mapping[str, object],
) -> None:
    from gsuid_core.ai_core.rag.base import client, get_point_id, embedding_model
    from gsuid_core.ai_core.rag.sparse import sparse_embed_batch_async

    if client is None or embedding_model is None or not chunks:
        return
    texts = [c for c in chunks if c.strip()]
    if not texts:
        return
    dense_vecs: List[Sequence[float]] = list(await embedding_model.aembed(texts))
    sparse_vecs = await sparse_embed_batch_async(texts)

    points: List[PointStruct] = []
    base_id = str(payload["id"]) if "id" in payload else ""
    for idx, txt in enumerate(texts):
        pid = get_point_id(f"{base_id}_{idx}")
        payload_ex: dict[str, object] = {**payload, "chunk_idx": idx, "text": txt[:2000]}
        vector: dict[str, Vector] = {TOOL_OUTPUT_DENSE: list(dense_vecs[idx])}
        sparse = sparse_vecs[idx] if idx < len(sparse_vecs) else None
        if sparse is not None:
            vector[TOOL_OUTPUT_SPARSE] = sparse
        points.append(PointStruct(id=pid, vector=vector, payload=payload_ex))
    await client.upsert(collection_name=TOOL_OUTPUT_COLLECTION, points=points, wait=False)


async def search_tool_outputs(
    query: str,
    limit: int = 8,
    scope_key: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> List[dict[str, object]]:
    return await _search_hybrid_collection(
        collection_name=TOOL_OUTPUT_COLLECTION,
        dense_name=TOOL_OUTPUT_DENSE,
        sparse_name=TOOL_OUTPUT_SPARSE,
        query=query,
        owner_user_id=owner_user_id,
        scope_key=scope_key,
        limit=limit,
    )


async def delete_tool_output_index(record_ids: Sequence[str]) -> None:
    """按 payload.id 批量删 Qdrant 点（TTL / 任务硬删后清理悬空向量）。"""
    from gsuid_core.i18n import t
    from gsuid_core.logger import logger
    from gsuid_core.ai_core.rag.base import client

    ids = [str(x) for x in record_ids if x]
    if client is None or not ids:
        return
    try:
        await client.delete(
            collection_name=TOOL_OUTPUT_COLLECTION,
            points_selector=Filter(must=[FieldCondition(key="id", match=MatchAny(any=ids))]),
        )
    except Exception as e:
        logger.debug(t("log.ai.tool_output_index_delete_skip", e=e))


async def ensure_artifact_collection() -> None:
    await _ensure_hybrid_collection(
        collection_name=ARTIFACT_COLLECTION,
        dense_name=ARTIFACT_DENSE,
        sparse_name=ARTIFACT_SPARSE,
        keyword_fields=("scope_key", "owner_user_id", "date_str", "profile"),
        is_artifact=True,
    )


async def index_artifact(
    *,
    art_id: str,
    summary: str,
    text: str,
    owner_user_id: str,
    scope_key: str,
    date_str: str,
    profile: str,
) -> None:
    from gsuid_core.ai_core.rag.base import client, get_point_id, embedding_model
    from gsuid_core.ai_core.rag.sparse import sparse_embed_batch_async

    if client is None or embedding_model is None or not art_id:
        return
    body = (summary or "").strip()
    extra = (text or "").strip()
    if extra and extra != body:
        body = f"{body}\n{extra[:800]}".strip()
    if not body:
        return
    await ensure_artifact_collection()
    dense_vecs: List[Sequence[float]] = list(await embedding_model.aembed([body]))
    sparse_vecs = await sparse_embed_batch_async([body])
    vector: dict[str, Vector] = {ARTIFACT_DENSE: list(dense_vecs[0])}
    if sparse_vecs and sparse_vecs[0] is not None:
        vector[ARTIFACT_SPARSE] = sparse_vecs[0]
    payload: dict[str, object] = {
        "id": art_id,
        "summary": summary[:512],
        "owner_user_id": owner_user_id,
        "scope_key": scope_key,
        "date_str": date_str,
        "profile": profile,
    }
    await client.upsert(
        collection_name=ARTIFACT_COLLECTION,
        points=[PointStruct(id=get_point_id(art_id), vector=vector, payload=payload)],
        wait=False,
    )


async def search_artifacts_hybrid(
    query: str,
    *,
    owner_user_id: str,
    scope_key: Optional[str] = None,
    limit: int = 8,
) -> List[dict[str, object]]:
    return await _search_hybrid_collection(
        collection_name=ARTIFACT_COLLECTION,
        dense_name=ARTIFACT_DENSE,
        sparse_name=ARTIFACT_SPARSE,
        query=query,
        owner_user_id=owner_user_id,
        scope_key=scope_key,
        limit=limit,
    )
