"""表情包 VLM 结构化打标引擎

支持三种模式：
- ``auto``：后台自动打标，受 ``meme_vlm_enable`` 开关控制；
- ``retag_overwrite``：控制台单张重新识别，``force_run=True`` 免开关阻塞，存量在线热覆盖；
- ``complete_missing``：控制台批量识别待补全，``force_run=True``，已入库记录保留手填非空字段。
"""

from __future__ import annotations

import re
import base64
import asyncio
from typing import Literal, Optional
from dataclasses import dataclass

from pydantic_ai.messages import ImageUrl

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import extract_json_from_text
from gsuid_core.ai_core.gs_agent import create_agent
from gsuid_core.ai_core.meme.config import meme_config
from gsuid_core.ai_core.meme.library import MemeLibrary, _read_file, get_memes_base_path
from gsuid_core.ai_core.configs.task_kinds import TASK_MEME_TAGGER
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

TagMode = Literal["auto", "retag_overwrite", "complete_missing"]


@dataclass(frozen=True, slots=True)
class TagJob:
    """单条 VLM 打标队列任务。"""

    meme_id: str
    mode: TagMode = "auto"
    force_run: bool = False


@dataclass(frozen=True, slots=True)
class MemeTagResult:
    """VLM 结构化打标结果（7 项元数据 + 表情判定 + NSFW 分数）。"""

    is_meme: bool
    title: str
    description: str
    ocr_text: str
    custom_tags: list[str]
    emotion_tags: list[str]
    usage_hint: str
    nsfw_score: float


# 打标队列（容量 1000，满足整库批量待补全入队）与在队去重集合
_tag_queue: asyncio.Queue[TagJob] = asyncio.Queue(maxsize=1000)
_queued_jobs: set[tuple[str, TagMode]] = set()

# 打标信号量
_tag_semaphore: Optional[asyncio.Semaphore] = None

# 打标 worker 与扫描任务引用
_worker_task: Optional[asyncio.Task[None]] = None
_scanner_task: Optional[asyncio.Task[None]] = None

_TAG_PAUSE_POLL_SEC: int = 5
_PENDING_SCAN_INTERVAL_SEC: int = 300

# 旧版无情绪信息量的泛词，避免污染细粒度情绪标签
_BANNED_GENERIC_EMOTIONS: frozenset[str] = frozenset({"日常", "表情包", "表情", "通用", "普通"})
_NEGATIVE_USAGE_SPLIT_RE = re.compile(
    r"[。；;，,]\s*(?:不适合|不宜|不要用于|请勿用于|切勿用于|避免用于|不建议用于|勿用于)"
    r"|(?:不适合|不宜|不要用于|请勿用于|避免用于|不建议用于)"
)


def is_tagging_enabled() -> bool:
    """是否允许后台自动执行 VLM 打标（控制台主动识别走 force_run=True 不受此限制）。"""
    return bool(meme_config.get_config("meme_vlm_enable").data)


