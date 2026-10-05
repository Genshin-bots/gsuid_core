"""认知检索工具：主人格唯一「回想」动词 + 图片检索。

``search_cognition`` 并行覆盖记忆 / 偏好 / 知识库 / 落盘 / 产物 / 近窗 /
记录 / 图片 / 表情。知识只回章节目录，深读走 ``read_handle``。
"""

import re
from typing import Dict, Optional, FrozenSet
from dataclasses import dataclass

from pydantic_ai import RunContext
from qdrant_client.http.models.models import ScoredPoint

from gsuid_core.ai_core.rag import search_images
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.cognition import (
    CogKind,
    CogScope,
    kinds_from_names,
    search_cognition as federated_search,
    resolve_recall_kinds,
    strip_speaker_from_query,
)
from gsuid_core.ai_core.cognition.types import CognitiveHit
from gsuid_core.ai_core.cognition.facade import render_cognition_block
from gsuid_core.ai_core.buildin_tools.visibility import (
    visible_to_capability_only,
)

# 片段多而重复，一页两条够用。其它记忆/落盘再留两条。知识走目录，不占这两条。
EPISODE_PAGE = 2
OTHER_PAGE = 2
KNOWLEDGE_CATALOG = 8
PREVIEW_LINES = 3
PREVIEW_LINE_CHARS = 80
POOL_LIMIT = 24
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)(?:（续\d+）)?\s*$")
_CHUNK_SEG_RE = re.compile(r" - 第\d+段$")

# 本轮已检索过的 query（run 级，存 ToolContext.extra；ToolContext 每轮新建，轮末自然丢弃）
_SEEN_QUERIES_KEY = "cognition.seen_queries"
# run 内的空结果标记：旧写法用「无命中」作值，那是检索术语，会被模型照抄讲给群里。
_NO_MATERIAL = "本轮没有可用材料"
_POOL_KEY = "cognition.hit_pools"
_NO_MORE = "没有更多高命中"


@dataclass(frozen=True)
class _CatalogRow:
    title: str
    handle: str
    chunk_index: int
    preview: tuple[str, ...]


def chapter_title(title: str, summary: str) -> str:
    """片首 Markdown 标题优先；否则去掉「- 第N段」。"""
    first = ""
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    matched = _HEADING_RE.match(first)
    if matched:
        return matched.group(1).strip() or title or "知识"
    base = _CHUNK_SEG_RE.sub("", title).strip()
    return base or title or "知识"


def preview_lines(summary: str) -> tuple[str, ...]:
    """标题以下最多三行，每行再截断。预览只用来认章节，不代替正文。"""
    lines: list[str] = []
    skipped_heading = False
    for raw in summary.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if not skipped_heading and stripped.startswith("#"):
            skipped_heading = True
            continue
        lines.append(stripped[:PREVIEW_LINE_CHARS])
        if len(lines) >= PREVIEW_LINES:
            break
    if lines:
        return tuple(lines)
    flat = summary.replace("\n", " ").strip()
    if not flat or flat.startswith("#"):
        return ()
    return (flat[:PREVIEW_LINE_CHARS],)


def knowledge_catalog(hits: list[CognitiveHit]) -> list[_CatalogRow]:
    """按融合顺序取不重复的章节。同一句柄、同一标题只留最先的一条。"""
    pool = [hit for hit in hits if hit.kind is CogKind.KNOWLEDGE]
    strong = [hit for hit in pool if hit.high_confidence]
    source = strong if strong else pool
    seen: set[tuple[str, str]] = set()
    rows: list[_CatalogRow] = []
    for hit in source:
        title = chapter_title(hit.title, hit.summary)
        key = (hit.handle or hit.id, title)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            _CatalogRow(
                title=title,
                handle=hit.handle,
                chunk_index=hit.chunk_index,
                preview=preview_lines(hit.summary),
            )
        )
        if len(rows) >= KNOWLEDGE_CATALOG:
            break
    return rows


