"""表情包 VLM 打标引擎

MemeTagEngine 负责后台消费打标队列，调用 AI Agent 理解图片内容，
生成情绪/场景标签，并将结果写入数据库和 Qdrant 向量索引。
使用 create_agent + extract_json_from_text 复用现有基础设施。

图片处理由 GsCoreAIAgent._execute_run 自动完成：
- 模型支持图片时直接传图
- 模型不支持图片时通过 understand_image 转述为文字
"""

import io
import base64
import asyncio
from typing import Optional

from PIL import Image
from pydantic_ai.messages import ImageUrl

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import extract_json_from_text
from gsuid_core.ai_core.gs_agent import create_agent
from gsuid_core.ai_core.meme.config import meme_config
from gsuid_core.ai_core.meme.library import MemeLibrary, _read_file, get_memes_base_path
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

# 打标队列
_tag_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)

# 打标信号量
_tag_semaphore: Optional[asyncio.Semaphore] = None

# 打标 worker 任务引用
_worker_task: Optional[asyncio.Task] = None

# 打标开关关闭时的轮询间隔（秒）：worker / scanner 暂停时以此间隔重新检查开关，
# 越小则网页控制台切换 meme_vlm_enable 后恢复打标越及时
_TAG_PAUSE_POLL_SEC: int = 5


def is_tagging_enabled() -> bool:
    """是否允许执行 VLM 打标

    实时读取配置：总开关 meme_enable 与 VLM 打标开关 meme_vlm_enable 任一关闭即停止。
    配置由网页控制台 set_config 即时写入内存，因此无需重启即可实时启停打标队列。
    """
    if not meme_config.get_config("meme_enable").data:
        return False
    if not meme_config.get_config("meme_vlm_enable").data:
        return False
    return True


# VLM 打标提示词
TAG_PROMPT = """你是一个图片分析助手。请分析这张图片并返回 JSON 格式的标签信息。

请返回以下格式的 JSON（不要包含其他内容）：
{
    "is_meme": true,
    "description": "简短描述图片内容（20字以内）",
    "emotion_tags": ["情绪标签1", "情绪标签2"],
    "scene_tags": ["场景标签1", "场景标签2"],
    "persona_hint": "common",
    "nsfw_score": 0.0
}

字段说明：
- is_meme: 是否适合作为表情包使用。
    判断标准：只要图片具有表达情绪、态度或可用于社交聊天的功能，就应标记为 true。包括但不限于：
  · 带文字/配文的图片、梗图、表情包
  · 二次元/动漫角色的夸张表情或可爱动作（即使没有文字）
  · 动物的搞笑/可爱瞬间
  · 简笔画、涂鸦、卡通形象
  · 具有明显情绪表达的人物表情特写
  · meme 格式的图片（如对比图、反应图等）
  仅当图片为纯粹的风景照、证件照、产品图、截图、普通写真、技术图表等完全不具备表情包属性的内容时才填 false
- description: 图片内容的简短描述
- emotion_tags: 情绪标签列表，如 "开心", "无语", "搞笑", "可爱", "愤怒", "悲伤", "惊讶", "尴尬", "得意", "委屈"
- scene_tags: 场景标签列表，如 "日常", "吐槽", "卖萌", "怼人", "安慰", "庆祝", "晚安", "早安"
- persona_hint: 建议的 persona 归属，如果不确定填 "common"
- nsfw_score: NSFW 分数（0.0~1.0），0 表示完全安全，1 表示完全不安全

只返回 JSON，不要有其他文字。"""


# 定期扫描任务引用
_scanner_task: Optional[asyncio.Task] = None


async def start_tag_worker() -> None:
    """启动后台打标 worker"""
    global _tag_semaphore, _worker_task, _scanner_task

    semaphore_count: int = meme_config.get_config("meme_vlm_semaphore").data
    _tag_semaphore = asyncio.Semaphore(semaphore_count)

    _worker_task = asyncio.create_task(_tag_worker_loop())
    _scanner_task = asyncio.create_task(_pending_scanner_loop())

    # worker 常驻运行并实时轮询 meme_vlm_enable，启动时若该开关关闭则处于暂停态，
    # 待网页控制台开启后自动开始消费队列
    state = "运行中" if is_tagging_enabled() else "已暂停 (meme_vlm_enable 关闭)"
    logger.info(
        t(
            "[Meme] 打标 worker 已启动，并发上限: {semaphore_count}，当前状态: {state}",
            semaphore_count=semaphore_count,
            state=state,
        )
    )


