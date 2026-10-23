"""表情包 AI 工具注册

注册 collect_meme、search_meme。主人格发表情只走回复里的 ``<meme:情绪>``。

使用 @ai_tools 装饰器注册，Event 和 Bot 参数由装饰器自动注入。
"""

from typing import Sequence

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.meme.config import meme_config
from gsuid_core.ai_core.meme.library import MemeLibrary
from gsuid_core.ai_core.meme.database_model import AiMemeRecord
from gsuid_core.ai_core.buildin_tools.visibility import visible_to_capability_only


@ai_tools(category="common", capability_domain="表情", code_callable=False)
async def collect_meme(
    ev: Event,
    reason: str = "",
) -> str:
    """
    读取当前消息中的图片并写入表情包库

    当用户发送了一张有趣的图片时，AI 可以调用此工具将其收藏。

    Args:
        reason: 收藏原因（可选），用于记录

    Returns:
        收藏结果描述

    Example:
        >>> await collect_meme(reason="这张图很搞笑")
    """
    from gsuid_core.ai_core.meme.observer import _download_image, _extract_image_urls

    image_urls = _extract_image_urls(ev)
    if not image_urls:
        return "当前消息中没有图片"

    results: list[str] = []
    for url in image_urls[:3]:
        download_result = await _download_image(url)
        if download_result is None:
            results.append(f"下载失败: {url}")
            continue

        image_data, file_mime = download_result

        record = await MemeLibrary.save_raw(
            image_data=image_data,
            file_mime=file_mime,
            source_group=ev.group_id or "",
            source_user=ev.user_id or "",
            source_url=url,
        )

        if record is not None:
            results.append(f"已收藏: {record.meme_id}")
            from gsuid_core.ai_core.meme.tagger import enqueue_tag

            await enqueue_tag(record.meme_id)
        else:
            results.append("图片已存在")

    return "; ".join(results)


@ai_tools(category="common", capability_domain="表情", visible_when=visible_to_capability_only)
async def search_meme(
    ev: Event,
    query: str,
) -> str:
    """
    搜索表情包库

    通过语义搜索查找匹配的表情包，返回描述列表。

    Args:
        query: 搜索关键词或描述，如 "搞笑猫咪", "无语表情"

    Returns:
        匹配的表情包描述列表

    Example:
        >>> await search_meme(query="搞笑猫咪")
        >>> await search_meme(query="无语的表情")
    """
    persona_name = _get_persona_for_event(ev)
    from gsuid_core.ai_core.persona.config import persona_allows_meme_reply

    if not persona_allows_meme_reply(persona_name):
        return "这个人格没有打开表情回复。"
    pack_ids: list[str] = []
    if persona_name:
        from gsuid_core.ai_core.meme.pack_model import PackStore

        pack_ids = await PackStore.pack_ids_for_persona(persona_name)
    records: Sequence[AiMemeRecord] = await MemeLibrary.search_by_text(
        query_text=query,
        top_k=5,
        score_threshold=meme_config.get_config("meme_search_threshold").data,
        pack_ids=pack_ids,
    )

    if not records:
        return "未找到匹配的表情包"

    result_lines: list[str] = []
    for i, record in enumerate(records, 1):
        tags = ", ".join(record.all_tags) if record.all_tags else "无标签"
        result_lines.append(
            f"{i}. [{record.meme_id}] {record.description or '无描述'} (标签: {tags}, 使用次数: {record.use_count})"
        )

    return "\n".join(result_lines)


def _get_persona_for_event(ev: Event) -> str | None:
    """从事件中获取当前 persona 名称。解析失败返回 None。"""
    try:
        from gsuid_core.ai_core.persona.config import persona_config_manager

        session_id = ev.session_id
        persona_name = persona_config_manager.get_persona_for_session(session_id)
        if persona_name:
            return persona_name
        return None
    except Exception as e:
        logger.warning(t("log.meme.persona_resolve_fail", e=e))
        return None


@ai_tools(category="self", capability_domain="群聊黑话", code_callable=False)
async def record_meme(
    ev: Event,
    term: str,
    meaning: str,
    origin: str = "",
    usage: str = "",
) -> str:
    """把刚搞懂的梗/黑话登记进词典，下次免搜。

    接梗失败或刚用网页查清一个梗后调用。不要把学术名词当梗登记。

    Args:
        term: 梗名或触发短语。
        meaning: 语义解释（≤300字）。
        origin: 出处（可选）。
        usage: 接法范式（可选）。
    """
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember
    from gsuid_core.ai_core.meme.database_model import AiMemeKnowledge

    name = (term or "").strip()
    gloss = (meaning or "").strip()
    if not name or not gloss:
        return "⚠️ term 和 meaning 都不能空。"
    scope_key = make_scope_key(ScopeType.GROUP, ev.group_id) if ev.group_id else ""
    row = await AiMemeKnowledge.upsert_term(
        bot_id=ev.bot_id,
        term=name,
        meaning=gloss,
        origin=origin,
        usage=usage,
        scope_key=scope_key,
        confidence=0.8,
        source="model",
    )
    await remember(
        MemoryWrite(
            kind=CogKind.MEME_KNOWLEDGE,
            ref=str(row.id),
            scope_key=scope_key or f"self:{ev.bot_id}",
            owner_user_id=ev.user_id,
            title=name,
            summary=gloss[:120],
            source="model",
        )
    )
    return f"已记住「{name}」。下次群友提到会直接懂。"