def format_knowledge_catalog(rows: list[_CatalogRow], offsets: dict[tuple[str, int], int]) -> str:
    """目录在栅栏外，预览在栅栏内。read_handle 的 offset 已经是命中片的起点。"""
    if not rows:
        return ""
    from gsuid_core.ai_core.content_guard import wrap_untrusted

    lines = ["【知识目录】选一节再读全文。预览只用来确认章节，不要凭预览作答。"]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {row.title}")
        if row.preview:
            lines.append(wrap_untrusted("knowledge", "\n".join(row.preview)))
        if not row.handle:
            continue
        if row.handle.startswith("kb_kbdoc:") and row.chunk_index > 0:
            off = offsets[(row.handle, row.chunk_index)] if (row.handle, row.chunk_index) in offsets else 0
            lines.append(f"read_handle(handle_id={row.handle!r}, offset={off})")
        else:
            lines.append(f"read_handle(handle_id={row.handle!r})")
    return "\n".join(lines)


def _line_source(hits: list[CognitiveHit]) -> list[CognitiveHit]:
    pool = [hit for hit in hits if hit.kind is not CogKind.KNOWLEDGE]
    strong = [hit for hit in pool if hit.high_confidence]
    return strong if strong else pool


def line_pages(hits: list[CognitiveHit]) -> list[list[CognitiveHit]]:
    """每页最多两条片段，再加最多两条其它非知识命中。"""
    source = _line_source(hits)
    episodes = [hit for hit in source if hit.kind is CogKind.EPISODE]
    others = [hit for hit in source if hit.kind is not CogKind.EPISODE]
    pages: list[list[CognitiveHit]] = []
    episode_at = 0
    other_at = 0
    while episode_at < len(episodes) or other_at < len(others):
        page = episodes[episode_at : episode_at + EPISODE_PAGE]
        episode_at += EPISODE_PAGE
        page.extend(others[other_at : other_at + OTHER_PAGE])
        other_at += OTHER_PAGE
        if page:
            pages.append(page)
    return pages


def page_at(pages: list[list[CognitiveHit]], offset: int) -> tuple[list[CognitiveHit], int | None]:
    """offset 是已展示过的非知识条数。返回本页，以及下一页的 offset。"""
    start = offset if offset > 0 else 0
    seen = 0
    total = sum(len(page) for page in pages)
    for page in pages:
        if seen >= start:
            nxt = seen + len(page)
            return page, nxt if nxt < total else None
        seen += len(page)
    return [], None


async def _render_catalog(rows: list[_CatalogRow]) -> str:
    from gsuid_core.ai_core.planning.handle_resolver import kbdoc_char_offsets

    offsets: dict[tuple[str, int], int] = {}
    docs: set[str] = set()
    for row in rows:
        if row.handle.startswith("kb_kbdoc:") and row.chunk_index > 0:
            docs.add(row.handle[len("kb_kbdoc:") :])
    for doc_id in docs:
        for index, off in (await kbdoc_char_offsets(doc_id)).items():
            offsets[(f"kb_kbdoc:{doc_id}", index)] = off
    return format_knowledge_catalog(rows, offsets)


def _seen_queries(ctx: RunContext[ToolContext]) -> Dict[str, str]:
    extra = ctx.deps.extra
    if _SEEN_QUERIES_KEY not in extra or not isinstance(extra[_SEEN_QUERIES_KEY], dict):
        extra[_SEEN_QUERIES_KEY] = {}
    return extra[_SEEN_QUERIES_KEY]


def _keep_trigger_surface(surface: str) -> bool:
    """单字和两字母缩写当触发词会把问句掏空。"""
    text = surface.strip()
    if not text:
        return False
    if text.isascii():
        return len(text) >= 3
    return len(text) >= 2


def trigger_surfaces(persona_name: str) -> tuple[str, ...]:
    """当前人格名，加上该人格配置里的唤醒词。没建过配置的名字不落盘。"""
    names: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        text = raw.strip()
        if not _keep_trigger_surface(text) or text in seen:
            return
        seen.add(text)
        names.append(text)

    add(persona_name)
    from gsuid_core.ai_core.persona.config import persona_config_manager

    if not persona_config_manager.exists(persona_name):
        return tuple(names)
    raw = persona_config_manager.get_config(persona_name).get_config("keywords").data
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                add(item)
    return tuple(names)


def strip_trigger_words(query: str, persona_name: str | None) -> str:
    """检索前拿掉人格名和唤醒词。同名实体否则会占满目录。整句被拿光时保留原问句。"""
    if not persona_name or not query:
        return query
    from gsuid_core.ai_core.entity_index import strip_surfaces

    stripped = strip_surfaces(query, trigger_surfaces(persona_name))
    return stripped or query