async def stop_tag_worker() -> None:
    """停止后台打标 worker"""
    global _worker_task, _scanner_task
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    if _scanner_task and not _scanner_task.done():
        _scanner_task.cancel()
        try:
            await _scanner_task
        except asyncio.CancelledError:
            pass
    logger.info(t("[Meme] 打标 worker 已停止"))


async def enqueue_tag(meme_id: str) -> None:
    """将 meme_id 加入打标队列

    Args:
        meme_id: 表情包 ID
    """
    # 打标开关关闭时不入队，避免堆满有界队列；重新启用后由 _pending_scanner_loop
    # 扫描 pending 记录补回，不会丢失待打标图片
    if not is_tagging_enabled():
        return
    if _tag_queue.full():
        logger.warning(t("[Meme] 打标队列已满（>{p0}），丢弃: {meme_id}", p0=_tag_queue.maxsize, meme_id=meme_id))
        return
    await _tag_queue.put(meme_id)
    logger.debug(t("[Meme] 加入打标队列: {meme_id}", meme_id=meme_id))


async def _tag_worker_loop() -> None:
    """打标 worker 主循环

    每次循环实时检查打标开关（meme_enable / meme_vlm_enable），任一关闭时暂停消费队列，
    实现通过网页控制台对打标队列的实时启停（无需重启）。
    """
    was_enabled: Optional[bool] = None
    while True:
        try:
            enabled = is_tagging_enabled()
            # 开关状态切换时打印日志，便于在网页控制台实时启停时观察生效情况
            if enabled != was_enabled:
                if enabled:
                    logger.info(t("[Meme] VLM 打标已启用，开始消费打标队列"))
                elif was_enabled is not None:
                    logger.info(t("[Meme] VLM 打标已关闭，暂停打标队列"))
                was_enabled = enabled

            if not enabled:
                await asyncio.sleep(_TAG_PAUSE_POLL_SEC)
                continue

            # 带超时获取，队列为空时也能周期性重新检查开关；关闭开关后最多再处理一条
            # 已在队列中的图片（下一轮循环顶部即暂停），该图片会被正常打标，无需二次确认
            try:
                meme_id = await asyncio.wait_for(_tag_queue.get(), timeout=_TAG_PAUSE_POLL_SEC)
            except asyncio.TimeoutError:
                continue

            if _tag_semaphore is None:
                await asyncio.sleep(1)
                _tag_queue.task_done()
                continue

            async with _tag_semaphore:
                await _tag_single(meme_id)

            _tag_queue.task_done()

            # 打标间隔
            from gsuid_core.ai_core.meme.config import MEME_TAG_INTERVAL_SEC

            await asyncio.sleep(MEME_TAG_INTERVAL_SEC)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(t("[Meme] 打标 worker 异常: {e}", e=e))
            await asyncio.sleep(5)


# 定期扫描间隔（秒）
_PENDING_SCAN_INTERVAL_SEC = 300  # 5分钟


async def _pending_scanner_loop() -> None:
    """定期扫描未打标的 pending 图片并加入队列

    启动后先等待一会儿让系统完全启动，然后每五分钟扫描一次
    inbox 和其他文件夹中状态为 pending 的记录，加入打标队列。
    """
    from gsuid_core.ai_core.meme.database_model import AiMemeRecord

    # 启动后先等待30秒，让系统完全启动
    await asyncio.sleep(30)

    while True:
        try:
            # 实时开关检查：关闭时不扫描、不入队，短间隔轮询以便重新启用后快速恢复打标
            if not is_tagging_enabled():
                await asyncio.sleep(_TAG_PAUSE_POLL_SEC)
                continue

            # 扫描 pending 状态的记录
            pending_records = await AiMemeRecord.get_pending_records(limit=50)
            if pending_records:
                for record in pending_records:
                    await enqueue_tag(record.meme_id)
                logger.info(t("[Meme] 定期扫描: 将 {p0} 条 pending 记录加入打标队列", p0=len(pending_records)))

            await asyncio.sleep(_PENDING_SCAN_INTERVAL_SEC)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(t("[Meme] pending 扫描异常: {e}", e=e))
            await asyncio.sleep(60)