TAG_PROMPT = """你是一个聊天表情包检索标注专家。
你的标注将直接用于群聊机器人根据 `<meme:情绪或态度短语>` 语义检索这张表情。请仔细观察图片并返回严格 JSON。

请仅返回以下格式的 JSON（不要包含任何额外说明或 Markdown 围栏以外的文字）：
{
    "is_meme": true,
    "title": "2到10字口语化短名称",
    "description": "30到80字神态与动作描写（禁写外貌衣着背景）",
    "ocr_text": "图中可见文字原文，无文字留空字符串",
    "custom_tags": ["口语动作或反应词1", "触发词2", "触发词3"],
    "emotion_tags": ["群聊高频情绪或态度词1", "词2"],
    "usage_hint": "适合用于聊天中……的正向适用场景与口语反应（30到80字）",
    "nsfw_score": 0.0
}

字段规范：
1. is_meme: 是否适合作为聊天表情包使用。只要具备情绪/态度表达、玩梗、反应或社交互动功能就填 true；
   仅纯风景照、证件照、商品图、大段代码/文档截图、技术图表等完全无表情包属性时填 false。
2. title: 2–10 字口语化且具辨识度的名称（如「斜眼嫌弃」「气鼓鼓叉腰」「事已至此先吃饭吧」「尴尬冒冷汗」）。
   有醒目配字时优先提炼配字梗；无字时直白概括神态与态度，避免书面文艺词。
3. description: 30–80 字聚焦面部神情、眼神方向、肢体动作、表情符号特效与玩梗点。
   **严禁描写**角色性别、画风、发色发型、瞳色、耳饰衣领服饰或背景颜色（同一人格包内角色外貌相同，外貌词会严重稀释语义检索）。
4. ocr_text: 按正常阅读顺序忠实转录图中全部可见文字；图中无文字则填 ""，严禁臆造文字。
5. custom_tags: 3–6 个**群聊口语触发词、动作姿态或梗词**
   （如 ["斜眼看", "静静看着你", "冷眼旁观", "捂脸", "双手抱胸"]）；
   **严禁填写**外貌与构图标签（禁止出现「动漫女性」「侧面近景」「高马尾」「红衣」「蓝眼」等）。
6. emotion_tags: 2–5 个**群聊高频口语情绪/态度词**
   （如 ["嫌弃", "无语", "冷漠", "怀疑", "看戏"] 或 ["尴尬", "心虚", "社死"]）；
   优先使用自然口语词而非书面文艺词（如用「冷漠/嫌弃/无语」代替「疏离感/克制专注」），严禁「日常」「表情包」「通用」等泛词。
7. usage_hint: 30–80 字正向适用语境，写清**对方说了什么或什么群聊场面下适合发这张图**，并自然包含口语同义说法
   （例如「适合用于对方说离谱话、尬吹或自爆时，表达冷眼旁观、嫌弃、无语或静静看着你表演」）。
   **严禁**写「不适合用于……」「不要用于……」「避免用于……」等任何否定或排除句。
8. nsfw_score: 0.0–1.0 的 NSFW 风险分，0.0 表示完全安全，1.0 表示明确违规。

只返回 JSON，不要输出其他文字。"""


def _strip_negative_usage_clauses(raw: str) -> str:
    """裁掉模型违规附带的「不适合用于…」否定从句，防止负向词污染向量索引。"""
    text = raw.strip()
    if not text:
        return ""
    match = _NEGATIVE_USAGE_SPLIT_RE.search(text)
    if match is None:
        return text[:512]
    positive = text[: match.start()].strip().rstrip("，,；;、")
    return positive[:512]


def _clean_str(mapping: dict[str, object], key: str, max_len: int) -> str:
    if key not in mapping:
        return ""
    val = mapping[key]
    if not isinstance(val, str):
        return ""
    return val.strip()[:max_len]


def _clean_str_list(mapping: dict[str, object], key: str, *, limit: int = 8) -> list[str]:
    if key not in mapping:
        return []
    raw = mapping[key]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        word = item.strip()
        if not word or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= limit:
            break
    return out


def _clean_bool(mapping: dict[str, object], key: str, default: bool) -> bool:
    if key not in mapping:
        return default
    raw = mapping[key]
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return default


def _clean_float(mapping: dict[str, object], key: str, default: float) -> float:
    if key not in mapping:
        return default
    raw = mapping[key]
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(0.0, min(1.0, float(raw)))
    if isinstance(raw, str):
        try:
            return max(0.0, min(1.0, float(raw.strip())))
        except ValueError:
            return default
    return default


def parse_tag_result(parsed: object) -> Optional[MemeTagResult]:
    """把 VLM 返回的 JSON 对象收窄为强类型 MemeTagResult。"""
    if not isinstance(parsed, dict):
        return None
    mapping: dict[str, object] = {str(k): v for k, v in parsed.items()}
    raw_emotions = _clean_str_list(mapping, "emotion_tags", limit=6)
    filtered_emotions = [w for w in raw_emotions if w not in _BANNED_GENERIC_EMOTIONS]
    emotion_tags = filtered_emotions if filtered_emotions else raw_emotions
    return MemeTagResult(
        is_meme=_clean_bool(mapping, "is_meme", True),
        title=_clean_str(mapping, "title", 64),
        description=_clean_str(mapping, "description", 512),
        ocr_text=_clean_str(mapping, "ocr_text", 256),
        custom_tags=_clean_str_list(mapping, "custom_tags", limit=8),
        emotion_tags=emotion_tags[:5],
        usage_hint=_strip_negative_usage_clauses(_clean_str(mapping, "usage_hint", 512)),
        nsfw_score=_clean_float(mapping, "nsfw_score", 0.0),
    )


def _task_supports_image(task_level: Literal["high", "low"], task_kind: str | None) -> bool:
    from gsuid_core.ai_core.configs.models import (
        get_config_name_for_task,
        get_model_config_for_task,
    )

    if not get_config_name_for_task(task_level, task_kind):
        return False
    raw: object = get_model_config_for_task(task_level, task_kind).get_config("model_support").data
    if isinstance(raw, list):
        return any(str(item) == "image" for item in raw)
    if isinstance(raw, str):
        return "image" in raw
    return False


