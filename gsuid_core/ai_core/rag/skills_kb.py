"""docs/skills 开发文档 → 知识库挂载（启动期）+ 命名空间检索（通用）。

把 ``docs/skills/<skill>/`` 下的全部 SKILL 文档（``references/*.md``，无 references 时退回
``SKILL.md``）在框架启动时挂载进知识库，供能力代理（如 ``plugin_developer_agent``）用
**混合检索（dense + BM25 稀疏 RRF）**按需查阅——取代在单文件里做子串标题匹配的脆弱方式。

本模块**发现并挂载 docs/skills 下的每一个 skill**（如 ``gscore-plugin-development`` /
``gscore-ai-core-api`` / ``gscore-adapter-development``），新增 skill 目录无需改代码、自动纳入。

## 隔离设计（关键）

所有分片统一写 ``source="skill_doc"``，每个 skill 各自占一个命名空间 ``plugin="skilldoc:<skill>"``：

- 与插件知识（``source="plugin"``）/ 手动知识（``source="manual"``）的同步、对账互不干扰
  （``sync_knowledge`` 只清 plugin 来源、``reconcile_manual_knowledge`` 只管 manual 来源）。
- 通用 ``search_knowledge`` 工具与意图分类器按 ``exclude_sources=["skill_doc"]`` 把**整类**开发
  文档挡在日常聊天 RAG 之外（一处排除覆盖全部 skill、且对将来新增 skill 自动生效），避免污染。
- 能力代理用 ``search_skill_docs`` 工具按 ``plugin="skilldoc:<skill>"`` 命名空间过滤检索；
  不限定 skill 时检索全部已挂载 skill。

## 幂等

按每篇文件内容哈希（含分片策略版本）跳过未变化文档，避免每次启动重复嵌入数百分片；当
``skill_doc`` 这一类在向量库被清空（本地库丢失 / 重置）时强制重嵌自愈。维度迁移由
``init_knowledge_collection`` 的全量 payload 备份重嵌统一覆盖（它 scroll 全量点、含本类）。
"""

import hashlib
from typing import Dict, List, Optional
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.rag.chunking import split_text

# 全部 skill 开发文档共用的来源标记：聊天侧据此一处排除整类。
SKILLS_DOC_SOURCE: str = "skill_doc"

# 每个 skill 的命名空间前缀（写入 payload 的 plugin 字段）：skilldoc:<skill 目录名>。
_SKILL_NS_PREFIX: str = "skilldoc:"
# doc_id 形如 skilldoc::<skill>::<文件名 stem>；内容哈希写进分片 tags 做幂等判定。
_DOC_ID_PREFIX: str = "skilldoc::"
_HASH_TAG_PREFIX: str = "_srchash:"

# 分片策略版本：折进内容哈希——改了切分方式（即便文件没变）也会让旧入库失配、自动重切重嵌。
_CHUNKER_VERSION: str = "md-v1"

# 按 Markdown 小节（H2）切分；超过软上限的小节再**保代码块完整地**细切，避免单片过长触发 dense
# 静默截断（本地 bge-small-zh 仅 512 token 上限）。软上限略放宽以尽量保住"一小节一片"的完整度。
_SECTION_SOFT_CAP: int = 1200
_SUB_TARGET: int = 1000
_SUB_HARD_CAP: int = 2200
_SUB_OVERLAP: int = 120

# docs/skills 根目录（相对仓库根；parents[3] 即仓库根，与 buildin_tools 同深度）。
_SKILLS_ROOT: Path = Path(__file__).resolve().parents[3] / "docs" / "skills"


def skill_doc_namespace(skill: str) -> str:
    """skill 目录名 → 知识库 plugin 命名空间值。"""
    return f"{_SKILL_NS_PREFIX}{skill}"


