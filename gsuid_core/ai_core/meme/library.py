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

        # 移动文件
        if old_path.exists():
            await _move_file(old_path, new_path)
        elif not new_path.exists():
            logger.warning(f"[Meme] 源文件不存在: {old_path}")
            return False

        # 更新数据库
        await AiMemeRecord.update_record(
            meme_id,
            {
                "file_path": new_relative_path,
                "folder": target_folder,
            },
        )
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

        # 删除 Qdrant 向量（如果存在）
        if record.qdrant_id:
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
        """通过文本语义搜索表情包

        Args:
            query_text: 查询文本
            folder: 可选的文件夹过滤
            top_k: 返回数量
            score_threshold: 最低相似度阈值，低于此值的结果将被过滤

        Returns:
            匹配的 AiMemeRecord 列表
        """
        query_vector = await _embed_text(query_text)
        if query_vector is None:
            return []
        return await MemeLibrary.search(query_vector, folder, top_k, score_threshold)

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


# ── Qdrant 操作辅助函数 ──

MEME_COLLECTION_NAME = "ai_meme"


async def _ensure_meme_collection() -> None:
    """确保 ai_meme Collection 存在"""
    from gsuid_core.ai_core.rag.base import DIMENSION, client

    if client is None:
        return

    existing = {c.name for c in (await client.get_collections()).collections}
    if MEME_COLLECTION_NAME not in existing:
        from qdrant_client.models import Distance, VectorParams, PayloadSchemaType

        await client.create_collection(
            collection_name=MEME_COLLECTION_NAME,
            vectors_config=VectorParams(
                size=DIMENSION,
                distance=Distance.COSINE,
            ),
        )
        # 为 folder 建立 payload 索引
        await client.create_payload_index(
            collection_name=MEME_COLLECTION_NAME,
            field_name="folder",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        await client.create_payload_index(
            collection_name=MEME_COLLECTION_NAME,
            field_name="status",
            field_schema=PayloadSchemaType.KEYWORD,
        )
        logger.info(f"[Meme] 创建 Qdrant Collection: {MEME_COLLECTION_NAME}")


async def _embed_text(text: str) -> Optional[List[float]]:
    """将文本编码为向量"""
    from gsuid_core.ai_core.rag.base import embedding_provider

    if embedding_provider is None:
        return None
    return await embedding_provider.embed_single(text)


async def _upsert_to_qdrant(
    meme_id: str,
    content: str,
    folder: str,
    persona_hint: str,
    status: str,
    use_count: int,
    file_mime: str,
) -> Optional[str]:
    """写入或更新 Qdrant 向量"""
    import uuid

    from qdrant_client.models import PointStruct

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return None

    vector = await _embed_text(content)
    if vector is None:
        return None

    point_id = str(uuid.uuid4())
    await client.upsert(
        collection_name=MEME_COLLECTION_NAME,
        points=[
            PointStruct(
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
        ],
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
    from qdrant_client.models import Filter, MatchValue, FieldCondition

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    query_filter = None
    if folder:
        query_filter = Filter(
            must=[
                FieldCondition(
                    key="folder",
                    match=MatchValue(value=folder),
                ),
                FieldCondition(
                    key="status",
                    match=MatchValue(value="tagged"),
                ),
            ]
        )

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