@to_thread
def _extract_gif_second_frame_sync(image_data: bytes) -> Optional[bytes]:
    """从GIF中提取第二帧（同步版本）

    大多数VLM模型不支持GIF格式，如果是GIF则提取第二帧（索引1）
    进行识别。如果GIF只有一帧或提取失败，返回None。

    Args:
        image_data: GIF文件二进制数据

    Returns:
        第二帧的PNG格式数据，失败返回None
    """
    try:
        img = Image.open(io.BytesIO(image_data))
        if img.format != "GIF":
            return None
        # GIF帧数
        n_frames: int = getattr(img, "n_frames", 1)

        if n_frames <= 1:
            return None

        # 跳到第二帧（索引1）
        img.seek(1)

        # 转换为RGB（去掉透明通道）并保存为PNG
        # P 模式先转 RGBA：直接拿调色板索引通道当 mask 会导致怪色
        if img.mode == "P":
            img = img.convert("RGBA")
        if img.mode == "RGBA":
            rgb_img = Image.new("RGB", img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[-1])
            img = rgb_img
        elif img.mode != "RGB":
            img = img.convert("RGB")

        output = io.BytesIO()
        img.save(output, format="PNG")
        return output.getvalue()
    except Exception as e:
        logger.warning(t("[Meme] GIF第二帧提取失败: {e}", e=e))
        return None


async def _tag_single(meme_id: str) -> None:
    """对单个表情包进行 VLM 打标

    图片处理由 GsCoreAIAgent._execute_run 自动完成：
    - 模型支持图片时直接传图
    - 模型不支持图片时通过 understand_image 转述为文字
    - GIF格式会提取第二帧进行识别（大多数VLM不支持GIF）

    Args:
        meme_id: 表情包 ID
    """
    record = await AiMemeRecord.get_by_meme_id(meme_id)
    if record is None:
        logger.warning(t("[Meme] 打标时找不到记录: {meme_id}", meme_id=meme_id))
        return

    # 检查状态，避免重复打标
    if record.status not in ("pending", "pending_manual"):
        return

    # 读取图片文件
    file_path = get_memes_base_path() / record.file_path
    if not file_path.exists():
        logger.warning(t("[Meme] 图片文件不存在: {file_path}", file_path=file_path))
        await MemeLibrary.mark_tag_failed(meme_id)
        return

    image_data = await _read_file(file_path)

    # GIF格式处理：提取第二帧
    is_gif = record.file_mime == "image/gif"
    if is_gif:
        gif_frame_data = await _extract_gif_second_frame_sync(image_data)
        if gif_frame_data is not None:
            image_data = gif_frame_data
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            # GIF提取后转为PNG格式
            effective_mime = "image/png"
        else:
            # 提取失败，使用原图（某些VLM可能支持GIF）
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            effective_mime = record.file_mime
    else:
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        effective_mime = record.file_mime

    # 调用 Agent 进行打标（直接传 ImageUrl，由 _execute_run 自动处理图片能力判断）
    tag_result = await _call_tag_agent(image_b64, effective_mime)
    if tag_result is None:
        logger.warning(t("[Meme] VLM 打标失败: {meme_id}", meme_id=meme_id))
        await MemeLibrary.mark_tag_failed(meme_id)
        return

    # NSFW 检查：rejected 前也写入标签，方便后期人工审核
    nsfw_threshold: float = meme_config.get_config("meme_nsfw_threshold").data
    if tag_result["nsfw_score"] >= nsfw_threshold:
        logger.info(t("[Meme] NSFW 分数过高，标记为 rejected: {meme_id}", meme_id=meme_id))
        await MemeLibrary.update_tags(
            meme_id=meme_id,
            description=tag_result["description"],
            emotion_tags=tag_result["emotion_tags"],
            scene_tags=tag_result["scene_tags"],
            persona_hint=tag_result["persona_hint"],
        )
        await MemeLibrary.mark_rejected(meme_id, tag_result["nsfw_score"])
        return

    # 非表情包检查：rejected 前也写入标签，方便后期人工审核
    if not tag_result["is_meme"]:
        logger.info(t("[Meme] 不是表情包，标记为 rejected: {meme_id}", meme_id=meme_id))
        await MemeLibrary.update_tags(
            meme_id=meme_id,
            description=tag_result["description"],
            emotion_tags=tag_result["emotion_tags"],
            scene_tags=tag_result["scene_tags"],
            persona_hint=tag_result["persona_hint"],
        )
        await MemeLibrary.mark_rejected(meme_id, 0.0)
        return

    # 更新标签
    persona_hint = tag_result["persona_hint"]

    # 确定目标文件夹：仅 inbox 的图由 VLM persona_hint 决定归属；
    # 已人工放置的图保留现有文件夹，避免打标静默覆盖用户的目录选择
    if record.folder == "inbox":
        target_folder = "common"
        if persona_hint and persona_hint != "common":
            target_folder = f"persona_{persona_hint}"
    else:
        target_folder = record.folder
        if record.persona_hint:
            persona_hint = record.persona_hint

    # 先移动文件，成功后再置 tagged，避免出现"已 tagged 但滞留 inbox"
    # 的不可检索状态（folder=inbox 的点任何 persona/common 过滤都搜不到）
    if record.folder != target_folder:
        moved = await MemeLibrary.move_file(meme_id, target_folder)
        if not moved:
            await MemeLibrary.update_tags(
                meme_id=meme_id,
                description=tag_result["description"],
                emotion_tags=tag_result["emotion_tags"],
                scene_tags=tag_result["scene_tags"],
                persona_hint=persona_hint,
            )
            await MemeLibrary.mark_tag_failed(meme_id)
            logger.warning(t("[Meme] 打标完成但移动文件失败，置为待人工处理: {meme_id}", meme_id=meme_id))
            return

    await MemeLibrary.update_tags(
        meme_id=meme_id,
        description=tag_result["description"],
        emotion_tags=tag_result["emotion_tags"],
        scene_tags=tag_result["scene_tags"],
        persona_hint=persona_hint,
        status="tagged",
    )

    # 同步到 Qdrant
    record = await AiMemeRecord.get_by_meme_id(meme_id)
    if record is not None:
        await MemeLibrary.sync_to_qdrant(record)

    logger.info(t("[Meme] 打标完成: {meme_id} -> {target_folder}", meme_id=meme_id, target_folder=target_folder))