def _resolve_tag_task_level() -> Literal["high", "low"]:
    """未设 meme_tagger 专项覆盖且 low 档无视觉能力时，自动回退具备 image 能力的 high 档。"""
    from gsuid_core.ai_core.configs.models import resolve_task_route

    route = resolve_task_route("low", TASK_MEME_TAGGER)
    if route.source != "default":
        return "low"
    if not _task_supports_image("low", TASK_MEME_TAGGER) and _task_supports_image("high", None):
        return "high"
    return "low"


def _ensure_semaphore() -> asyncio.Semaphore:
    global _tag_semaphore
    if _tag_semaphore is None:
        raw_count = meme_config.get_config("meme_vlm_semaphore").data
        count = int(raw_count) if isinstance(raw_count, int) and raw_count > 0 else 1
        _tag_semaphore = asyncio.Semaphore(count)
    return _tag_semaphore


async def start_tag_worker() -> None:
    """启动后台打标 worker 与 pending 定时扫描（幂等）。"""
    global _tag_semaphore, _worker_task, _scanner_task

    raw_count = meme_config.get_config("meme_vlm_semaphore").data
    semaphore_count = int(raw_count) if isinstance(raw_count, int) and raw_count > 0 else 1
    _tag_semaphore = asyncio.Semaphore(semaphore_count)

    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_tag_worker_loop())
    if _scanner_task is None or _scanner_task.done():
        _scanner_task = asyncio.create_task(_pending_scanner_loop())

    state = "running" if is_tagging_enabled() else "standby"
    logger.info(
        t(
            "log.meme.tagging_worker_concurrency_limit",
            semaphore_count=semaphore_count,
            state=state,
        )
    )


async def stop_tag_worker() -> None:
    """停止后台打标 worker。"""
    global _worker_task, _scanner_task
    if _worker_task is not None:
        if not _worker_task.done():
            _worker_task.cancel()
            try:
                await _worker_task
            except asyncio.CancelledError:
                pass
        _worker_task = None
    if _scanner_task is not None:
        if not _scanner_task.done():
            _scanner_task.cancel()
            try:
                await _scanner_task
            except asyncio.CancelledError:
                pass
        _scanner_task = None
    _queued_jobs.clear()
    logger.info(t("log.meme.meme_worker_stop_tagging_stopped"))


async def enqueue_tag(
    meme_id: str,
    *,
    mode: TagMode = "auto",
    force_run: bool = False,
) -> bool:
    """将打标任务加入队列。控制台主动触发传 force_run=True，不受 meme_vlm_enable 阻塞。"""
    if not force_run and not is_tagging_enabled():
        return False
    job_key = (meme_id, mode)
    if job_key in _queued_jobs:
        return True
    try:
        _tag_queue.put_nowait(TagJob(meme_id=meme_id, mode=mode, force_run=force_run))
    except asyncio.QueueFull:
        logger.warning(t("log.meme.tagging_queue_full_dropping", p0=_tag_queue.maxsize, meme_id=meme_id))
        return False
    _queued_jobs.add(job_key)
    logger.debug(t("log.meme.added_tagging_queue", meme_id=meme_id))
    return True


async def _tag_worker_loop() -> None:
    """打标 worker 主循环：自动任务受开关控制，控制台主动任务（force_run）始终立即消费。"""
    was_enabled: Optional[bool] = None
    while True:
        try:
            enabled = is_tagging_enabled()
            if enabled != was_enabled:
                if enabled:
                    logger.info(t("log.meme.vlm_tagging_enabled_consume"))
                elif was_enabled is not None:
                    logger.info(t("log.meme.vlm_tagging_disabled_pausing"))
                was_enabled = enabled

            try:
                job = await asyncio.wait_for(_tag_queue.get(), timeout=_TAG_PAUSE_POLL_SEC)
            except asyncio.TimeoutError:
                continue

            job_key = (job.meme_id, job.mode)
            try:
                if not job.force_run and not is_tagging_enabled():
                    continue
                sem = _ensure_semaphore()
                async with sem:
                    await _tag_single(job)
            finally:
                _queued_jobs.discard(job_key)
                _tag_queue.task_done()

            from gsuid_core.ai_core.meme.config import MEME_TAG_INTERVAL_SEC

            await asyncio.sleep(MEME_TAG_INTERVAL_SEC)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(t("log.meme.meme_worker_exception_tagging", e=e))
            await asyncio.sleep(5)