def _append_alias_canonicals(query: str) -> str:
    """句内已登记别名展开成正式名。整句中文没有空格，不能只按词块查。"""
    from gsuid_core.ai_core.entity_index import find_entities_in_text

    extras: list[str] = []
    seen: set[str] = set()
    for ref in find_entities_in_text(query):
        if ref.is_ambiguous or not ref.canonicals:
            continue
        canon = ref.canonicals[0]
        if not canon or canon in query or canon in seen:
            continue
        seen.add(canon)
        extras.append(canon)
    if not extras:
        return query
    return f"{query} {' '.join(extras)}"


def _query_key(query: str, kinds: FrozenSet[CogKind]) -> str:
    """归一化后的 query + kinds 切片作为去重键（空白与大小写差异不算新 query）。"""
    normalized = "".join(query.split()).lower()
    return f"{normalized}|{','.join(sorted(k.value for k in kinds))}"


def _page_key(query: str, kinds: FrozenSet[CogKind], offset: int) -> str:
    """同一问法的不同页不是同一次调用。同一页重调才短路。"""
    start = offset if offset > 0 else 0
    return f"{_query_key(query, kinds)}@{start}"


def _cached_pool(extra: object, pool_key: str) -> list[CognitiveHit] | None:
    if not isinstance(extra, dict) or _POOL_KEY not in extra:
        return None
    raw = extra[_POOL_KEY]
    if not isinstance(raw, dict) or pool_key not in raw:
        return None
    rows = raw[pool_key]
    if not isinstance(rows, list):
        return None
    hits: list[CognitiveHit] = []
    for row in rows:
        if not isinstance(row, CognitiveHit):
            return None
        hits.append(row)
    return hits


def _store_pool(extra: object, pool_key: str, hits: list[CognitiveHit]) -> None:
    if not isinstance(extra, dict):
        return
    raw = extra[_POOL_KEY] if _POOL_KEY in extra else None
    bucket: Dict[str, list[CognitiveHit]] = {}
    if isinstance(raw, dict):
        for key, rows in raw.items():
            if isinstance(key, str) and isinstance(rows, list) and all(isinstance(row, CognitiveHit) for row in rows):
                typed: list[CognitiveHit] = []
                ok = True
                for row in rows:
                    if not isinstance(row, CognitiveHit):
                        ok = False
                        break
                    typed.append(row)
                if ok:
                    bucket[key] = typed
    bucket[pool_key] = hits
    extra[_POOL_KEY] = bucket


def _scope_from_ctx(ctx: RunContext[ToolContext], include_skill_doc: bool = False) -> CogScope:
    """从工具上下文构造检索 scope。

    **私聊 group_id 必须是 None**：回退成 user_id 只会去查一个空的幻影
    ``group:{user_id}``，召回恒为 0。这条口径必须与 handle_ai 主链路一致。
    """
    from gsuid_core.bot import Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.memory.config import memory_config
    from gsuid_core.ai_core.turn_pipeline import parse_clock_at

    ev = ctx.deps.ev
    bot = ctx.deps.bot
    self_id = ""
    if isinstance(bot, Bot):
        self_id = str(bot.bot_self_id)
    elif isinstance(ev, Event):
        self_id = str(ev.bot_self_id)
    extra = ctx.deps.extra
    clock = None
    if "turn_clock" in extra and isinstance(extra["turn_clock"], str):
        clock = parse_clock_at(extra["turn_clock"])
    return CogScope(
        user_id=str(ev.user_id) if ev is not None and ev.user_id else "",
        bot_id=bot.bot_id if bot is not None else "",
        bot_self_id=self_id,
        group_id=str(ev.group_id) if ev is not None and ev.group_id else None,
        include_skill_doc=include_skill_doc,
        # 语义性开关在唯一的配置层给默认值，不在函数签名里给
        enable_system2=memory_config.enable_system2,
        enable_user_global=memory_config.enable_user_global_memory,
        clock_at=clock,
    )