def _discover_skill_docs() -> Dict[str, List[Path]]:
    """发现 docs/skills 下每个 skill 及其文档文件。

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
    # 折进分片版本：切分策略变更即视为内容变更，触发重切重嵌。
    payload = f"{_CHUNKER_VERSION}\x00{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ───────────────────────── Markdown 小节级分片（代码块感知） ─────────────────────────


def _iter_h2_sections(text: str) -> List[str]:
    """按 H2（``## ``）把 markdown 切成"小节"块（每块含各自标题）；**代码围栏内的 ``#`` 不算标题**。

    H1 标题与首个 H2 之前的引言归入第一块。H3+ 不作为切分边界——让一个小节（含其子小节）
    保持完整，符合"按小节切"的语义。
    """
    lines = text.splitlines()
    in_fence = False
    boundaries: List[int] = [0]
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if i != 0 and stripped.startswith("## "):  # "### " 不以 "## "（带尾空格）开头，天然排除
            boundaries.append(i)
    boundaries.append(len(lines))

    sections: List[str] = []
    for a, b in zip(boundaries, boundaries[1:]):
        block = "\n".join(lines[a:b]).strip()
        if block:
            sections.append(block)
    return sections


def _section_heading(block: str) -> str:
    """块首行若是 Markdown 标题则返回之，否则空串。"""
    if not block:
        return ""
    first = block.splitlines()[0].strip()
    return first if first.startswith("#") else ""


def _atomic_segments(body: str) -> List[str]:
    """把小节正文切成"原子块"：每个**完整代码围栏**是一块，围栏之间的散文按空行分段成块。"""
    lines = body.splitlines()
    segments: List[str] = []
    buf: List[str] = []
    in_fence = False

    def _flush_prose() -> None:
        text = "\n".join(buf).strip("\n")
        for para in text.split("\n\n"):
            if para.strip():
                segments.append(para.strip("\n"))
        buf.clear()

    for line in lines:
        if line.lstrip().startswith("```"):
            if not in_fence:
                _flush_prose()
                buf.append(line)
                in_fence = True
            else:
                buf.append(line)
                segments.append("\n".join(buf))  # 完整代码块整块产出
                buf.clear()
                in_fence = False
            continue
        buf.append(line)
    if in_fence:  # 容错：围栏未闭合，整块产出
        segments.append("\n".join(buf))
    else:
        _flush_prose()
    return [s for s in segments if s.strip()]


def _pack_segments(segments: List[str]) -> List[str]:
    """把原子块按目标片长贪心打包成片：单块超过硬上限才硬切（极少数超长块），否则整块保留。"""
    pieces: List[str] = []
    cur = ""

    def _flush() -> None:
        nonlocal cur
        if cur.strip():
            pieces.append(cur.strip("\n"))
        cur = ""

    for seg in segments:
        if len(seg) > _SUB_HARD_CAP:
            _flush()
            pieces.extend(split_text(seg, _SUB_TARGET, _SUB_OVERLAP))
            continue
        if cur and len(cur) + len(seg) + 1 > _SUB_TARGET:
            _flush()
        cur = f"{cur}\n{seg}" if cur else seg
    _flush()
    return [p for p in pieces if p.strip()]


def _markdown_chunks(text: str) -> List[str]:
    """把一篇文档切成"按小节"的分片：小节整片入库；超长小节再**保代码块完整地**细切，
    每个子片重新前置该小节标题以保持自描述（独立检索召回时仍知道自己属于哪一小节）。"""
    chunks: List[str] = []
    for block in _iter_h2_sections(text):
        if len(block) <= _SECTION_SOFT_CAP:
            chunks.append(block)
            continue
        heading = _section_heading(block)
        body = block[len(heading) :].lstrip("\n") if heading else block
        pieces = _pack_segments(_atomic_segments(body))
        for idx, piece in enumerate(pieces):
            if not heading:
                chunks.append(piece)
            elif idx == 0:
                chunks.append(f"{heading}\n{piece}")
            else:
                chunks.append(f"{heading}（续{idx + 1}）\n{piece}")
    return [c for c in chunks if c.strip()]


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
        logger.debug(t("🧠 [SkillsKB] 统计 skill_doc 点数失败（按非空处理）: {e}", e=e))
        return -1


async def sync_skill_docs() -> None:
    """启动期把 docs/skills 下全部 skill 文档挂载进知识库（幂等）。供 ``rag.startup.init_all`` 调用。"""
    from gsuid_core.ai_core.rag.base import client, embedding_model
    from gsuid_core.ai_core.rag.knowledge import (
        add_knowledge_document,
        delete_knowledge_document,
    )
    from gsuid_core.ai_core.database.models import AIKnowledgeChunk

    skills = _discover_skill_docs()
    if not skills:
        logger.warning(t("🧠 [SkillsKB] 未发现任何 skill 文档，跳过挂载: {_SKILLS_ROOT}", _SKILLS_ROOT=_SKILLS_ROOT))
        return
    if client is None or embedding_model is None:
        logger.debug(t("🧠 [SkillsKB] RAG 未就绪，跳过 skill 文档挂载"))
        return

    # 现存 skill_doc 分片：doc_id -> 已存内容哈希（取自分片 tags 里的 _srchash:）
    existing_rows = await AIKnowledgeChunk.iter_all(source=SKILLS_DOC_SOURCE)
    existing_hash: Dict[str, str] = {}
    existing_doc_ids: set = set()
    for row in existing_rows:
        existing_doc_ids.add(row.doc_id)
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
                logger.warning(t("🧠 [SkillsKB] 读取文档失败，跳过: {skill}/{p0}: {e}", skill=skill, p0=f.name, e=e))
                continue
            doc_id = _doc_id_for(skill, f)
            desired_doc_ids.add(doc_id)
            h = _content_hash(text)
            if not force and existing_hash.get(doc_id) == h:
                continue  # 未变化 → 跳过重嵌
            sections = _markdown_chunks(text)
            if not sections:
                continue
            title = _doc_title(text, f.stem)
            await add_knowledge_document(
                doc_id=doc_id,
                title=f"[{skill}] {title}",
                items=[{"content": c} for c in sections],
                tags=[namespace, skill, f"{_HASH_TAG_PREFIX}{h}"],
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
                "🧠 [SkillsKB] skill 文档挂载完成：更新 {changed} 篇、清理 {p0} 篇"
                "（{p1} 个 skill / 共 {total_files} 篇文档）",
                changed=changed,
                p0=len(stale),
                p1=len(skills),
                total_files=total_files,
            )
        )
    else:
        logger.debug(
            t(
                "🧠 [SkillsKB] skill 文档已是最新（{p0} 个 skill / {total_files} 篇），跳过重嵌",
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