async def _call_tag_agent(
    image_b64: str,
    file_mime: str,
) -> Optional[dict]:
    """通过 Agent 进行图片打标

    使用 create_agent 创建临时 Agent，传入 ImageUrl。
    GsCoreAIAgent._execute_run 会自动根据模型能力决定：
    - 模型支持图片：直接传图给 LLM
    - 模型不支持图片：调用 understand_image 转述为文字

    Args:
        image_b64: Base64 编码的图片数据
        file_mime: 图片 MIME 类型

    Returns:
        解析后的标签字典，失败返回 None
    """

    # 创建临时 Agent（无工具，纯文本+图片分析）
    agent = create_agent(
        system_prompt=TAG_PROMPT,
        max_tokens=21000,
        max_iterations=1,
        create_by="MemeTagger",
        task_level="low",
    )

    # 构建包含图片的用户消息（ImageUrl 会被 _execute_run 自动处理）
    img_url = f"data:{file_mime};base64,{image_b64}"
    user_message = [ImageUrl(url=img_url)]

    result = await agent.run(
        user_message=user_message,
        return_mode="return",
    )

    if not result:
        logger.warning(t("[Meme] Agent 返回空结果"))
        return None

    # 使用 extract_json_from_text 解析
    parsed = extract_json_from_text(result)
    if not isinstance(parsed, dict):
        logger.warning(t("[Meme] 解析结果不是 dict"))
        return None

    # 确保字段类型正确
    return {
        "is_meme": bool(parsed.get("is_meme", True)),
        "description": str(parsed.get("description", "")),
        "emotion_tags": list(parsed.get("emotion_tags", [])),
        "scene_tags": list(parsed.get("scene_tags", [])),
        "persona_hint": str(parsed.get("persona_hint", "common")),
        "nsfw_score": float(parsed.get("nsfw_score", 0.0)),
    }