@ai_tools(
    category="buildin",
    capability_domain="回想",
)
async def search_cognition(
    ctx: RunContext[ToolContext],
    query: str,
    kinds: Optional[str] = None,
    limit: int = 24,
    offset: int = 0,
) -> str:
    """回想**我已经知道的事**：长期记忆、用户偏好、知识库、以前查过的材料、任务产物。

    **不查实时 / 外部数据**：网页与专域实时信息一律用 `web_search_tool` /
    `web_fetch_tool` / 专域数据工具。本工具查不到外面的东西。

    涉及公共概念时，回执会带**路径卡**（挂在上面的文章目录 + 本环境事实）。
    问到某一栏且能唯一选定时，同一次返回该篇全文（≤6000 字，超出用 read_handle）。
    插件/手动文只读；要补充请用 `attach_article` 新建一篇，不要改只读正文。

    什么时候用：
    - 用户问到过去的事（"上周/上次/之前我们聊过…""你说过的那个…"），当前上下文没答案时；
    - 需要"已有材料"（专业知识、说明文档、稳定资料、以前搜过的长文）时；
    - 想确认"我对某人了解多少 / 有没有答应过什么"时；
    - 问已有记忆：query 带上问题里的专名/数字/约束；
    - 问已入库的专名时，片段再多也先看路径卡；路径卡在就不改走 web_search。
    - 办眼前的事需要说话人身上的事实、当前消息和上文都没写：query 写「说话人ID + 要填的槽」，
      不要把本次外部题目的词拼进去；填槽后再 web_search / 专域工具。

    一页最多 2 条对话片段，再加最多 2 条其它记忆或落盘。知识库不贴正文，只给最多
    8 节目录（章节标题、三行预览、read_handle）。要依据资料作答就调用目录里的
    read_handle，不要凭预览下结论，也不要加大 limit 或再搜一次来翻页。
    下一批片段把 offset 设为回执里的数；知识目录只在第一页。
    同一页重调结果相同；**换槽位词**（专名/日期/清单项）再搜会取到不同片段。
    这次没有结果，换个问法也不会有别的：外部实时事实另找联网来源，某个领域的专用
    能力另找对应工具——两者都不要向用户解释。取到的片段只证明谁在该时点说过；
    日期、数量、状态不是当前事实。问现在如何而没有本轮其它工具结果时，只转述原话。

    Args:
        ctx: 工具执行上下文
        query: 自然语言查询。问已有记忆时带上专名与约束；办眼前的事填槽时
            写「说话人ID + 要填的槽」，不要把外部题目的词拼进去。
        kinds: 可选，逗号分隔的类型过滤。留空=记忆+知识+落盘；
            query 含当前说话人 ID 时查 episode/entity/fact/preference（不含近窗/知识库）。
            图片/表情/出站/业务记录须显式打开。
        limit: 不决定页宽。它只决定候选池深度——给得越大池子越宽（上限 48）。
        offset: 跳过前若干条非知识命中。下一批用上一页回执里的 offset。

    Returns:
        路径卡（仅第一页）+ 知识目录（仅第一页）+ 最多 2 条片段和 2 条其它命中。
        没有结果时只回一行。
    """
    scope = _scope_from_ctx(ctx)
    if not scope.user_id:
        return "⚠️ 无用户上下文，拒绝检索（防跨用户泄漏）。"
    selected = kinds_from_names(set(kinds.split(","))) if kinds else frozenset()
    selected = resolve_recall_kinds(selected, query=query, user_id=scope.user_id)

    # 同一页重搜必然同结果。不挡的话主会话会把同一页连打到熔断。
    seen = _seen_queries(ctx)
    start = offset if offset > 0 else 0
    key = _page_key(query, selected, start)
    if key in seen:
        prev = seen[key]
        same = f"结果同上：{prev}" if prev != _NO_MATERIAL else "仍无可用材料"
        return (
            f"（本轮已拿这个问法问过，{same}。"
            "换个问法不会变；要外部实时事实走联网来源，要专域信息走对应能力，"
            "或者直接据已有信息作答。）"
        )

    search_q = strip_speaker_from_query(query, scope.user_id)
    from gsuid_core.ai_core.memory.retrieval.lexical import strip_clock_lines

    search_q = strip_clock_lines(search_q) or search_q
    persona_name = ""
    if "persona_name" in ctx.deps.extra and isinstance(ctx.deps.extra["persona_name"], str):
        persona_name = ctx.deps.extra["persona_name"]
    search_q = strip_trigger_words(search_q, persona_name or None)
    search_q = _append_alias_canonicals(search_q)

    pool_key = _query_key(search_q, selected)
    hits = _cached_pool(ctx.deps.extra, pool_key)
    if hits is None:
        # limit 不再决定页宽。池子只为后面的 offset 留高命中，不整页交出去。
        lim = POOL_LIMIT if limit <= POOL_LIMIT else min(limit, 48)
        hits = await federated_search(
            search_q,
            kinds=selected,
            scope=scope,
            limit=lim,
        )
        _store_pool(ctx.deps.extra, pool_key, hits)
    page, next_offset = page_at(line_pages(hits), start)
    from gsuid_core.ai_core.cognition.hub import expand_hub, render_expand_result

    # 路径卡和知识目录只跟第一页。后面的页再挂全文，主会话会被同一篇占满。
    rows = knowledge_catalog(hits) if start == 0 and CogKind.KNOWLEDGE in selected else []
    if start == 0 and CogKind.KNOWLEDGE in selected:
        expansion = await expand_hub(search_q, hits, scope=scope)
    else:
        expansion = None
    from gsuid_core.ai_core.content_guard import wrap_untrusted

    trusted = [h for h in page if h.kind is CogKind.OUTBOUND]
    others = [h for h in page if h.kind is not CogKind.OUTBOUND]
    trusted_block = render_cognition_block(query, trusted, header="出站（可信）") if trusted else ""
    hits_block = render_cognition_block(query, others, hint_query=search_q, coverage_note=True)
    card = render_expand_result(query, expansion) if expansion is not None else ""
    catalog = await _render_catalog(rows)
    if not page and not card and not catalog:
        seen[key] = _NO_MATERIAL if start == 0 else _NO_MORE
    elif card and page:
        seen[key] = f"找到 {len(page)} 条，含路径卡"
    elif card:
        seen[key] = "路径卡"
    else:
        extra = f"，知识目录 {len(rows)} 节" if rows else ""
        seen[key] = f"找到 {len(page)} 条{extra}"
    if start > 0 and not page and not card:
        return f"（{_NO_MORE}。换槽位词再搜，或据已有片段作答。）"
    parts: list[str] = []
    if card:
        parts.append(card)
    if trusted_block:
        parts.append(trusted_block)
    if others:
        parts.append(wrap_untrusted("memory_recall", hits_block))
    elif not trusted and not card and not catalog:
        parts.append(hits_block)
    if catalog:
        parts.append(catalog)
    if next_offset is not None:
        parts.append(f"（还有没展开的片段。下一批把 offset 设为 {next_offset}。）")
    return "\n\n".join(parts) if parts else hits_block


