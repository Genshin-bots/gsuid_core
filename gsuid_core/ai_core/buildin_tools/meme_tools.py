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


async def _collect_event_image_payloads(ev: Event) -> list[tuple[bytes, str, str]]:
    """提取当前消息与被引用消息中的图片字节，支持 RM ID、URL 与 base64://。"""
    from gsuid_core.utils.image.image_io import sniff_image_mime
    from gsuid_core.ai_core.meme.observer import _extract_image_urls, _materialize_image_source
    from gsuid_core.utils.resource_manager import RM

    payloads: list[tuple[bytes, str, str]] = []
    seen_rm_ids: set[str] = set()

    gid = str(ev.group_id or "")

    async def _append_rm_id(res_id: str) -> None:
        rid = res_id.strip()
        if not rid or rid in seen_rm_ids or not RM.has(rid, group_id=gid):
            return
        seen_rm_ids.add(rid)
        try:
            raw = await RM.get(rid, group_id=gid)
        except ValueError:
            return
        if not isinstance(raw, (bytes, bytearray)):
            return
        blob = bytes(raw)
        mime = sniff_image_mime(blob)
        if mime is not None:
            payloads.append((blob, mime, ""))

    # 1. 优先读取当前消息及引用消息已注册到 RM 的图片 ID
    rm_ids: list[str] = [*ev.reply_image_id_list, *ev.image_id_list]
    if ev.image_id:
        rm_ids.append(ev.image_id)
    for rid in rm_ids:
        await _append_rm_id(rid)
        if len(payloads) >= 3:
            return payloads

    # 2. 若仅带 reply_id 且当前事件未展开引用图，回查 A 轨历史中的引用消息图片
    if ev.reply_id and not ev.reply_image_id_list:
        from gsuid_core.message_history import get_history_manager

        quoted = get_history_manager().find_by_msg_id(ev, str(ev.reply_id))
        if quoted is not None:
            q_meta = quoted.metadata
            if "image_id_list" in q_meta and isinstance(q_meta["image_id_list"], list):
                for item in q_meta["image_id_list"]:
                    if isinstance(item, str):
                        await _append_rm_id(item)
                        if len(payloads) >= 3:
                            return payloads
            if "image_id" in q_meta and isinstance(q_meta["image_id"], str):
                await _append_rm_id(q_meta["image_id"])
                if len(payloads) >= 3:
                    return payloads
            if len(payloads) < 3 and "images" in q_meta and isinstance(q_meta["images"], list):
                for item in q_meta["images"]:
                    if isinstance(item, str):
                        mat = await _materialize_image_source(item)
                        if mat is not None:
                            payloads.append(mat)
                            if len(payloads) >= 3:
                                return payloads

    # 3. 兜底：直接从 ev.content 提取 URL / base64:// 图片（兼容未注册 RM 的事件）
    if not payloads:
        for src in _extract_image_urls(ev)[:3]:
            mat = await _materialize_image_source(src)
            if mat is not None:
                payloads.append(mat)
                if len(payloads) >= 3:
                    return payloads

    return payloads


@ai_tools(category="common", capability_domain="表情", code_callable=False)
async def collect_meme(
    ev: Event,
) -> str:
    """
    读取当前消息或引用消息中的图片并收藏到「群聊采集」表情包

    当用户发送或引用了一张有趣的表情/图片要求收藏时，调用此工具入库。

    Returns:
        收藏结果描述

    Example:
        >>> await collect_meme()
    """
    from gsuid_core.pool import to_thread
    from gsuid_core.ai_core.meme.tagger import enqueue_tag
    from gsuid_core.ai_core.meme.normalize import prepare_meme_image, is_known_outbound_meme
    from gsuid_core.ai_core.meme.pack_model import PackStore

    payloads = await _collect_event_image_payloads(ev)
    if not payloads:
        return "当前消息和引用消息中没有可收藏的图片"

    collected_pack = await PackStore.ensure_collected_pack()
    results: list[str] = []
    for image_data, file_mime, src_url in payloads[:3]:
        prepared = await to_thread(prepare_meme_image)(image_data, file_mime)
        if await to_thread(is_known_outbound_meme)(prepared.data, prepared.width, prepared.height):
            results.append("图片已存在")
            continue
        record = await MemeLibrary.save_prepared(
            prepared,
            source_group=ev.group_id or "",
            source_user=ev.user_id or "",
            source_url=src_url,
            pending_pack_id=collected_pack.pack_id,
            assign_default_pack=False,
            enabled=True,
        )

        if record is not None:
            results.append(f"已收藏至「{collected_pack.name}」: {record.meme_id}")
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
        label = (
            f"{record.title} - {record.description}"
            if record.title and record.description
            else (record.title or record.description or "无描述")
        )
        result_lines.append(f"{i}. [{record.meme_id}] {label} (标签: {tags}, 使用次数: {record.use_count})")

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
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key, self_scope_key
    from gsuid_core.ai_core.cognition.types import CogKind
    from gsuid_core.ai_core.persona.settings import persona_name_from_event
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
            scope_key=scope_key or self_scope_key(ev.bot_id, persona_name_from_event(ev) or ""),
            owner_user_id=ev.user_id,
            title=name,
            summary=gloss[:120],
            source="model",
        )
    )
    return f"已记住「{name}」。下次群友提到会直接懂。"