async def _pending_scanner_loop() -> None:
    """定期扫描未打标的 pending 图片并加入队列（仅在 meme_vlm_enable=True 时扫描）。"""
    await asyncio.sleep(30)

    while True:
        try:
            if not is_tagging_enabled():
                await asyncio.sleep(_TAG_PAUSE_POLL_SEC)
                continue

            pending_records = await AiMemeRecord.get_pending_records(limit=50)
            if pending_records:
                for record in pending_records:
                    await enqueue_tag(record.meme_id, mode="auto", force_run=False)
                logger.info(t("log.meme.periodic_scan_adding_pending", p0=len(pending_records)))

            await asyncio.sleep(_PENDING_SCAN_INTERVAL_SEC)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(t("log.meme.meme_pending_exception_scan", e=e))
            await asyncio.sleep(60)


def _merge_complete_missing(
    record: AiMemeRecord,
    tag: MemeTagResult,
) -> tuple[str, str, str, list[str], list[str], str]:
    """已入库记录走待补全时保护手填非空字段，仅补全空缺与过短旧描述。"""
    title = record.title if record.title.strip() else tag.title
    ocr_text = record.ocr_text if record.ocr_text.strip() else tag.ocr_text
    usage_hint = record.usage_hint if record.usage_hint.strip() else tag.usage_hint
    custom_tags = list(record.custom_tags) if record.custom_tags else list(tag.custom_tags)
    description = (
        tag.description if len(record.description.strip()) < 25 and tag.description.strip() else record.description
    )
    if record.emotion_tags:
        merged_emotions = list(dict.fromkeys([*record.emotion_tags, *tag.emotion_tags]))[:8]
    else:
        merged_emotions = list(tag.emotion_tags)
    return title, description, ocr_text, custom_tags, merged_emotions, usage_hint


