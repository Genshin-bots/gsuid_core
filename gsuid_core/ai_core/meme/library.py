"""表情包存储层

MemeLibrary 负责文件系统操作和数据库操作的封装。
文件存储在 data/ai_core/memes/ 下，按文件夹分类。
"""

import re
import shutil
import asyncio
import hashlib
from typing import Any, Dict, List, Tuple, Optional, Sequence
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.path_safety import PathEscapeError, safe_join
from gsuid_core.ai_core.rag.qmodels import (
    Filter,
    Vector,
    Condition,
    SparseVector,
    VectorParams,
    PayloadSchemaType,
    SparseVectorParams,
)
from gsuid_core.ai_core.meme.normalize import (
    PreparedMeme,
    prepare_meme_image,
    evict_outbound_meme_cache,
)
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

_LEXICAL_STOPWORDS: frozenset[str] = frozenset(
    {"表情", "表情包", "图片", "一个", "一下", "有点", "就是", "这个", "那个"}
)
_GENERIC_LEGACY_TAGS: frozenset[str] = frozenset({"日常", "卖萌", "可爱"})
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ALNUM_RUN_RE = re.compile(r"[a-z0-9]{2,}")
_COMPACT_STRIP_RE = re.compile(r"[\s，,。！？!?、；;：:\"'`~·•…（）()\[\]【】<>《》\-—_]+")


@dataclass(frozen=True, slots=True)
class ScoredMeme:
    """表情检索综合打分项。"""

    record: AiMemeRecord
    score: float
    dense_score: float
    lexical_score: float


def build_meme_index_text(record: AiMemeRecord) -> str:
    """构造正向结构化向量索引文本；已有标题与适用场景时限制画面长度，防冗长外貌稀释语义。"""
    parts: list[str] = []
    if record.title:
        parts.append(f"名称：{record.title}")
    if record.ocr_text:
        parts.append(f"文字：{record.ocr_text}")
    if record.emotion_tags:
        parts.append(f"情绪：{'、'.join(record.emotion_tags)}")
    if record.usage_hint:
        parts.append(f"适用回应：{record.usage_hint}")
    if record.custom_tags:
        parts.append(f"标签：{'、'.join(record.custom_tags)}")
    if record.description:
        desc_text = record.description[:80] if (record.title and record.usage_hint) else record.description
        parts.append(f"画面：{desc_text}")
    return "\n".join(parts).strip()


def _compact_norm(text: str) -> str:
    return _COMPACT_STRIP_RE.sub("", text.lower())


def extract_lexical_terms(query: str) -> tuple[str, list[str]]:
    """提取 >=2 字中文词块/二元词与英文数字词，严格剔除单字噪声与停用词。"""
    low = query.strip().lower()
    compact = _compact_norm(low)
    terms: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if len(tok) < 2 or tok in _LEXICAL_STOPWORDS or tok in seen:
            return
        seen.add(tok)
        terms.append(tok)

    for run in _CJK_RUN_RE.findall(low):
        if len(run) < 2:
            continue
        _add(run)
        if len(run) > 2:
            for idx in range(len(run) - 1):
                _add(run[idx : idx + 2])
    for tok in _ALNUM_RUN_RE.findall(low):
        _add(tok)
    return compact, terms


def compute_lexical_score(
    query: str,
    record: AiMemeRecord,
    *,
    dense_passed: bool = False,
) -> float:
    """计算结构化词面分：标题/配字强匹配 + 二元词以上情绪/适用/画面加权，禁单字命中。"""
    q_compact, terms = extract_lexical_terms(query)
    if not q_compact and not terms:
        return 0.0

    score = 0.0
    title_compact = _compact_norm(record.title)
    ocr_compact = _compact_norm(record.ocr_text)

    exact_title_ocr = False
    if len(q_compact) >= 2:
        for target in (title_compact, ocr_compact):
            if len(target) >= 2 and (q_compact in target or target in q_compact):
                exact_title_ocr = True
                break
    if exact_title_ocr:
        score += 0.55

    if not terms:
        return score

    title_ocr_blob = f"{record.title} {record.ocr_text}".lower()
    title_ocr_hits = sum(1 for tok in terms if tok in title_ocr_blob)
    if title_ocr_hits > 0:
        score += 0.35 * (title_ocr_hits / len(terms))

    non_generic_emotions = [tag.lower() for tag in record.emotion_tags if tag not in _GENERIC_LEGACY_TAGS]
    generic_emotions = [tag.lower() for tag in record.emotion_tags if tag in _GENERIC_LEGACY_TAGS]
    usage_low = record.usage_hint.lower()

    emo_usage_hits = 0
    generic_only_hit = False
    for tok in terms:
        if tok in _GENERIC_LEGACY_TAGS:
            if tok in usage_low or any(tok in emo for emo in generic_emotions):
                generic_only_hit = True
            continue
        if tok in usage_low or any(tok in emo for emo in non_generic_emotions):
            emo_usage_hits += 1
        elif any(tok in emo for emo in generic_emotions):
            generic_only_hit = True
    if emo_usage_hits > 0:
        score += 0.30 * (emo_usage_hits / len(terms))
    elif generic_only_hit and (score > 0.0 or dense_passed):
        score += 0.03

    desc_custom_blob = f"{record.description} {' '.join(record.custom_tags)}".lower()
    desc_hits = sum(1 for tok in terms if tok in desc_custom_blob)
    if desc_hits > 0:
        score += 0.15 * (desc_hits / len(terms))

    return score


def get_memes_base_path() -> Path:
    """获取表情包存储根目录"""
    return get_res_path(["ai_core", "memes"])


