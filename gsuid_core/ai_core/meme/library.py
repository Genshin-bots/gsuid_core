"""表情包存储层

MemeLibrary 负责文件系统操作和数据库操作的封装。
文件存储在 data/ai_core/memes/ 下，按文件夹分类。
"""

import shutil
import hashlib
from typing import List, Optional, Sequence
from pathlib import Path
from datetime import datetime, timezone

from gsuid_core.pool import to_thread
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.meme.database_model import AiMemeRecord


def get_memes_base_path() -> Path:
    """获取表情包存储根目录"""
    return get_res_path(["ai_core", "memes"])


def get_folder_path(folder: str) -> Path:
    """获取指定文件夹路径"""
    return get_memes_base_path() / folder


def compute_meme_id(image_data: bytes) -> str:
    """计算图片内容的 meme_id（sha256 前 16 位）"""
    return hashlib.sha256(image_data).hexdigest()[:16]


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
        width: int,
        height: int,
        source_group: str = "",
        source_user: str = "",
        source_url: str = "",
    ) -> Optional[AiMemeRecord]:
        """保存原始图片到 inbox 并创建数据库记录

        Args:
            image_data: 图片二进制数据
            file_mime: MIME 类型
            width: 图片宽度
            height: 图片高度
            source_group: 来源群组 ID
            source_user: 来源用户 ID
            source_url: 原始 URL

        Returns:
            创建的 AiMemeRecord，如果已存在则返回 None
        """
        meme_id = compute_meme_id(image_data)

        # 检查是否已存在
        if await AiMemeRecord.exists_by_meme_id(meme_id):
            logger.debug(f"[Meme] 图片已存在，跳过: {meme_id}")
            return None

        # 确定文件扩展名
        ext = _MIME_EXT_MAP.get(file_mime, ".jpg")

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
            source_url=source_url,
            folder="inbox",
            status="pending",
        )
        await AiMemeRecord.insert_record(record)
        logger.info(f"[Meme] 新表情包入库: {meme_id} ({width}x{height}, {file_mime})")
        return record

    @staticmethod
    async def move_file(meme_id: str, target_folder: str) -> bool:
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
        old_path = base_path / record.file_path

        # 确保目标文件夹存在
        target_path = get_folder_path(target_folder)
        target_path.mkdir(parents=True, exist_ok=True)

        # 构建新路径
        file_name = Path(record.file_path).name
        new_relative_path = f"{target_folder}/{file_name}"
        new_path = base_path / new_relative_path

        # 移动文件（源与目标相同则视为已就位）
        if old_path != new_path:
            if old_path.exists():
                await _move_file(old_path, new_path)
            elif not new_path.exists():
                logger.warning(f"[Meme] 源文件不存在: {old_path}")
                return False

        # 更新数据库：folder 是 persona 路由的代理，移动到 common/persona_* 时
        # 同步更新 persona_hint，保证两者双向一致
        update_data: dict = {
            "file_path": new_relative_path,
            "folder": target_folder,
        }
        if target_folder == "common":
            update_data["persona_hint"] = "common"
        elif target_folder.startswith("persona_"):
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
                    logger.warning(f"[Meme] 移动后同步 Qdrant 失败: {meme_id}: {e}")

        logger.info(f"[Meme] 移动表情包 {meme_id} -> {target_folder}")
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
        file_path = get_memes_base_path() / record.file_path
        if file_path.exists():
            await _unlink_file(file_path)

        # 删除 Qdrant 向量（无条件按 meme_id 过滤删点，兼顾 qdrant_id 缺失的历史数据）
        try:
            await _remove_from_qdrant(meme_id)
        except Exception as e:
            logger.warning(f"[Meme] 删除 Qdrant 向量失败: {e}")

        # 删除数据库记录
        await AiMemeRecord.delete_by_meme_id(meme_id)
        logger.info(f"[Meme] 删除表情包: {meme_id}")
        return True

    @staticmethod
    async def update_tags(
        meme_id: str,
        description: Optional[str] = None,
        emotion_tags: Optional[List[str]] = None,
        scene_tags: Optional[List[str]] = None,
        custom_tags: Optional[List[str]] = None,
        persona_hint: Optional[str] = None,
        status: Optional[str] = None,
    ) -> bool:
        """更新表情包标签信息

        Args:
            meme_id: 表情包 ID
            description: 描述文本
            emotion_tags: 情绪标签
            scene_tags: 场景标签
            custom_tags: 自定义标签
            persona_hint: Persona 归属提示
            status: 状态

        Returns:
            是否成功
        """
        update_data: dict = {}
        if description is not None:
            update_data["description"] = description
        if emotion_tags is not None:
            update_data["emotion_tags"] = emotion_tags
        if scene_tags is not None:
            update_data["scene_tags"] = scene_tags
        if custom_tags is not None:
            update_data["custom_tags"] = custom_tags
        if persona_hint is not None:
            update_data["persona_hint"] = persona_hint
        if status is not None:
            update_data["status"] = status
            if status == "tagged":
                update_data["tagged_at"] = datetime.now(timezone.utc)

        if not update_data:
            return False

        return await AiMemeRecord.update_record(meme_id, update_data)

    @staticmethod
    async def mark_tag_failed(meme_id: str) -> None:
        """标记打标失败，状态设为 pending_manual"""
        await AiMemeRecord.update_record(
            meme_id,
            {
                "status": "pending_manual",
            },
        )

    @staticmethod
    async def mark_rejected(meme_id: str, nsfw_score: float) -> None:
        """标记为 rejected（NSFW 或质量不达标）

        Args:
            meme_id: 表情包 ID
            nsfw_score: NSFW 分数
        """
        # 移动文件到 rejected 文件夹
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
    ) -> Sequence[AiMemeRecord]:
        """通过向量检索搜索表情包

        Args:
            query_vector: 查询向量
            folder: 可选的文件夹过滤
            top_k: 返回数量
            score_threshold: 最低相似度阈值，低于此值的结果将被过滤

        Returns:
            匹配的 AiMemeRecord 列表
        """
        meme_ids = await _search_qdrant(query_vector, folder, top_k, score_threshold)
        if not meme_ids:
            return []
        return await AiMemeRecord.search_by_ids(meme_ids)

    @staticmethod
    async def search_by_text(
        query_text: str,
        folder: Optional[str] = None,
        top_k: int = 5,
        score_threshold: Optional[float] = None,
    ) -> Sequence[AiMemeRecord]:
        """通过文本语义 + 标签精确匹配搜索表情包

        策略：
        1. 将查询文本按空格拆分为关键词，做标签/描述精确匹配
        2. 始终执行向量语义检索（description+tags 的语义近邻，
           如"困"可命中"想睡觉"，不会被标签精确匹配短路）
        3. 两路结果各占约一半名额合并去重，剩余名额互相回填，
           保证语义结果始终进入候选池而非仅做兜底

        Args:
            query_text: 查询文本
            folder: 可选的文件夹过滤
            top_k: 返回数量
            score_threshold: 最低相似度阈值，低于此值的结果将被过滤

        Returns:
            匹配的 AiMemeRecord 列表
        """
        # 将查询文本拆分为关键词用于标签匹配
        keywords = [kw.strip() for kw in query_text.split() if kw.strip()]

        # 标签/描述精确匹配
        tag_results = await AiMemeRecord.search_by_tags(keywords, folder=folder, limit=top_k)

        # 向量语义检索（与标签匹配并行参与，不再被短路）
        vec_results = await _vector_search(query_text, folder, top_k, score_threshold)

        # 合并去重：标签命中与向量命中各占约一半名额，剩余互相回填
        seen_ids: set[str] = set()
        merged: list[AiMemeRecord] = []

        def _take(records: Sequence[AiMemeRecord], quota: int) -> None:
            for record in records:
                if quota <= 0:
                    return
                if record.meme_id in seen_ids:
                    continue
                seen_ids.add(record.meme_id)
                merged.append(record)
                quota -= 1

        _take(tag_results, max(top_k // 2, 1))
        _take(vec_results, top_k - len(merged))
        _take(tag_results, top_k - len(merged))
        return merged[:top_k]

    @staticmethod
    async def sync_to_qdrant(record: AiMemeRecord) -> None:
        """将表情包的描述和标签同步到 Qdrant 向量索引

        Args:
            record: 表情包记录
        """
        content = f"{record.description} {' '.join(record.all_tags)}".strip()
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
            logger.warning(f"[Meme] 从向量索引移除失败: {meme_id}: {e}")
            return
        await AiMemeRecord.update_record(meme_id, {"qdrant_id": ""})


async def _vector_search(
    query_text: str,
    folder: Optional[str],
    top_k: int,
    score_threshold: Optional[float],
) -> Sequence[AiMemeRecord]:
    """纯向量语义检索（内部辅助函数，供 search_by_text 调用）"""
    query_vector = await _embed_text(query_text)
    if query_vector is None:
        return []
    return await MemeLibrary.search(query_vector, folder, top_k, score_threshold)


# ── Qdrant 操作辅助函数 ──

MEME_COLLECTION_NAME = "ai_meme"


async def _ensure_meme_collection() -> None:
    """确保 ai_meme Collection 存在，并在嵌入维度变化时基于数据库记录重建索引。"""
    from gsuid_core.ai_core.rag.base import client, get_strict_dimension
    from gsuid_core.ai_core.rag.collection_migration import force_recreate_collection, collection_vector_mismatched

    if client is None:
        return

    dimension = get_strict_dimension()
    existing = {c.name for c in (await client.get_collections()).collections}
    should_reindex = False

    if MEME_COLLECTION_NAME in existing:
        if await collection_vector_mismatched(MEME_COLLECTION_NAME, dimension):
            logger.warning(f"[Meme] Collection {MEME_COLLECTION_NAME} 维度变化，强制重建后基于数据库记录重建索引")
            should_reindex = True
        else:
            if await _meme_collection_needs_recovery():
                logger.warning(
                    f"[Meme] Collection {MEME_COLLECTION_NAME} 疑似上次迁移/同步未完成，强制重建后从数据库恢复索引"
                )
                should_reindex = True
            else:
                # 既有 Collection 补建 payload 索引（幂等），覆盖历史部署缺少 meme_id 索引的情况
                from qdrant_client.models import PayloadSchemaType

                for field_name in ("folder", "status", "meme_id"):
                    try:
                        await client.create_payload_index(
                            collection_name=MEME_COLLECTION_NAME,
                            field_name=field_name,
                            field_schema=PayloadSchemaType.KEYWORD,
                        )
                    except Exception as e:
                        # 索引已存在或后端不支持，幂等场景下属预期
                        logger.debug(f"[Meme] 跳过 payload 索引 {field_name}: {e}")
                return

    if MEME_COLLECTION_NAME not in existing or should_reindex:
        from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

        await force_recreate_collection(
            collection_name=MEME_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=dimension,
                distance=Distance.COSINE,
                on_disk=True,
            ),
            on_disk_payload=True,
        )
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError("Qdrant client 重建后不可用")
        # 为 folder / status / meme_id 建立 payload 索引
        # （meme_id 索引用于幂等 upsert 前按 meme_id 删点，避免全量扫描）
        for field_name in ("folder", "status", "meme_id"):
            await refreshed_client.create_payload_index(
                collection_name=MEME_COLLECTION_NAME,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        logger.info(f"[Meme] 创建 Qdrant Collection: {MEME_COLLECTION_NAME}, 维度: {dimension}")

    if should_reindex:
        await _reindex_meme_collection_from_db()


async def _eligible_meme_records() -> list[AiMemeRecord]:
    """返回可写入向量索引的表情包记录。"""
    records = await AiMemeRecord.get_all_records_no_page()
    return [record for record in records if record.status in {"tagged", "manual"}]


async def _meme_collection_needs_recovery() -> bool:
    """检测 Meme Collection 是否可能处于上次迁移/同步失败后的不完整状态。"""
    from gsuid_core.ai_core.rag.collection_migration import count_collection_points

    records = await _eligible_meme_records()
    if not records:
        return False
    point_count = await count_collection_points(MEME_COLLECTION_NAME)
    return point_count < len(records)


async def _reindex_meme_collection_from_db() -> None:
    """基于 AiMemeRecord 数据库记录重建表情包向量索引。"""
    records = await _eligible_meme_records()
    restored = 0
    skipped = 0
    for record in records:
        content = f"{record.description} {' '.join(record.all_tags)}".strip()
        if not content:
            skipped += 1
            continue
        try:
            await MemeLibrary.sync_to_qdrant(record)
            restored += 1
        except Exception as e:
            skipped += 1
            logger.warning(f"[Meme] 重建表情包向量索引失败，已跳过 {record.meme_id}: {e}")

    logger.info(f"[Meme] 维度迁移重建索引完成: {restored} 条，跳过 {skipped} 条")
    if records and restored == 0:
        raise RuntimeError("Meme 维度迁移未恢复任何索引，保留重试状态并等待下次启动继续恢复")


async def _embed_text(text: str) -> Optional[List[float]]:
    """将文本编码为向量"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        return None
    return await embedding_provider.embed_single(text)


async def _force_recreate_meme_collection() -> None:
    """强制重建表情包 Collection，用于本地 Qdrant 旧维度 ndarray 残留自恢复。"""
    from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

    from gsuid_core.ai_core.rag.base import client, get_strict_dimension
    from gsuid_core.ai_core.rag.collection_migration import force_recreate_collection

    if client is None:
        return

    dimension = get_strict_dimension()
    await force_recreate_collection(
        collection_name=MEME_COLLECTION_NAME,
        vectors_config=VectorParams(
            size=dimension,
            distance=Distance.COSINE,
            on_disk=True,
        ),
        on_disk_payload=True,
    )
    from gsuid_core.ai_core.rag.base import client as refreshed_client

    if refreshed_client is None:
        raise RuntimeError("Qdrant client 重建后不可用")
    for field_name in ("folder", "status", "meme_id"):
        await refreshed_client.create_payload_index(
            collection_name=MEME_COLLECTION_NAME,
            field_name=field_name,
            field_schema=PayloadSchemaType.KEYWORD,
        )
    logger.info(f"[Meme] 已强制重建 Qdrant Collection: {MEME_COLLECTION_NAME}, 维度: {dimension}")


async def _upsert_to_qdrant(
    meme_id: str,
    content: str,
    folder: str,
    persona_hint: str,
    status: str,
    use_count: int,
    file_mime: str,
) -> Optional[str]:
    """写入或更新 Qdrant 向量

    point id 由 meme_id 派生（UUID5），保证多次 sync 幂等覆盖同一个点，
    不会随每次编辑/重打标产生重复向量点。upsert 前先按 meme_id 清理
    旧点，自愈历史版本随机 UUID 留下的重复点。
    """
    from qdrant_client.models import PointStruct

    from gsuid_core.ai_core.rag.base import client, get_point_id

    if client is None:
        return None

    vector = await _embed_text(content)
    if vector is None:
        return None

    # 清理该 meme 既有的全部向量点（含历史随机 UUID 重复点），失败不阻塞写入
    try:
        await _remove_from_qdrant(meme_id)
    except Exception as e:
        logger.warning(f"[Meme] 清理旧向量点失败（继续写入）: {meme_id}: {e}")

    point_id = get_point_id(meme_id)
    point = PointStruct(
        id=point_id,
        vector=vector,
        payload={
            "meme_id": meme_id,
            "folder": folder,
            "persona_hint": persona_hint,
            "status": status,
            "use_count": use_count,
            "file_mime": file_mime,
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
        logger.warning(f"[Meme] Qdrant 写入检测到向量维度残留，强制重建 Collection 后重试: {e}")
        await _force_recreate_meme_collection()
        from gsuid_core.ai_core.rag.base import client as refreshed_client

        if refreshed_client is None:
            raise RuntimeError("Qdrant client 重建后不可用")
        await refreshed_client.upsert(
            collection_name=MEME_COLLECTION_NAME,
            points=[point],
        )
    return point_id


async def _search_qdrant(
    query_vector: List[float],
    folder: Optional[str],
    top_k: int,
    score_threshold: Optional[float] = None,
) -> List[str]:
    """在 Qdrant 中搜索相似向量，返回 meme_id 列表

    Args:
        query_vector: 查询向量
        folder: 可选的文件夹过滤
        top_k: 返回数量
        score_threshold: 最低相似度阈值，低于此值的结果将被过滤

    Returns:
        匹配的 meme_id 列表
    """
    from qdrant_client.models import Filter, MatchAny, MatchValue, FieldCondition

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    # status 过滤无条件携带：索引中可能存在非 tagged/manual 的点
    # （导入路径、历史遗留），不能依赖"只有可用状态才会被 sync"这一不变量
    must_conditions = [
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
    query_filter = Filter(must=must_conditions)

    # 如果未指定阈值，从配置中读取
    if score_threshold is None:
        from gsuid_core.ai_core.meme.config import meme_config

        score_threshold = meme_config.get_config("meme_search_threshold").data

    response = await client.query_points(
        collection_name=MEME_COLLECTION_NAME,
        query=query_vector,
        query_filter=query_filter,
        limit=top_k,
        score_threshold=score_threshold,
        with_payload=True,
    )
    return [r.payload["meme_id"] for r in response.points if r.payload and "meme_id" in r.payload]


async def _remove_from_qdrant(meme_id: str) -> None:
    """从 Qdrant 中删除指定 meme_id 的向量"""
    from qdrant_client.models import Filter, MatchValue, FieldCondition

    from gsuid_core.ai_core.rag.base import client

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
