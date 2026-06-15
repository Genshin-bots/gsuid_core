"""Qdrant Collection 维度检查与迁移辅助函数。"""

import json
import inspect
from typing import Any, Optional
from pathlib import Path
from datetime import datetime, timezone

from qdrant_client.models import VectorParams, VectorParamsDiff
from qdrant_client.http.models.models import PayloadSchemaType

from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH

MIGRATION_BACKUP_DIR = AI_CORE_PATH / "migration_backups"

# 维度/向量结构不匹配时，本地 Qdrant(numpy) 或远程服务会抛出的特征错误片段。
# 用相对具体的短语判定，避免裸 "dim" 之类宽泛子串误命中无关错误（如 collection 名里含 dim）。
_VECTOR_STRUCTURE_ERROR_SIGNATURES: tuple[str, ...] = (
    "not aligned",  # numpy 矩阵乘法 shape 不匹配
    "broadcast input array",  # numpy 本地维度残留
    "vector dimension",  # qdrant: Vector dimension error
    "dimension error",
    "wrong vector size",
    "wrong input: vector",
    "expected dim",
    "dense vector",  # named vector 结构缺失，如 "Dense vector dense is not found"
    # 远程 Qdrant 缺失 payload 索引时拒绝 Filter：
    # 与维度/结构异常同类，运维配置缺失而非代码 bug
    "index required but not found",
)


def is_vector_structure_error(message: str) -> bool:
    """判断异常信息是否为向量维度/结构不匹配，用于触发集合重建或检索降级。

    仅匹配较具体的错误特征短语，避免裸 "dim" 误命中无关异常导致不必要的删库重建。
    """
    if not message:
        return False
    lowered = message.lower()
    return any(sig in lowered for sig in _VECTOR_STRUCTURE_ERROR_SIGNATURES)


async def scroll_all_payloads(collection_name: str) -> list[tuple[Any, dict[str, Any]]]:
    """滚动导出 Collection 中的全部 payload。

    仅导出 point id 与 payload，不导出旧向量；切换嵌入模型后旧向量不可复用，
    迁移时应基于 payload 重新生成向量。
    """
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return []

    items: list[tuple[Any, dict[str, Any]]] = []
    next_page_offset = None

    while True:
        records, next_page_offset = await client.scroll(
            collection_name=collection_name,
            limit=256,
            offset=next_page_offset,
            with_payload=True,
            with_vectors=False,
        )
        for record in records:
            if record.payload is not None:
                items.append((record.id, dict(record.payload)))
            else:
                logger.warning(
                    f"🧠 [Migration] Collection {collection_name} 中 point_id={record.id} 的 payload 为 None，已跳过"
                )
        if next_page_offset is None:
            break

    return items


def _json_default(value: Any) -> str:
    """将 Qdrant payload 中 JSON 不直接支持的值转为字符串。"""
    return str(value)


async def save_payload_backup(collection_name: str, payloads: list[tuple[Any, dict[str, Any]]]) -> Path:
    """在删除 Collection 前将 payload 备份落盘，降低迁移中断时的数据丢失风险。"""
    MIGRATION_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = collection_name.replace("/", "_").replace("\\", "_")
    backup_path = MIGRATION_BACKUP_DIR / f"{safe_name}_{timestamp}.json"
    data = {
        "collection_name": collection_name,
        "created_at": timestamp,
        "payloads": [{"id": str(point_id), "payload": payload} for point_id, payload in payloads],
    }
    backup_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    logger.warning(f"🧠 [Migration] Collection {collection_name} 迁移 payload 已备份到: {backup_path}")
    return backup_path


def remove_payload_backup(backup_path: Optional[Path], collection_name: str) -> None:
    """迁移成功后删除落盘备份；删除失败只记录警告。"""
    if backup_path is None:
        return
    try:
        backup_path.unlink(missing_ok=True)
        logger.info(f"🧠 [Migration] Collection {collection_name} 迁移成功，已删除备份: {backup_path}")
    except Exception as e:
        logger.warning(f"🧠 [Migration] Collection {collection_name} 迁移备份删除失败，请手动确认: {backup_path}, {e}")