def get_folder_path(folder: str) -> Path:
    """获取指定文件夹路径（拒绝穿越出 memes 根目录）"""
    return safe_join(get_memes_base_path(), folder)


def compute_meme_id(image_data: bytes) -> str:
    """计算图片内容的 meme_id（sha256 前 16 位）"""
    return hashlib.sha256(image_data).hexdigest()[:16]


def ext_for_mime(mime: str) -> str:
    """MIME 对应的扩展名。不认识的类型用 .jpg。"""
    if mime in _MIME_EXT_MAP:
        return _MIME_EXT_MAP[mime]
    return ".jpg"


# MIME 类型到文件扩展名的映射
_MIME_EXT_MAP: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@to_thread
def _write_file(path: Path, data: bytes) -> None:
    """同步写入文件（通过 to_thread 异步化）"""
    path.write_bytes(data)


@to_thread
def _read_file(path: Path) -> bytes:
    """同步读取文件（通过 to_thread 异步化）"""
    return path.read_bytes()


@to_thread
def _unlink_file(path: Path) -> None:
    """同步删除文件（通过 to_thread 异步化）"""
    path.unlink()


@to_thread
def _move_file(src: Path, dst: Path) -> None:
    """同步移动文件（通过 to_thread 异步化）"""
    shutil.move(str(src), str(dst))


