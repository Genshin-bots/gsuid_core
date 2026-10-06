"""开发文档 Skill（``.agents/skills``）→ 知识库挂载（启动期）+ 命名空间检索（通用）。

把 ``.agents/skills/<skill>/`` 下的全部 SKILL 文档（``references/*.md``，无 references 时退回
``SKILL.md``）在框架启动时挂载进知识库，供能力代理（如 ``plugin_developer_agent``）用
**混合检索（dense + BM25 稀疏 RRF）**按需查阅——取代在单文件里做子串标题匹配的脆弱方式。

本模块**发现并挂载 .agents/skills 下的每一个 skill**（如 ``gscore-plugin-development`` /
``gscore-ai-core-api`` / ``gscore-adapter-development``），新增 skill 目录无需改代码、自动纳入。

## 隔离设计（关键）

所有分片统一写 ``source="skill_doc"``，每个 skill 各自占一个命名空间 ``plugin="skilldoc:<skill>"``：

- 与插件知识（``source="plugin"``）/ 手动知识（``source="manual"``）的同步、对账互不干扰
  （``sync_knowledge`` 只清 plugin 来源、``reconcile_manual_knowledge`` 只管 manual 来源）。
- 通用 ``search_cognition`` 与意图分类器按 ``exclude_sources=["skill_doc"]`` 把**整类**开发
  文档挡在日常聊天 RAG 之外（一处排除覆盖全部 skill、且对将来新增 skill 自动生效），避免污染。
- 能力代理用 ``search_skill_docs`` 工具按 ``plugin="skilldoc:<skill>"`` 命名空间过滤检索；
  不限定 skill 时检索全部已挂载 skill。

## 幂等

按每篇文件内容哈希（含分片策略版本）跳过未变化文档，避免每次启动重复嵌入数百分片；当
``skill_doc`` 这一类在向量库被清空（本地库丢失 / 重置）时强制重嵌自愈。维度迁移由
``init_knowledge_collection`` 的全量 payload 备份重嵌统一覆盖（它 scroll 全量点、含本类）。
"""

import json
import hashlib
from typing import Dict, List, Optional
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.chunking import (
    CHUNKER_ID,
    DEFAULT_CHUNK_OVERLAP,
    document_bodies,
    embed_char_budget,
    current_max_input_tokens,
)

# 全部 skill 开发文档共用的来源标记：聊天侧据此一处排除整类。
SKILLS_DOC_SOURCE: str = "skill_doc"

# 每个 skill 的命名空间前缀（写入 payload 的 plugin 字段）：skilldoc:<skill 目录名>。
_SKILL_NS_PREFIX: str = "skilldoc:"
# doc_id 形如 skilldoc::<skill>::<文件名 stem>；内容哈希写进分片 tags 做幂等判定。
_DOC_ID_PREFIX: str = "skilldoc::"
_HASH_TAG_PREFIX: str = "_srchash:"

# .agents/skills 根目录（相对仓库根；parents[3] 即仓库根）。
_SKILLS_ROOT: Path = Path(__file__).resolve().parents[3] / ".agents" / "skills"


def skill_doc_namespace(skill: str) -> str:
    """skill 目录名 → 知识库 plugin 命名空间值。"""
    return f"{_SKILL_NS_PREFIX}{skill}"


def _discover_skill_docs() -> Dict[str, List[Path]]:
    """发现 .agents/skills 下每个 skill 及其文档文件。

    优先取 ``<skill>/references/*.md``（正文）；无 references 目录时退回单篇 ``<skill>/SKILL.md``。
    返回 ``{skill 目录名: [md 文件...]}``（均按文件名稳定排序）。
    """
    result: Dict[str, List[Path]] = {}
    if not _SKILLS_ROOT.is_dir():
        return result
    for skill_dir in sorted(p for p in _SKILLS_ROOT.iterdir() if p.is_dir()):
        refs_dir = skill_dir / "references"
        if refs_dir.is_dir():
            files = sorted(refs_dir.glob("*.md"))
        else:
            skill_md = skill_dir / "SKILL.md"
            files = [skill_md] if skill_md.is_file() else []
        if files:
            result[skill_dir.name] = files
    return result


def known_skill_names() -> List[str]:
    """当前可挂载的 skill 目录名列表（供工具校验 / 提示）。"""
    return sorted(_discover_skill_docs().keys())


def _doc_id_for(skill: str, path: Path) -> str:
    return f"{_DOC_ID_PREFIX}{skill}::{path.stem}"