def find_latest_payload_backup(collection_name: str) -> Optional[Path]:
    """查找指定 Collection 最新的迁移 payload 备份。"""
    safe_name = collection_name.replace("/", "_").replace("\\", "_")
    if not MIGRATION_BACKUP_DIR.exists():
        return None
    candidates = sorted(MIGRATION_BACKUP_DIR.glob(f"{safe_name}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def load_payload_backup(backup_path: Path, collection_name: str) -> list[tuple[Any, dict[str, Any]]]:
    """读取迁移 payload 备份；格式异常时返回空列表。"""
    try:
        data = json.loads(backup_path.read_text(encoding="utf-8"))
        if data.get("collection_name") != collection_name:
            logger.warning(f"🧠 [Migration] 备份文件 Collection 不匹配，已忽略: {backup_path}")
            return []
        payloads = data.get("payloads", [])
        if not isinstance(payloads, list):
            return []
        result: list[tuple[Any, dict[str, Any]]] = []
        for item in payloads:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload")
            if isinstance(payload, dict):
                result.append((item.get("id"), payload))
        return result
    except Exception as e:
        logger.warning(f"🧠 [Migration] 读取迁移备份失败，已忽略: {backup_path}, {e}")
        return []


async def count_collection_points(collection_name: str) -> int:
    """统计 Collection point 数量；失败时返回 0 以触发保守恢复。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return 0
    try:
        result = client.count(collection_name=collection_name, exact=True)
        if inspect.isawaitable(result):
            result = await result
        return int(getattr(result, "count", 0) or 0)
    except Exception as e:
        logger.warning(f"🧠 [Migration] 统计 Collection {collection_name} point 数量失败: {e}")
        return 0


def get_vector_params(vectors_config: Any, vector_name: Optional[str] = None) -> Optional[VectorParams]:
    """从 Qdrant vectors_config 中取出目标 VectorParams。"""
    if isinstance(vectors_config, VectorParams):
        return vectors_config if vector_name in (None, "", "default") else None

    if isinstance(vectors_config, dict):
        if vector_name is None:
            if len(vectors_config) != 1:
                return None
            candidate = next(iter(vectors_config.values()))
        else:
            candidate = vectors_config.get(vector_name)
        if isinstance(candidate, VectorParams):
            return candidate
        # qdrant-client 在部分版本中可能返回 pydantic model 或普通 dict，避免 isinstance 过窄导致误判。
        if isinstance(candidate, dict):
            return candidate  # type: ignore[return-value]
        if candidate is not None and hasattr(candidate, "size"):
            return candidate  # type: ignore[return-value]

    # qdrant-client 本地模式的命名向量配置在部分版本中不是原生 dict，而是带属性的模型对象。
    # 例如 vectors_config.name_dense / vectors_config.summary_dense 中才包含 VectorParams。
    if vector_name:
        candidate = getattr(vectors_config, vector_name, None)
        if isinstance(candidate, VectorParams):
            return candidate
        if isinstance(candidate, dict):
            return candidate  # type: ignore[return-value]
        if candidate is not None and hasattr(candidate, "size"):
            return candidate  # type: ignore[return-value]

    return None


def get_vector_size(vectors_config: Any, vector_name: Optional[str] = None) -> Optional[int]:
    """读取目标向量的维度。"""
    params = get_vector_params(vectors_config, vector_name)
    if params is None:
        return None
    size = params.get("size") if isinstance(params, dict) else getattr(params, "size", None)
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


def is_vector_size_matched(vectors_config: Any, expected_size: int, vector_name: Optional[str] = None) -> bool:
    """判断目标向量维度是否匹配当前嵌入模型。"""
    return get_vector_size(vectors_config, vector_name) == expected_size


async def force_recreate_collection(
    collection_name: str,
    vectors_config: Any,
    sparse_vectors_config: Optional[Any] = None,
    on_disk_payload: bool = True,
) -> None:
    """强制重建 Collection（删除旧集合后按当前嵌入维度重新创建）。

    本地 Qdrant 把每个 Collection 的数据存放在独立目录下的 storage.sqlite，并通过常驻
    sqlite 连接持有文件句柄；所有 Collection 的元信息集中记录在库根的 meta.json。维度迁移
    时若直接 shutil.rmtree 集合目录，Windows 上会因 sqlite 文件被占用而静默失败
    (rmtree ignore_errors)，残留的旧维度 storage.sqlite 会被新集合复用，导致后续 768 维
    向量继续写入 512 维存储。因此本地模式下先显式关闭目标 Collection 的 sqlite 句柄，再走
    delete_collection 删除（会同步更新 meta.json 与内存集合表并真正删除目录），最后重新创建。

    注意：绝不关闭或替换全局 client。其它模块通过 `from ...base import client` 持有的是
    同一对象引用，一旦关闭/替换：1) 这些引用会指向已关闭实例（"QdrantLocal instance is
    closed"）；2) 重建 client 会从 meta.json 重新加载旧集合，使 create_collection 抛出
    "Collection already exists"。
    """
    import gsuid_core.ai_core.rag.base as rag_base

    client = rag_base.client
    if client is None:
        return

    kwargs = {
        "collection_name": collection_name,
        "vectors_config": vectors_config,
        "on_disk_payload": on_disk_payload,
    }
    if sparse_vectors_config is not None:
        kwargs["sparse_vectors_config"] = sparse_vectors_config

    inner = getattr(client, "_client", None)
    collections = getattr(inner, "collections", None)
    is_local_client = isinstance(collections, dict)

    if is_local_client:
        # 关闭目标 Collection 的 sqlite 句柄，确保随后 delete_collection 内部的 rmtree 能
        # 真正删除旧维度存储（尤其是 Windows 文件锁场景），避免新集合复用旧 storage.sqlite。
        existing_collection = collections.get(collection_name)
        if existing_collection is not None:
            close_collection = getattr(existing_collection, "close", None)
            if callable(close_collection):
                try:
                    close_collection()
                except Exception as e:
                    logger.warning(f"🧠 [Qdrant] 关闭 Collection {collection_name} 存储句柄失败: {e}")
        if await client.collection_exists(collection_name):
            await client.delete_collection(collection_name=collection_name)
        await client.create_collection(**kwargs)
        logger.info(f"🧠 [Qdrant] 已强制重建本地 Collection: {collection_name}")
        return

    recreate = getattr(client, "recreate_collection", None)
    if callable(recreate):
        try:
            result = recreate(**kwargs)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            # 并发重建竞争：另一路已用相同目标配置建好同名集合（同一维度迁移目标），
            # 远程 Qdrant 返回 409 "already exists"。集合已存在即视为成功，避免启动因竞态崩溃；
            # 其它错误照常抛出。
            if "already exists" in str(e).lower() and await client.collection_exists(collection_name):
                logger.warning(f"🧠 [Qdrant] Collection {collection_name} 已被并发重建创建，忽略 409 冲突")
                return
            raise
        logger.info(f"🧠 [Qdrant] 已强制重建 Collection: {collection_name}")
        return

    if await client.collection_exists(collection_name):
        await client.delete_collection(collection_name=collection_name)
    await client.create_collection(**kwargs)
    logger.info(f"🧠 [Qdrant] 已删除并重建 Collection: {collection_name}")


async def ensure_vector_on_disk(collection_name: str, vector_name: Optional[str] = None) -> None:
    """确保目标向量启用 on_disk；不支持时仅记录警告。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    try:
        col_info = await client.get_collection(collection_name=collection_name)
        vectors_config = col_info.config.params.vectors
        params = get_vector_params(vectors_config, vector_name)
        if params is None or getattr(params, "on_disk", False):
            return

        diff_key = "" if vector_name in (None, "", "default") else vector_name
        logger.info(f"🧠 [Qdrant] 迁移集合 {collection_name} 向量到磁盘存储...")
        await client.update_collection(
            collection_name=collection_name,
            vectors_config={diff_key: VectorParamsDiff(on_disk=True)},
        )
        logger.info(f"🧠 [Qdrant] 集合 {collection_name} on_disk 迁移完成")
    except Exception as e:
        logger.warning(f"🧠 [Qdrant] 检查/迁移集合 {collection_name} on_disk 配置失败: {e}")


async def ensure_payload_indexes(
    collection_name: str,
    keyword_fields: list[str],
) -> None:
    """确保指定 Collection 上的关键字段已创建 keyword 类型的 payload 索引。

    远程 Qdrant 服务在使用 Filter 按 payload 字段过滤时，要求该字段必须存在对应类型的
    索引。远程索引检查/创建失败时必须中断初始化，避免后续查询阶段才抛出 400；本地嵌入式
    Qdrant 不强制要求 payload 索引，因此本地失败仅记录警告。
    """
    from qdrant_client.local.async_qdrant_local import AsyncQdrantLocal

    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return

    is_local_client = isinstance(client._client, AsyncQdrantLocal)

    try:
        col_info = await client.get_collection(collection_name=collection_name)
        existing_indexes = col_info.payload_schema or {}
    except Exception as e:
        message = f"🧠 [Qdrant] 获取集合 {collection_name} 信息失败，无法确认 payload 索引: {e}"
        if is_local_client:
            logger.warning(message)
            return
        raise RuntimeError(message) from e

    for field in keyword_fields:
        if field in existing_indexes:
            continue
        try:
            await client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(f"🧠 [Qdrant] 已为集合 {collection_name} 创建 keyword 索引: {field}")
        except Exception as e:
            message = f"🧠 [Qdrant] 为集合 {collection_name} 创建 keyword 索引 {field} 失败: {e}"
            if is_local_client:
                logger.warning(message)
                continue
            raise RuntimeError(message) from e


async def collection_vector_mismatched(
    collection_name: str, expected_size: int, vector_name: Optional[str] = None
) -> bool:
    """检查 Collection 是否存在维度不一致或缺少目标向量。"""
    from gsuid_core.ai_core.rag.base import client

    if client is None:
        return False

    col_info = await client.get_collection(collection_name=collection_name)
    vectors_config = col_info.config.params.vectors
    actual_size = get_vector_size(vectors_config, vector_name)
    if actual_size == expected_size:
        return False

    logger.warning(
        f"🧠 [Qdrant] Collection {collection_name} 向量维度不匹配: "
        f"actual={actual_size}, expected={expected_size}, vector={vector_name or 'default'}"
    )
    return True
