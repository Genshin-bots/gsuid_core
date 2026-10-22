"""表情包 AI 工具注册

注册 send_meme、collect_meme、search_meme 三个 AI 工具，
供主 Agent 在群聊中自主决策发送表情包。

使用 @ai_tools 装饰器注册，Event 和 Bot 参数由装饰器自动注入。
"""

from typing import Sequence

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.meme.config import meme_config
from gsuid_core.ai_core.meme.library import MemeLibrary, _read_file, get_memes_base_path
from gsuid_core.ai_core.meme.selector import (
    PICK_NO_BIND,
    PICK_COOLDOWN,
    PICK_EXHAUSTED,
    PICK_NO_PERSONA,
    pick,
)
from gsuid_core.ai_core.meme.database_model import AiMemeRecord
from gsuid_core.ai_core.buildin_tools.visibility import visible_to_capability_only


@ai_tools(category="self", code_callable=False)
async def send_meme(
    ev: Event,
    bot: Bot,
    mood: str,
    scene: str = "",
) -> str:
    """
    从当前人格已挂载的表情包里发一张。
    mood 只能是：开心、无语、搞笑、可爱、愤怒、悲伤、惊讶、尴尬、得意、委屈、
    难过、生气、害羞、疑惑、害怕、嫌弃、发呆、微笑、大笑、大哭、暴怒、饿、
    好的、不要、谢谢、抱歉、加油、挥手、比心、不知道、点头。
    scene 可空，只能是：日常、吐槽、卖萌、怼人、安慰、庆祝、晚安、早安。
    按这句话的意思选词，不要用文件名。

    会排除最近已发送的图片，并遵守冷却时间。

    Args:
        mood: 情绪词，必须来自上面的情绪词表
        scene: 场景词，必须来自上面的场景词表，可以空着

    Returns:
        发送结果描述

    Example:
        >>> await send_meme(mood="开心", scene="庆祝")
        >>> await send_meme(mood="无语")
    """
    session_id = ev.session_id
    persona_name = _get_persona_for_event(ev)
    from gsuid_core.ai_core.persona.config import persona_allows_meme_reply

    if not persona_allows_meme_reply(persona_name):
        return "这个人格没有打开表情回复，不要再发。"

    # 选择表情包
    record, reason = await pick(
        mood=mood,
        scene=scene,
        persona=persona_name,
        session_id=session_id,
    )

    if record is None:
        # 区分"现在不能发"和"库里没有"，避免 LLM 误判后无意义重试或向用户致歉
        if reason == PICK_COOLDOWN:
            return "表情包发送冷却中（库中可能有匹配的图），本次跳过即可，不要重试"
        if reason == PICK_EXHAUSTED:
            return "匹配的表情包最近都已发送过，本次跳过以避免重复，不要重试"
        if reason == PICK_NO_PERSONA:
            return "没有解析到当前人格，这次不发表情，不要改发别的图。"
        if reason == PICK_NO_BIND:
            return "这个人格没有挂载表情包，不要改去 common 目录，也不要改发其他人格的图。"
        return "已挂载的包里没有匹配的表情，不要改发别的图，不要重试。"

    # 发送图片
    file_path = get_memes_base_path() / record.file_path
    if not file_path.exists():
        logger.warning(t("log.ai.meme_file_path", file_path=file_path))
        return "表情包文件不存在"

    image_data = await _read_file(file_path)

    from gsuid_core.ai_core.utils import pause_before_meme
    from gsuid_core.utils.image.convert import convert_img

    img_base64 = await convert_img(image_data)
    message = MessageSegment.image(img_base64)

    from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

    await pause_before_meme()
    await bot.send(message, extra_metadata=turn_reply_metadata(ev))

    # 记录使用
    await AiMemeRecord.record_usage(record.meme_id, ev.group_id or "")

    logger.info(
        t(
            "log.ai.meme_mood_scene_persona",
            p0=record.meme_id,
            mood=mood,
            scene=scene,
            persona_name=persona_name,
        )
    )
    return f"已发送表情包: {record.description or record.meme_id}"


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