def _doc_title(text: str, fallback: str) -> str:
    """取首个一级标题作章节标题；无则用文件名兜底。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _content_hash(text: str) -> str:
    # 切法编号和模型 token 上限折进哈希。片文本没变时只改哈希、不重嵌。
    payload = f"{CHUNKER_ID}\x00{current_max_input_tokens()}\x00{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ───────────────────────── 启动挂载 + 检索 ─────────────────────────


async def _skill_docs_point_count() -> int:
    """知识库里 ``skill_doc`` 整类的现存向量点数；统计失败返回 -1（按"非空"处理，不触发强制重嵌）。"""
    from qdrant_client.models import Filter, MatchValue, FieldCondition

    from gsuid_core.ai_core.rag.base import KNOWLEDGE_COLLECTION_NAME, client

    if client is None:
        return -1
    try:
        result = await client.count(
            collection_name=KNOWLEDGE_COLLECTION_NAME,
            count_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=SKILLS_DOC_SOURCE))]),
        )
        return result.count
    except Exception as e:
        logger.debug(t("log.rag.skillskb_skill_doc_points_fail", e=e))
        return -1


async def sync_skill_docs() -> None:
    """启动期把 .agents/skills 下全部 skill 文档挂载进知识库（幂等）。供 ``rag.startup.init_all`` 调用。"""
    from gsuid_core.ai_core.rag.base import client, embedding_model
    from gsuid_core.ai_core.rag.knowledge import (
        add_knowledge_document,
        delete_knowledge_document,
    )
    from gsuid_core.ai_core.database.models import AIKnowledgeChunk

    skills = _discover_skill_docs()
    if not skills:
        logger.warning(t("log.rag.skillskb_skill_documents_found_skip", _SKILLS_ROOT=_SKILLS_ROOT))
        return
    if client is None or embedding_model is None:
        logger.debug(t("log.rag.skillskb_ready_skipping_skill"))
        return

    # 现存 skill_doc 分片：doc_id -> 已存内容哈希（取自分片 tags 里的 _srchash:）
    existing_rows = await AIKnowledgeChunk.iter_all(source=SKILLS_DOC_SOURCE)
    existing_hash: Dict[str, str] = {}
    existing_doc_ids: set[str] = set()
    rows_by_doc: Dict[str, List[AIKnowledgeChunk]] = {}
    for row in existing_rows:
        existing_doc_ids.add(row.doc_id)
        if row.doc_id not in rows_by_doc:
            rows_by_doc[row.doc_id] = []
        rows_by_doc[row.doc_id].append(row)
        for tag in row.tags_list():
            if tag.startswith(_HASH_TAG_PREFIX):
                existing_hash[row.doc_id] = tag[len(_HASH_TAG_PREFIX) :]

    # 整类在向量库被清空（本地库丢失/重置）→ 即便哈希匹配也强制重嵌，自愈
    force = bool(existing_doc_ids) and (await _skill_docs_point_count()) == 0

    desired_doc_ids: set = set()
    changed = 0
    total_files = 0
    for skill, files in skills.items():
        namespace = skill_doc_namespace(skill)
        for f in files:
            total_files += 1
            try:
                text = f.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning(t("log.rag.skillskb_read_document_skipping_fail", skill=skill, p0=f.name, e=e))
                continue
            doc_id = _doc_id_for(skill, f)
            desired_doc_ids.add(doc_id)
            h = _content_hash(text)
            stored = existing_hash[doc_id] if doc_id in existing_hash else ""
            if not force and stored == h:
                continue
            title = f"[{skill}] {_doc_title(text, f.stem)}"
            tags = [namespace, skill, f"{_HASH_TAG_PREFIX}{h}"]
            bodies = document_bodies(
                full_text=text,
                sections=(),
                title=title,
                tags=tags,
                budget=embed_char_budget(),
                overlap=DEFAULT_CHUNK_OVERLAP,
            )
            if not bodies:
                continue
            old_rows = rows_by_doc[doc_id] if doc_id in rows_by_doc else []
            old_bodies = [row.content.strip() for row in sorted(old_rows, key=lambda row: (row.chunk_index, row.id))]
            # 切法编号变了但片文本没变：只改 SQL 里的哈希，向量不用重算。
            if not force and old_bodies == bodies:
                for row in old_rows:
                    kept = [tag for tag in row.tags_list() if not tag.startswith(_HASH_TAG_PREFIX)]
                    kept.append(f"{_HASH_TAG_PREFIX}{h}")
                    row.tags = json.dumps(kept, ensure_ascii=False)
                await AIKnowledgeChunk.upsert_many(old_rows)
                continue
            await add_knowledge_document(
                doc_id=doc_id,
                title=title,
                full_text=text,
                tags=tags,
                plugin=namespace,
                source=SKILLS_DOC_SOURCE,
                replace=True,
            )
            changed += 1

    # 清理已删除/改名的文档（只动本类、确属 skilldoc 前缀的 doc_id）
    stale = {d for d in (existing_doc_ids - desired_doc_ids) if d.startswith(_DOC_ID_PREFIX)}
    for doc_id in stale:
        await delete_knowledge_document(doc_id)

    if changed or stale:
        logger.info(
            t(
                "log.rag.skillskb_skill_changed_total_ok",
                changed=changed,
                p0=len(stale),
                p1=len(skills),
                total_files=total_files,
            )
        )
    else:
        logger.debug(
            t(
                "log.rag.skillskb_skill_documents_date_skip",
                p0=len(skills),
                total_files=total_files,
            )
        )


async def search_skill_doc_chunks(
    query: str,
    skills: Optional[List[str]] = None,
    limit: int = 8,
) -> list:
    """对 skill 文档做混合检索（dense + BM25 RRF），返回 ScoredPoint 列表。

    Args:
        query: 自然语言查询
        skills: 限定到这些 skill（目录名）；``None`` / 空 = 检索全部已挂载 skill。
        limit: 返回片段数
    """
    from gsuid_core.ai_core.rag.knowledge import query_knowledge

    if skills:
        namespaces = [skill_doc_namespace(s) for s in skills]
    else:
        namespaces = [skill_doc_namespace(s) for s in _discover_skill_docs()]
    if not namespaces:
        return []
    return await query_knowledge(query=query, limit=limit, plugin_filter=namespaces)
