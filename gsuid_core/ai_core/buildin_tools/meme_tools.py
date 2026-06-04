"""表情包 AI 工具注册

注册 send_meme、collect_meme、search_meme 三个 AI 工具，
供主 Agent 在群聊中自主决策发送表情包。

使用 @ai_tools 装饰器注册，Event 和 Bot 参数由装饰器自动注入。
"""

from typing import Sequence

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.meme.config import meme_config
from gsuid_core.ai_core.meme.library import MemeLibrary, _read_file, get_memes_base_path
from gsuid_core.ai_core.meme.selector import pick
from gsuid_core.ai_core.meme.database_model import AiMemeRecord


@ai_tools(category="common", capability_domain="表情")
async def send_meme(
    ev: Event,
    bot: Bot,
    mood: str,
    scene: str = "",
) -> str:
    """
    发送一张表情包到当前群聊

    根据情绪和场景描述，从表情包库中选择最合适的图片发送。
    会自动排除最近已发送的图片，并遵守冷却时间限制。

    Args:
        mood: 当前情绪或心情，如 "开心", "无语", "搞笑", "可爱", "愤怒"
        scene: 场景描述（可选），如 "吐槽", "安慰", "庆祝", "怼人"

    Returns:
        发送结果描述

    Example:
        >>> await send_meme(mood="开心", scene="庆祝")
        >>> await send_meme(mood="无语")
    """
    if not meme_config.get_config("meme_enable").data:
        return "表情包模块未启用"

    session_id = ev.session_id
    persona_name = _get_persona_for_event(ev)

    # 选择表情包
    record = await pick(
        mood=mood,
        scene=scene,
        persona=persona_name,
        session_id=session_id,
    )

    if record is None:
        return "未找到合适的表情包"

    # 发送图片
    file_path = get_memes_base_path() / record.file_path
    if not file_path.exists():
        logger.warning(f"[Meme] 表情包文件不存在: {file_path}")
        return "表情包文件不存在"

    image_data = await _read_file(file_path)

    from gsuid_core.utils.image.convert import convert_img

    img_base64 = await convert_img(image_data)
    message = MessageSegment.image(img_base64)

    await bot.send(message)

    # 记录使用
    await AiMemeRecord.record_usage(record.meme_id, ev.group_id or "")

    logger.info(f"[Meme] 发送表情包: {record.meme_id} (mood={mood}, scene={scene}, persona={persona_name})")
    return f"已发送表情包: {record.description or record.meme_id}"


@ai_tools(category="common", capability_domain="表情")
async def collect_meme(
    ev: Event,
    reason: str = "",
) -> str:
    """
    主动收藏当前消息中的图片到表情包库

    当用户发送了一张有趣的图片时，AI 可以调用此工具将其收藏。

    Args:
        reason: 收藏原因（可选），用于记录

    Returns:
        收藏结果描述

    Example:
        >>> await collect_meme(reason="这张图很搞笑")
    """
    if not meme_config.get_config("meme_enable").data:
        return "表情包模块未启用"

    from gsuid_core.ai_core.meme.observer import (
        _download_image,
        _extract_image_urls,
        _get_image_dimensions,
    )

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
        width, height = await _get_image_dimensions(image_data)

        record = await MemeLibrary.save_raw(
            image_data=image_data,
            file_mime=file_mime,
            width=width,
            height=height,
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


@ai_tools(category="common", capability_domain="表情")
async def search_meme(
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
    if not meme_config.get_config("meme_enable").data:
        return "表情包模块未启用"

    records: Sequence[AiMemeRecord] = await MemeLibrary.search_by_text(
        query_text=query,
        top_k=5,
        score_threshold=meme_config.get_config("meme_search_threshold").data,
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


def _get_persona_for_event(ev: Event) -> str:
    """从事件中获取当前 persona 名称"""
    try:
        from gsuid_core.ai_core.persona.config import persona_config_manager

        session_id = ev.session_id
        persona_name = persona_config_manager.get_persona_for_session(session_id)
        return persona_name or "common"
    except Exception:
        return "common"