async def _tag_single(job_or_id: TagJob | str) -> None:
    """对单张表情执行 VLM 结构化打标，按模式完成全量写入、在线热覆盖或待补全。"""
    job = job_or_id if isinstance(job_or_id, TagJob) else TagJob(meme_id=job_or_id)
    meme_id = job.meme_id

    record = await AiMemeRecord.get_by_meme_id(meme_id)
    if record is None:
        logger.warning(t("log.meme.record_found_tagging", meme_id=meme_id))
        return

    if job.mode == "auto" and record.status not in ("pending", "pending_manual"):
        return

    file_path = get_memes_base_path() / record.file_path
    if not file_path.is_file():
        await MemeLibrary.delete_meme(meme_id)
        return

    image_data = await _read_file(file_path)
    image_b64 = base64.b64encode(image_data).decode("utf-8")
    effective_mime = record.file_mime or "image/jpeg"

    tag_result = await _call_tag_agent(image_b64, effective_mime)
    if tag_result is None:
        logger.warning(t("log.meme.vlm_tagging_id", meme_id=meme_id))
        if record.status in ("pending", "pending_manual"):
            await MemeLibrary.mark_tag_failed(meme_id)
        return

    nsfw_threshold = float(meme_config.get_config("meme_nsfw_threshold").data)
    already_online = record.status in ("tagged", "manual")

    # 已在线记录由管理员主动触发重识别/补全，刻意跳过 VLM 自动 NSFW 拒收以防误清人工复核资产
    if job.mode == "complete_missing" and already_online:
        title, description, ocr_text, custom_tags, emotion_tags, usage_hint = _merge_complete_missing(
            record,
            tag_result,
        )
        await MemeLibrary.update_tags(
            meme_id=meme_id,
            title=title,
            description=description,
            ocr_text=ocr_text,
            custom_tags=custom_tags,
            emotion_tags=emotion_tags,
            usage_hint=usage_hint,
        )
        updated = await AiMemeRecord.get_by_meme_id(meme_id)
        if updated is not None:
            await MemeLibrary.sync_to_qdrant(updated)
        logger.info(t("log.meme.tagging_id_target", meme_id=meme_id, target_folder=record.folder))
        return

    if job.mode == "retag_overwrite" and already_online:
        await MemeLibrary.update_tags(
            meme_id=meme_id,
            title=tag_result.title,
            description=tag_result.description,
            ocr_text=tag_result.ocr_text,
            custom_tags=tag_result.custom_tags,
            emotion_tags=tag_result.emotion_tags,
            usage_hint=tag_result.usage_hint,
            status="tagged",
        )
        updated = await AiMemeRecord.get_by_meme_id(meme_id)
        if updated is not None:
            await MemeLibrary.sync_to_qdrant(updated)
        logger.info(t("log.meme.tagging_id_target", meme_id=meme_id, target_folder=record.folder))
        return

    # 尚未入库或非在线态（pending / pending_manual / rejected）
    if tag_result.nsfw_score >= nsfw_threshold:
        logger.info(t("log.meme.nsfw_score_high_marked", meme_id=meme_id))
        await MemeLibrary.update_tags(
            meme_id=meme_id,
            title=tag_result.title,
            description=tag_result.description,
            ocr_text=tag_result.ocr_text,
            custom_tags=tag_result.custom_tags,
            emotion_tags=tag_result.emotion_tags,
            usage_hint=tag_result.usage_hint,
        )
        await MemeLibrary.mark_rejected(meme_id, tag_result.nsfw_score)
        return

    should_check_is_meme = job.mode == "auto" or (
        job.mode == "complete_missing" and record.folder == "inbox" and record.source_group != "manual"
    )
    if should_check_is_meme and not tag_result.is_meme:
        logger.info(t("log.meme.marked_rejected_id_meme", meme_id=meme_id))
        await MemeLibrary.update_tags(
            meme_id=meme_id,
            title=tag_result.title,
            description=tag_result.description,
            ocr_text=tag_result.ocr_text,
            custom_tags=tag_result.custom_tags,
            emotion_tags=tag_result.emotion_tags,
            usage_hint=tag_result.usage_hint,
        )
        await MemeLibrary.mark_rejected(meme_id, 0.0)
        return

    target_folder = record.folder
    if record.folder in ("inbox", "rejected"):
        target_folder = "common"
        moved = await MemeLibrary.move_file(meme_id, "common", rewrite_persona_hint=False)
        if not moved:
            await MemeLibrary.update_tags(
                meme_id=meme_id,
                title=tag_result.title,
                description=tag_result.description,
                ocr_text=tag_result.ocr_text,
                custom_tags=tag_result.custom_tags,
                emotion_tags=tag_result.emotion_tags,
                usage_hint=tag_result.usage_hint,
            )
            await MemeLibrary.mark_tag_failed(meme_id)
            logger.warning(t("log.meme.tagging_file_move_set", meme_id=meme_id))
            return

    if job.mode == "complete_missing":
        final_title, final_desc, final_ocr, final_custom, final_emotions, final_usage = _merge_complete_missing(
            record,
            tag_result,
        )
    else:
        final_title = tag_result.title
        final_desc = tag_result.description
        final_ocr = tag_result.ocr_text
        final_custom = tag_result.custom_tags
        final_emotions = tag_result.emotion_tags
        final_usage = tag_result.usage_hint

    await MemeLibrary.update_tags(
        meme_id=meme_id,
        title=final_title,
        description=final_desc,
        ocr_text=final_ocr,
        custom_tags=final_custom,
        emotion_tags=final_emotions,
        usage_hint=final_usage,
        status="tagged",
    )
    from gsuid_core.ai_core.meme.pack_model import PackStore

    await PackStore.apply_successful_tag(meme_id)

    refreshed = await AiMemeRecord.get_by_meme_id(meme_id)
    if refreshed is not None:
        await MemeLibrary.sync_to_qdrant(refreshed)

    logger.info(t("log.meme.tagging_id_target", meme_id=meme_id, target_folder=target_folder))


async def _call_tag_agent(
    image_b64: str,
    file_mime: str,
) -> Optional[MemeTagResult]:
    """通过临时 Agent 执行 VLM 打标，优先选择原生支持视觉的模型档位。"""
    task_level = _resolve_tag_task_level()
    task_kind = TASK_MEME_TAGGER if task_level == "low" else None
    agent = create_agent(
        system_prompt=TAG_PROMPT,
        max_tokens=21000,
        create_by="MemeTagger",
        task_kind=task_kind,
        task_level=task_level,
    )

    img_url = f"data:{file_mime};base64,{image_b64}"
    result = await agent.run(
        user_message=[ImageUrl(url=img_url)],
        return_mode="return",
    )

    if not result:
        logger.warning(t("log.meme.meme_agent_returned_empty_result"))
        return None

    parsed = extract_json_from_text(result)
    tag_result = parse_tag_result(parsed)
    if tag_result is None:
        logger.warning(t("log.meme.meme_dict_parsed_result_is_not"))
        return None
    return tag_result