@ai_tools(category="common", visible_when=visible_to_capability_only)
async def search_image(
    ctx: RunContext[ToolContext],
    query: str,
    plugin: Optional[str] = None,
    limit: int = 5,
    score_threshold: float = 0.45,
) -> str:
    """
    检索图片资源

    根据用户查询的自然语言描述，从向量数据库中检索匹配的图片。
    支持语义相似度匹配和按插件过滤，返回匹配的图片路径和相关信息。
    当用户需要查找或发送特定图片时使用此工具。

    Args:
        ctx: 工具执行上下文
        query: 自然语言查询描述，如「主题图片」或「场景图」
        plugin: 可选，限定插件来源
        limit: 最大返回结果数量，默认5条
        score_threshold: 相似度分数阈值，低于此值的结果会被过滤，默认0.45

    Returns:
        匹配的图片信息列表字符串，包含图片路径、标签、描述和匹配分数
    """
    plugin_filter = [plugin] if plugin else None

    results: list[ScoredPoint] = await search_images(
        query=query,
        limit=limit,
        plugin_filter=plugin_filter,
    )

    image_list = []
    for point in results:
        payload = point.payload
        if payload is not None and point.score >= score_threshold:
            image_info = {
                "id": payload["id"] if "id" in payload else None,
                "path": payload["path"] if "path" in payload else None,
                "tags": payload["tags"] if "tags" in payload else [],
                "content": payload["content"] if "content" in payload else "",
                "plugin": payload["plugin"] if "plugin" in payload else None,
                "score": point.score,
            }
            image_list.append(image_info)

    if not image_list:
        return "未找到匹配的图片资源。"

    return str(image_list)