class MemeLibrary:
    """表情包存储管理器"""

    @staticmethod
    async def exists(meme_id: str) -> bool:
        """检查 meme_id 是否已存在"""
        return await AiMemeRecord.exists_by_meme_id(meme_id)

    @staticmethod
    async def get_record(meme_id: str) -> Optional[AiMemeRecord]:
        """获取单条记录"""
        return await AiMemeRecord.get_by_meme_id(meme_id)

    @staticmethod
    async def save_raw(
        image_data: bytes,
        file_mime: str,
        source_group: str = "",
        source_user: str = "",
        source_url: str = "",
        *,
        pending_pack_id: str = "",
        assign_default_pack: bool = True,
        enabled: bool = True,
    ) -> Optional[AiMemeRecord]:
        """缩到短边 400px 后写入 inbox。已有同一张图时返回 None。"""
        prepared = await to_thread(prepare_meme_image)(image_data, file_mime)
        return await MemeLibrary.save_prepared(
            prepared,
            source_group=source_group,
            source_user=source_user,
            source_url=source_url,
            pending_pack_id=pending_pack_id,
            assign_default_pack=assign_default_pack,
            enabled=enabled,
        )

    @staticmethod
    async def save_prepared(
        prepared: PreparedMeme,
        source_group: str = "",
        source_user: str = "",
        source_url: str = "",
        *,
        pending_pack_id: str = "",
        assign_default_pack: bool = True,
        enabled: bool = True,
    ) -> Optional[AiMemeRecord]:
        """写入已归一化的图到 inbox。已有同一张图时返回 None。"""
        # 先缩放再算 id，磁盘上的字节和 meme_id 是同一张图。
        image_data = prepared.data
        file_mime = prepared.mime
        width = prepared.width
        height = prepared.height
        meme_id = compute_meme_id(image_data)

        # 检查是否已存在
        if await AiMemeRecord.exists_by_meme_id(meme_id):
            logger.debug(t("log.meme.image_exists_skipping", meme_id=meme_id))
            return None

        ext = ext_for_mime(file_mime)

        # 写入文件到 inbox
        inbox_path = get_folder_path("inbox")
        inbox_path.mkdir(parents=True, exist_ok=True)
        file_name = f"{meme_id}{ext}"
        file_path = inbox_path / file_name
        await _write_file(file_path, image_data)

        # 相对路径（相对于 memes 目录）
        relative_path = f"inbox/{file_name}"

        # 创建数据库记录
        record = AiMemeRecord(
            meme_id=meme_id,
            file_path=relative_path,
            file_size=len(image_data),
            file_mime=file_mime,
            width=width,
            height=height,
            source_group=source_group,
            source_user=source_user,
            source_url=source_url[:1024],
            folder="inbox",
            status="pending",
            assign_default_pack=assign_default_pack if not pending_pack_id else False,
            pending_pack_id=pending_pack_id,
            enabled=enabled,
        )
        await AiMemeRecord.insert_record(record)
        logger.info(
            t(
                "log.meme.id_width_height",
                meme_id=meme_id,
                width=width,
                height=height,
                file_mime=file_mime,
            )
        )
        return record

    @staticmethod
    async def move_file(meme_id: str, target_folder: str, *, rewrite_persona_hint: bool = True) -> bool:
        """移动表情包文件到目标文件夹

        Args:
            meme_id: 表情包 ID
            target_folder: 目标文件夹名（如 "common", "persona_xxx"）

        Returns:
            是否成功
        """
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if record is None:
            return False

        base_path = get_memes_base_path()
        try:
            old_path = safe_join(base_path, record.file_path)
            target_path = get_folder_path(target_folder)
        except PathEscapeError:
            return False

        # 确保目标文件夹存在
        target_path.mkdir(parents=True, exist_ok=True)

        # 构建新路径
        file_name = Path(record.file_path).name
        try:
            new_path = safe_join(target_path, file_name)
            new_relative_path = str(new_path.relative_to(base_path.resolve())).replace("\\", "/")
        except (PathEscapeError, ValueError):
            return False

        # 移动文件（源与目标相同则视为已就位）
        if old_path != new_path:
            if old_path.exists():
                await _move_file(old_path, new_path)
            elif not new_path.exists():
                logger.warning(t("log.meme.path_old_path", old_path=old_path))
                return False

        # 更新数据库：folder 是 persona 路由的代理，移动到 common/persona_* 时
        # 同步更新 persona_hint，保证两者双向一致
        update_data: dict = {
            "file_path": new_relative_path,
            "folder": target_folder,
        }
        if rewrite_persona_hint and target_folder == "common":
            update_data["persona_hint"] = "common"
        elif rewrite_persona_hint and target_folder.startswith("persona_"):
            update_data["persona_hint"] = target_folder[len("persona_") :]
        await AiMemeRecord.update_record(meme_id, update_data)

        # 已入索引的记录必须重新同步 Qdrant，否则 payload 里的 folder 过期，
        # persona 路由（按 folder 过滤）会继续命中旧文件夹
        if record.status in ("tagged", "manual"):
            updated = await AiMemeRecord.get_by_meme_id(meme_id)
            if updated is not None:
                try:
                    await MemeLibrary.sync_to_qdrant(updated)
                except Exception as e:
                    logger.warning(t("log.meme.qdrant_id", meme_id=meme_id, e=e))

        logger.info(t("log.meme.id_target_folder", meme_id=meme_id, target_folder=target_folder))
        return True

    @staticmethod
    async def delete_meme(meme_id: str) -> bool:
        """删除表情包（文件 + 数据库记录）

        Args:
            meme_id: 表情包 ID

        Returns:
            是否成功
        """
        record = await AiMemeRecord.get_by_meme_id(meme_id)
        if record is None:
            return False

        # 删除文件
        try:
            file_path = safe_join(get_memes_base_path(), record.file_path)
        except PathEscapeError:
            file_path = None
        if file_path is not None and file_path.exists():
            await _unlink_file(file_path)

        # 删除 Qdrant 向量（无条件按 meme_id 过滤删点，兼顾 qdrant_id 缺失的历史数据）
        try:
            await _remove_from_qdrant(meme_id)
        except Exception as e:
            logger.warning(t("log.meme.meme_qdrant_fail_delete_vector", e=e))

        from gsuid_core.ai_core.meme.pack_model import PackStore

        await PackStore.delete_members_of_meme(meme_id)
        await AiMemeRecord.delete_by_meme_id(meme_id)
        evict_outbound_meme_cache(meme_id)
        logger.info(t("log.meme.meme_id_delete", meme_id=meme_id))
        return True

    @staticmethod
    async def drop_missing_files() -> int:
        """文件已经不在磁盘上的表情，删掉记录、成员和向量。"""
        records = await AiMemeRecord.get_all_records_no_page()
        base = get_memes_base_path()
        removed = 0
        for record in records:
            try:
                path = safe_join(base, record.file_path)
            except PathEscapeError:
                path = None
            if path is not None and path.is_file():
                continue
            await MemeLibrary.delete_meme(record.meme_id)
            removed += 1
        if removed:
            logger.info(t("log.meme.drop_missing_files", count=removed))
        return removed

    @staticmethod
    async def purge_memes(
        status: Optional[str] = None,
        folder: Optional[str] = None,
        batch_size: int = 200,
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """按条件批量清空表情包（DB 权威，再清源文件 + Qdrant）。

        面向数万条：分批删库 → 再删文件 / 向量，避免「有库无文件」。

        Args:
            status: 可选状态过滤（pending/tagged/manual/pending_manual/rejected）
            folder: 可选文件夹过滤
            batch_size: 每批处理条数

        Returns:
            (DB 成功删除数, 失败项 ``[{"meme_id", "reason"}]``；含清理失败)
        """
        rows = await AiMemeRecord.list_for_purge(status=status, folder=folder)
        if not rows:
            return 0, []

        base = get_memes_base_path()
        success_count = 0
        failed: List[Dict[str, Any]] = []
        path_map: Dict[str, str] = {meme_id: file_path for meme_id, file_path in rows}
        total = len(rows)

        for offset in range(0, total, batch_size):
            batch = rows[offset : offset + batch_size]
            batch_ids = [meme_id for meme_id, _ in batch]
            cleanup_ids: List[str] = []

            try:
                deleted = await AiMemeRecord.delete_by_meme_ids(batch_ids)
            except Exception as e:
                logger.warning(t("log.meme.bulk_delete_failed_fallback", e=e))
                for meme_id in batch_ids:
                    try:
                        ok = await AiMemeRecord.delete_by_meme_id(meme_id)
                        if ok:
                            success_count += 1
                            cleanup_ids.append(meme_id)
                        else:
                            failed.append({"meme_id": meme_id, "reason": "db delete returned false"})
                    except Exception as e2:
                        failed.append({"meme_id": meme_id, "reason": str(e2)})
            else:
                if deleted > 0:
                    success_count += deleted
                    # rowcount 无法标出哪些 id；本批均按待清理处理（缺库 id 仅多一次 unlink）
                    cleanup_ids = list(batch_ids)
                    if deleted < len(batch_ids):
                        logger.warning(
                            t(
                                "log.meme.bulk_delete_rowcount_short",
                                deleted=deleted,
                                batch=len(batch_ids),
                            )
                        )
                else:
                    # rowcount=0：逐条确认，避免把整批虚报为成功
                    for meme_id in batch_ids:
                        try:
                            ok = await AiMemeRecord.delete_by_meme_id(meme_id)
                            if ok:
                                success_count += 1
                                cleanup_ids.append(meme_id)
                            else:
                                failed.append({"meme_id": meme_id, "reason": "db delete returned false"})
                        except Exception as e2:
                            failed.append({"meme_id": meme_id, "reason": str(e2)})

            if not cleanup_ids:
                continue

            from gsuid_core.ai_core.meme.pack_model import PackStore

            await PackStore.delete_members_of_memes(cleanup_ids)
            for mid in cleanup_ids:
                evict_outbound_meme_cache(mid)

            unlink_jobs: List[Tuple[str, Path]] = []
            for meme_id in cleanup_ids:
                if meme_id not in path_map:
                    continue
                path = base / path_map[meme_id]
                if path.exists():
                    unlink_jobs.append((meme_id, path))

            if unlink_jobs:
                results = await asyncio.gather(
                    *[_unlink_file(p) for _, p in unlink_jobs],
                    return_exceptions=True,
                )
                for (meme_id, _), res in zip(unlink_jobs, results):
                    if isinstance(res, Exception):
                        logger.warning(t("log.meme.purge_unlink_failed", meme_id=meme_id, res=res))
                        failed.append(
                            {
                                "meme_id": meme_id,
                                "reason": f"file unlink failed (db deleted): {res}",
                            }
                        )

            try:
                await _remove_many_from_qdrant(cleanup_ids)
            except Exception as e:
                logger.warning(t("log.meme.meme_qdrant_fail_delete_vector", e=e))
                sample = cleanup_ids[0]
                failed.append(
                    {
                        "meme_id": sample,
                        "reason": (f"qdrant delete failed for {len(cleanup_ids)} ids (db deleted): {e}"),
                    }
                )

        logger.info(
            t(
                "log.meme.purge_done",
                status=status or "*",
                folder=folder or "*",
                success=success_count,
                failed=len(failed),
            )
        )
        return success_count, failed

    @staticmethod
    async def update_tags(
        meme_id: str,
        description: Optional[str] = None,
        emotion_tags: Optional[List[str]] = None,
        scene_tags: Optional[List[str]] = None,
        custom_tags: Optional[List[str]] = None,
        persona_hint: Optional[str] = None,
        status: Optional[str] = None,
        *,
        title: Optional[str] = None,
        ocr_text: Optional[str] = None,
        usage_hint: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> bool:
        """更新表情包结构化元数据与启停状态。"""
        update_data: dict[str, object] = {}
        if title is not None:
            update_data["title"] = title[:64]
        if ocr_text is not None:
            update_data["ocr_text"] = ocr_text[:256]
        if usage_hint is not None:
            update_data["usage_hint"] = usage_hint[:512]
        if description is not None:
            update_data["description"] = description[:512]
        if emotion_tags is not None:
            update_data["emotion_tags"] = emotion_tags
        if scene_tags is not None:
            update_data["scene_tags"] = scene_tags
        if custom_tags is not None:
            update_data["custom_tags"] = custom_tags
        if persona_hint is not None:
            update_data["persona_hint"] = persona_hint
        if enabled is not None:
            update_data["enabled"] = enabled
        if status is not None:
            update_data["status"] = status
            if status == "tagged":
                update_data["tagged_at"] = datetime.now(timezone.utc)

        if not update_data:
            return False

        return await AiMemeRecord.update_record(meme_id, update_data)

    @staticmethod
    async def mark_tag_failed(meme_id: str) -> None:
        """标记打标失败。保留待定包，重新打标成功后仍归入；不改 assign_default_pack。"""
        await AiMemeRecord.update_record(
            meme_id,
            {"status": "pending_manual"},
        )

    @staticmethod
    async def mark_rejected(meme_id: str, nsfw_score: float) -> None:
        """标记为 rejected（NSFW 或质量不达标）

        Args:
            meme_id: 表情包 ID
            nsfw_score: NSFW 分数
        """
        from gsuid_core.ai_core.meme.pack_model import PackStore

        await PackStore.reject_meme(meme_id)
        await MemeLibrary.move_file(meme_id, "rejected")
        await AiMemeRecord.update_record(
            meme_id,
            {
                "status": "rejected",
                "nsfw_score": nsfw_score,
            },
        )
        # rejected 记录不应留在向量索引中
        await MemeLibrary.remove_from_index(meme_id)

    @staticmethod
    async def search(
        query_vector: List[float],
        folder: Optional[str] = None,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        query_sparse: Optional["SparseVector"] = None,
        pack_ids: Optional[Sequence[str]] = None,
    ) -> Sequence[AiMemeRecord]:
        """通过稀疏 + 稠密混合向量检索搜索表情包

        Args:
            query_vector: 查询稠密向量
            folder: 可选的文件夹过滤
            top_k: 返回数量
            score_threshold: 最低相似度阈值（仅作用于 dense 分支的余弦门）
            query_sparse: 查询稀疏向量（BM25），None 时降级纯 dense

        Returns:
            匹配的 AiMemeRecord 列表
        """
        meme_ids = await _search_qdrant(query_vector, query_sparse, folder, top_k, score_threshold, pack_ids)
        if not meme_ids:
            return []
        return await AiMemeRecord.search_by_ids(meme_ids)

    @staticmethod
    async def search_scored(
        query_text: str,
        folder: Optional[str] = None,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        pack_ids: Optional[Sequence[str]] = None,
        *,
        include_disabled: bool = False,
    ) -> List[ScoredMeme]:
        """真实稠密余弦分 + 二元词以上结构化词面加权检索（零 Reranker）。"""
        if folder is not None and pack_ids is not None:
            logger.warning(t("log.meme.pack_and_folder"))
            return []
        if pack_ids is not None and len(pack_ids) == 0:
            return []

        from gsuid_core.ai_core.meme.config import meme_config

        threshold = (
            float(score_threshold)
            if score_threshold is not None
            else float(meme_config.get_config("meme_search_threshold").data)
        )

        dense_scores: dict[str, float] = {}
        query_vector = await _embed_text(query_text)
        if query_vector is not None:
            dense_hits = await _search_qdrant_dense(
                query_vector,
                folder=folder,
                top_k=max(top_k * 4, 16),
                score_threshold=threshold,
                pack_ids=pack_ids,
                include_disabled=include_disabled,
            )
            for mid, score in dense_hits:
                if mid not in dense_scores or score > dense_scores[mid]:
                    dense_scores[mid] = score

        candidate_map: dict[str, AiMemeRecord] = {}
        if dense_scores:
            dense_records = await AiMemeRecord.search_by_ids(list(dense_scores.keys()))
            if pack_ids is not None:
                from gsuid_core.ai_core.meme.pack_model import PackStore

                wanted_packs = set(pack_ids)
                pack_map = await PackStore.pack_ids_by_memes([rec.meme_id for rec in dense_records])
                for rec in dense_records:
                    rec_packs = pack_map[rec.meme_id] if rec.meme_id in pack_map else []
                    if any(pid in wanted_packs for pid in rec_packs):
                        candidate_map[rec.meme_id] = rec
            else:
                for rec in dense_records:
                    candidate_map[rec.meme_id] = rec

        _, lex_terms = extract_lexical_terms(query_text)
        if lex_terms:
            lexical_records = await AiMemeRecord.search_lexical_candidates(
                lex_terms,
                folder=folder,
                pack_ids=pack_ids,
                include_disabled=include_disabled,
                limit=max(top_k * 20, 200),
            )
            for rec in lexical_records:
                candidate_map[rec.meme_id] = rec

        scored: List[ScoredMeme] = []
        for meme_id, record in candidate_map.items():
            if record.status not in ("tagged", "manual"):
                continue
            if not include_disabled and not record.enabled:
                continue
            if folder is not None and record.folder != folder:
                continue

            dense_score = dense_scores[meme_id] if meme_id in dense_scores else 0.0
            dense_ok = meme_id in dense_scores and dense_score >= threshold
            lexical_score = compute_lexical_score(query_text, record, dense_passed=dense_ok)

            if dense_ok and lexical_score > 0.0:
                final_score = dense_score + 0.45 * lexical_score
            elif dense_ok:
                final_score = dense_score
            elif lexical_score >= 0.30:
                final_score = threshold + 0.5 * lexical_score
            else:
                continue

            scored.append(
                ScoredMeme(
                    record=record,
                    score=final_score,
                    dense_score=dense_score,
                    lexical_score=lexical_score,
                )
            )

        scored.sort(key=lambda item: (item.score, item.dense_score, item.lexical_score), reverse=True)
        return scored[:top_k]

    @staticmethod
    async def search_by_text(
        query_text: str,
        folder: Optional[str] = None,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
        pack_ids: Optional[Sequence[str]] = None,
        *,
        include_disabled: bool = False,
    ) -> Sequence[AiMemeRecord]:
        """文本语义 + 二元词结构化检索（兼容既有签名）。"""
        scored = await MemeLibrary.search_scored(
            query_text=query_text,
            folder=folder,
            top_k=top_k,
            score_threshold=score_threshold,
            pack_ids=pack_ids,
            include_disabled=include_disabled,
        )
        return [item.record for item in scored]

    @staticmethod
    async def sync_to_qdrant(record: AiMemeRecord) -> None:
        """将表情包的正向结构化文本和元数据同步到 Qdrant 向量索引。"""
        content = build_meme_index_text(record)
        if not content:
            return

        point_id = await _upsert_to_qdrant(
            meme_id=record.meme_id,
            content=content,
            folder=record.folder,
            persona_hint=record.persona_hint,
            status=record.status,
            use_count=record.use_count,
            file_mime=record.file_mime,
            enabled=record.enabled,
            existing_qdrant_id=record.qdrant_id,
        )
        if point_id:
            await AiMemeRecord.update_record(
                record.meme_id,
                {
                    "qdrant_id": point_id,
                },
            )

    @staticmethod
    async def remove_from_index(meme_id: str) -> None:
        """从向量索引中移除表情包（用于状态变为不可检索时，如 rejected）"""
        try:
            await _remove_from_qdrant(meme_id)
        except Exception as e:
            logger.warning(t("log.meme.fail_vector_meme_id", meme_id=meme_id, e=e))
            return
        await AiMemeRecord.update_record(meme_id, {"qdrant_id": ""})


# ── Qdrant 操作辅助函数 ──

MEME_COLLECTION_NAME = "ai_meme"
MEME_PAYLOAD_INDEX_FIELDS = ("folder", "status", "meme_id", "pack_ids", "enabled")
# 命名 dense 向量名（旧库为单一**无名**向量；改名即触发结构迁移，与知识库一致）
MEME_DENSE_VECTOR = "dense"


def _payload_schema_for(field_name: str) -> PayloadSchemaType:
    if field_name == "enabled":
        return PayloadSchemaType.BOOL
    return PayloadSchemaType.KEYWORD


def _meme_vectors_config(dimension: int) -> dict[str, VectorParams]:
    """表情包 dense 命名向量配置。"""
    from gsuid_core.ai_core.rag.qmodels import Distance

    return {MEME_DENSE_VECTOR: VectorParams(size=dimension, distance=Distance.COSINE, on_disk=True)}


def _meme_sparse_config() -> dict[str, SparseVectorParams]:
    """表情包 BM25 稀疏向量配置（IDF 服务端加权，与知识库一致）。"""
    from gsuid_core.ai_core.rag.qmodels import Modifier

    return {"sparse": SparseVectorParams(modifier=Modifier.IDF)}


async def _ensure_meme_collection() -> None:
    """确保 ai_meme 存在。新建、被掏空或维度变化时，按 SQL 记录重建向量。"""
    from gsuid_core.ai_core.rag.base import client, get_strict_dimension
    from gsuid_core.ai_core.rag.collection_migration import force_recreate_collection, collection_vector_mismatched

    if client is None:
        return

    dimension = get_strict_dimension()
    existing = {c.name for c in (await client.get_collections()).collections}
    # 集合不存在时也要从 SQL 灌入。只建空集合会把重建拖到下一次启动。
    should_reindex = MEME_COLLECTION_NAME not in existing

    if MEME_COLLECTION_NAME in existing:
        # 传 vector_name="dense" 同时覆盖两种迁移触发：① 维度变化（换嵌入模型）；
        # ② 结构变化（旧库的单一**无名** dense → 命名 "dense" + sparse）。重嵌后即为命名+稀疏结构。
        if await collection_vector_mismatched(MEME_COLLECTION_NAME, dimension, vector_name=MEME_DENSE_VECTOR):
            logger.warning(
                t(
                    "log.meme.collection_name_dense",
                    MEME_COLLECTION_NAME=MEME_COLLECTION_NAME,
                )
            )
            should_reindex = True
        else:
            if await _meme_collection_needs_recovery():
                logger.warning(
                    t(
                        "log.meme.collection_name",
                        MEME_COLLECTION_NAME=MEME_COLLECTION_NAME,
                    )
                )
                should_reindex = True
            else:
                for field_name in MEME_PAYLOAD_INDEX_FIELDS:
                    try:
                        await client.create_payload_index(
                            collection_name=MEME_COLLECTION_NAME,
                            field_name=field_name,
                            field_schema=_payload_schema_for(field_name),
                        )
                    except Exception as e:
                        # 索引已存在或后端不支持，幂等场景下属预期
                        logger.debug(t("log.meme.payload_field_name", field_name=field_name, e=e))
                await _backfill_missing_meme_vectors()
                return

    if MEME_COLLECTION_NAME not in existing or should_reindex:
        await force_recreate_collection(
            collection_name=MEME_COLLECTION_NAME,
            vectors_config=_meme_vectors_config(dimension),
            sparse_vectors_config=_meme_sparse_config(),
            on_disk_payload=True,
        )
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError(t("log.meme.qdrant_client"))
        for field_name in MEME_PAYLOAD_INDEX_FIELDS:
            await refreshed_client.create_payload_index(
                collection_name=MEME_COLLECTION_NAME,
                field_name=field_name,
                field_schema=_payload_schema_for(field_name),
            )
        logger.info(
            t(
                "log.meme.qdrant_collection_name",
                MEME_COLLECTION_NAME=MEME_COLLECTION_NAME,
                dimension=dimension,
            )
        )

    if should_reindex:
        await _reindex_meme_collection_from_db()


async def _eligible_meme_records() -> list[AiMemeRecord]:
    """返回可写入向量索引的表情包记录。"""
    records = await AiMemeRecord.get_all_records_no_page()
    return [record for record in records if record.status in {"tagged", "manual"}]


def _needs_full_meme_reindex(*, point_count: int, eligible: int) -> bool:
    """只有集合被掏空才整库重建。少数点缺失（空描述/写入失败）走补齐，禁止每次启动删库。"""
    return eligible > 0 and point_count <= 0


async def _meme_indexed_ids() -> set[str]:
    from gsuid_core.ai_core.rag.collection_migration import scroll_all_payloads

    ids: set[str] = set()
    for _pid, payload in await scroll_all_payloads(MEME_COLLECTION_NAME):
        raw = payload["meme_id"] if "meme_id" in payload else ""
        if isinstance(raw, str) and raw:
            ids.add(raw)
    return ids


async def _backfill_missing_meme_vectors() -> None:
    """已有集合上补写缺失点，不 delete collection。"""
    records = await _eligible_meme_records()
    if not records:
        return
    indexed = await _meme_indexed_ids()
    missing = [record for record in records if record.meme_id not in indexed]
    if not missing:
        return
    restored = 0
    skipped = 0
    for record in missing:
        content = build_meme_index_text(record)
        if not content:
            skipped += 1
            continue
        try:
            await MemeLibrary.sync_to_qdrant(record)
            restored += 1
        except Exception as e:
            skipped += 1
            logger.warning(t("log.meme.fail_skip_vector", p0=record.meme_id, e=e))
    logger.info(
        t(
            "log.meme.backfill_missing_vectors",
            restored=restored,
            skipped=skipped,
        )
    )


async def _meme_collection_needs_recovery() -> bool:
    """集合被掏空才整库重建；点略少于 SQL 只补齐。"""
    from gsuid_core.ai_core.rag.collection_migration import count_collection_points

    records = await _eligible_meme_records()
    if not records:
        return False
    point_count = await count_collection_points(MEME_COLLECTION_NAME)
    return _needs_full_meme_reindex(point_count=point_count, eligible=len(records))


async def _reindex_meme_collection_from_db() -> None:
    """基于 AiMemeRecord 数据库记录重建表情包向量索引。"""
    records = await _eligible_meme_records()
    restored = 0
    skipped = 0
    for record in records:
        content = build_meme_index_text(record)
        if not content:
            skipped += 1
            continue
        try:
            await MemeLibrary.sync_to_qdrant(record)
            restored += 1
        except Exception as e:
            skipped += 1
            logger.warning(t("log.meme.fail_skip_vector", p0=record.meme_id, e=e))

    logger.info(t("log.meme.restored_skip_skipped", restored=restored, skipped=skipped))
    if records and restored == 0:
        raise RuntimeError(t("log.meme.resume_migrate_retry"))


async def _embed_text(text: str) -> Optional[List[float]]:
    """将文本编码为向量"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        return None
    return await embedding_provider.embed_single(text)


async def _force_recreate_meme_collection() -> None:
    """强制重建表情包 Collection（命名 dense + BM25 稀疏），用于本地 Qdrant 旧维度 ndarray 残留自恢复。"""
    from gsuid_core.ai_core.rag.base import client, get_strict_dimension
    from gsuid_core.ai_core.rag.collection_migration import force_recreate_collection

    if client is None:
        return

    dimension = get_strict_dimension()
    await force_recreate_collection(
        collection_name=MEME_COLLECTION_NAME,
        vectors_config=_meme_vectors_config(dimension),
        sparse_vectors_config=_meme_sparse_config(),
        on_disk_payload=True,
    )
    from gsuid_core.ai_core.rag.base import client as refreshed_client

    if refreshed_client is None:
        raise RuntimeError(t("log.meme.qdrant_client"))
    for field_name in MEME_PAYLOAD_INDEX_FIELDS:
        await refreshed_client.create_payload_index(
            collection_name=MEME_COLLECTION_NAME,
            field_name=field_name,
            field_schema=_payload_schema_for(field_name),
        )
    logger.info(
        t(
            "log.meme.qdrant_collection_name_2",
            MEME_COLLECTION_NAME=MEME_COLLECTION_NAME,
            dimension=dimension,
        )
    )


async def _upsert_to_qdrant(
    meme_id: str,
    content: str,
    folder: str,
    persona_hint: str,
    status: str,
    use_count: int,
    file_mime: str,
    *,
    enabled: bool = True,
    existing_qdrant_id: str = "",
) -> Optional[str]:
    """写入或更新 Qdrant 向量（UUID5 幂等点 ID，含 enabled 与 pack_ids payload）。"""

    from gsuid_core.ai_core.rag.base import client, get_point_id
    from gsuid_core.ai_core.rag.qmodels import PointStruct

    if client is None:
        return None

    vector = await _embed_text(content)
    if vector is None:
        return None

    point_id = get_point_id(meme_id)
    # 仅在历史随机 UUID 点与确定性 UUID5 不一致时才先删旧点，避免并发 delete-then-upsert 丢点
    if existing_qdrant_id and existing_qdrant_id != point_id:
        try:
            await _remove_from_qdrant(meme_id)
        except Exception as e:
            logger.warning(t("log.meme.fail_cleanup_write_vector", meme_id=meme_id, e=e))
    from gsuid_core.ai_core.meme.pack_model import PackStore

    member_pack_ids = await PackStore.pack_ids_of_meme(meme_id)
    # 检索已改为 dense + SQL 二元词面，省略未消费的 sparse BM25 写入（保持集合 schema 兼容）
    named_vector: dict[str, Vector] = {MEME_DENSE_VECTOR: vector}
    point = PointStruct(
        id=point_id,
        vector=named_vector,
        payload={
            "meme_id": meme_id,
            "folder": folder,
            "persona_hint": persona_hint,
            "status": status,
            "use_count": use_count,
            "file_mime": file_mime,
            "pack_ids": member_pack_ids,
            "enabled": enabled,
        },
    )
    try:
        await client.upsert(
            collection_name=MEME_COLLECTION_NAME,
            points=[point],
        )
    except Exception as e:
        from gsuid_core.ai_core.rag.collection_migration import is_vector_structure_error

        if not is_vector_structure_error(str(e)):
            raise
        logger.warning(t("log.meme.qdrant_collection", e=e))
        await _force_recreate_meme_collection()
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError(t("log.meme.qdrant_client"))
        await refreshed_client.upsert(
            collection_name=MEME_COLLECTION_NAME,
            points=[point],
        )
    return point_id


def _build_meme_filter(
    *,
    folder: Optional[str],
    pack_ids: Optional[Sequence[str]],
    include_disabled: bool = False,
) -> Filter:
    from gsuid_core.ai_core.rag.qmodels import (
        MatchAny,
        MatchValue,
        FieldCondition,
    )

    must_conditions: list[Condition] = [
        FieldCondition(
            key="status",
            match=MatchAny(any=["tagged", "manual"]),
        ),
    ]
    if folder:
        must_conditions.insert(
            0,
            FieldCondition(
                key="folder",
                match=MatchValue(value=folder),
            ),
        )
    if pack_ids:
        must_conditions.append(
            FieldCondition(
                key="pack_ids",
                match=MatchAny(any=list(pack_ids)),
            )
        )
    must_not_conditions: list[Condition] | None = (
        None if include_disabled else [FieldCondition(key="enabled", match=MatchValue(value=False))]
    )
    return Filter(must=must_conditions, must_not=must_not_conditions)


async def _search_qdrant_dense(
    query_dense: List[float],
    *,
    folder: Optional[str],
    top_k: int,
    score_threshold: Optional[float] = None,
    pack_ids: Optional[Sequence[str]] = None,
    include_disabled: bool = False,
) -> List[Tuple[str, float]]:
    """直查 Qdrant 命名 dense 向量，返回保留真实余弦相似度的 (meme_id, score) 列表。"""
    from gsuid_core.ai_core.rag.base import client
    from gsuid_core.ai_core.rag.collection_migration import is_vector_structure_error

    if client is None:
        return []

    query_filter = _build_meme_filter(folder=folder, pack_ids=pack_ids, include_disabled=include_disabled)
    if score_threshold is None:
        from gsuid_core.ai_core.meme.config import meme_config

        score_threshold = float(meme_config.get_config("meme_search_threshold").data)

    try:
        response = await client.query_points(
            collection_name=MEME_COLLECTION_NAME,
            query=query_dense,
            using=MEME_DENSE_VECTOR,
            query_filter=query_filter,
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )
    except IndexError as e:
        logger.critical(
            t(
                "log.rag.hybrid_collection_name_local_start",
                collection_name=MEME_COLLECTION_NAME,
                e=e,
            )
        )
        return []
    except Exception as e:
        if is_vector_structure_error(str(e)):
            logger.warning(
                t(
                    "log.rag.hybrid_collection_name_vector_fail",
                    collection_name=MEME_COLLECTION_NAME,
                    e=e,
                )
            )
            return []
        raise

    hits: list[tuple[str, float]] = []
    for point in response.points:
        payload = point.payload
        if payload is None or "meme_id" not in payload:
            continue
        meme_id = payload["meme_id"]
        if isinstance(meme_id, str):
            hits.append((meme_id, float(point.score)))
    return hits


async def _search_qdrant(
    query_dense: List[float],
    query_sparse: Optional[SparseVector],
    folder: Optional[str],
    top_k: int,
    score_threshold: Optional[float] = None,
    pack_ids: Optional[Sequence[str]] = None,
) -> List[str]:
    """兼容旧接口：直查 dense 真实余弦分并返回 meme_id 列表。"""
    del query_sparse
    hits = await _search_qdrant_dense(
        query_dense,
        folder=folder,
        top_k=top_k,
        score_threshold=score_threshold,
        pack_ids=pack_ids,
    )
    return [meme_id for meme_id, _ in hits]


async def _remove_from_qdrant(meme_id: str) -> None:
    """从 Qdrant 中删除指定 meme_id 的向量"""
    from gsuid_core.ai_core.rag.base import client
    from gsuid_core.ai_core.rag.qmodels import Filter, MatchValue, FieldCondition

    if client is None:
        return

    await client.delete(
        collection_name=MEME_COLLECTION_NAME,
        points_selector=Filter(
            must=[
                FieldCondition(
                    key="meme_id",
                    match=MatchValue(value=meme_id),
                )
            ]
        ),
    )


async def _remove_many_from_qdrant(meme_ids: List[str]) -> None:
    """批量从 Qdrant 删除多个 meme_id 的向量。

    优先用 MatchAny 一次删完；客户端不支持时回退为逐条删除。
    """
    if not meme_ids:
        return

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    try:
        from gsuid_core.ai_core.rag.qmodels import Filter, MatchAny, FieldCondition

        await client.delete(
            collection_name=MEME_COLLECTION_NAME,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="meme_id",
                        match=MatchAny(any=list(meme_ids)),
                    )
                ]
            ),
        )
        return
    except Exception as e:
        logger.warning(t("log.meme.qdrant_matchany_batch_delete_failed", e=e))

    for meme_id in meme_ids:
        try:
            await _remove_from_qdrant(meme_id)
        except Exception as e:
            logger.warning(t("log.meme.meme_qdrant_fail_delete_vector", e=e))


async def write_member_payloads(meme_ids: Sequence[str] | None = None, *, strict: bool = False) -> None:
    """按成员表把完整 pack_ids 写进已有点。不嵌入。"""
    import time

    from gsuid_core.ai_core.rag.base import client, get_point_id
    from gsuid_core.ai_core.meme.pack_model import PackStore

    if client is None:
        return
    if meme_ids is None:
        records = [
            row
            for row in await AiMemeRecord.get_all_records_no_page()
            if row.qdrant_id or row.status in ("tagged", "manual")
        ]
    else:
        records = list(await AiMemeRecord.get_by_meme_ids(list(meme_ids)))
    started = time.perf_counter()
    pack_map = await PackStore.pack_ids_by_memes([r.meme_id for r in records])
    groups: dict[tuple[tuple[str, ...], bool], list[str]] = {}
    for record in records:
        point_id = record.qdrant_id or get_point_id(record.meme_id)
        try:
            found = await client.retrieve(
                collection_name=MEME_COLLECTION_NAME,
                ids=[point_id],
                with_payload=False,
                with_vectors=False,
            )
        except Exception as e:
            if strict:
                raise
            logger.warning(t("log.meme.pack_payload_fail", meme_id=record.meme_id, e=e))
            continue
        if not found:
            logger.warning(t("log.meme.pack_payload_fail", meme_id=record.meme_id, e="missing"))
            continue
        packs = tuple(pack_map[record.meme_id] if record.meme_id in pack_map else ())
        group_key = (packs, bool(record.enabled))
        if group_key not in groups:
            groups[group_key] = []
        groups[group_key].append(point_id)
    calls = 0
    point_count = 0
    for (packs, enabled), points in groups.items():
        payload: dict[str, object] = {"pack_ids": list(packs), "enabled": enabled}
        for offset in range(0, len(points), 100):
            chunk = points[offset : offset + 100]
            try:
                await client.set_payload(
                    collection_name=MEME_COLLECTION_NAME,
                    payload=payload,
                    points=chunk,
                )
            except Exception as e:
                if strict:
                    raise
                logger.warning(t("log.meme.pack_payload_fail", meme_id=chunk[0], e=e))
            calls += 1
            point_count += len(chunk)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        t(
            "log.meme.pack_payload_backfill",
            points=point_count,
            groups=len(groups),
            calls=calls,
            ms=elapsed_ms,
        )
    )
