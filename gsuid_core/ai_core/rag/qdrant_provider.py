"""Qdrant 客户端抽象层。

统一封装本地(嵌入式) / 远程 Qdrant 的构造与切换。所有模块都通过 rag/base.py 暴露的
全局 ``client`` 间接使用本抽象层产出的实例，而 ``client`` 由 ``build_qdrant_client()``
依据配置创建——这是本仓库内 **唯一** 决定连接本地还是远程 Qdrant 的地方。

切换 provider(local <-> remote) 时，由 ``migrate_qdrant_if_provider_changed()`` 在启动阶段
把旧后端的全部 Collection 数据复制到新后端（保留旧后端原始数据，不做删除）。
远程连接信息(url/api_key)始终保存在 ``qdrant_configs.json`` 中，因此无论当前 provider 是
local 还是 remote，都能据此构造出对应方向的源/目标客户端完成迁移。
"""

import gc
import json
import asyncio
from pathlib import Path

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Vector,
    PointStruct,
    VectorStruct,
    VectorStructOutput,
)
from qdrant_client.local.async_qdrant_local import AsyncQdrantLocal

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH
from gsuid_core.ai_core.configs.ai_config import ai_config, qdrant_config

PROVIDER_LOCAL = "local"
PROVIDER_REMOTE = "remote"

# 本地嵌入式 Qdrant 数据目录（与历史路径保持一致，避免老用户数据丢失）
LOCAL_QDRANT_DB_PATH: Path = AI_CORE_PATH / "local_qdrant_db"
# 记录上次实际使用的 Qdrant 后端，启动时据此判断是否需要迁移
_PROVIDER_STATE_FILE: Path = AI_CORE_PATH / "qdrant_provider_state.json"

# 迁移时每批滚动/写入的点数
_MIGRATION_BATCH_SIZE = 256


def get_qdrant_provider() -> str:
    """读取当前配置的 Qdrant 部署方式，非法值回退到 local。"""
    provider = str(ai_config.get_config("qdrant_provider").data or "").strip().lower()
    return provider if provider in (PROVIDER_LOCAL, PROVIDER_REMOTE) else PROVIDER_LOCAL


def get_remote_connection() -> tuple[str, str | None]:
    """读取远程 Qdrant 连接信息 (url, api_key)。api_key 为空时返回 None。"""
    url = str(qdrant_config.get_config("url").data or "").strip()
    api_key = str(qdrant_config.get_config("api_key").data or "").strip()
    return url, (api_key or None)


def _get_effective_provider(provider: str) -> str:
    """返回 provider 实际生效的后端类型。

    remote 配置但缺少 url 时会回退到 local，因此 effective provider 可能与配置值不同。
    """
    if provider == PROVIDER_REMOTE:
        url, _ = get_remote_connection()
        if not url:
            return PROVIDER_LOCAL
        return PROVIDER_REMOTE
    return PROVIDER_LOCAL


def build_qdrant_client(provider: str | None = None) -> AsyncQdrantClient:
    """按 provider 构造 AsyncQdrantClient。

    provider 为 None 时读取当前配置。remote 但未配置 url 时回退到本地嵌入式 Qdrant，
    避免误连空地址导致整个 AI 子系统不可用。

    当请求的实际后端与当前全局 client 一致且全局 client 已初始化时，直接复用全局实例，
    避免在同一进程内对同一个本地目录创建多个 Qdrant client 导致文件锁冲突。
    """
    if provider is None:
        provider = get_qdrant_provider()

    effective = _get_effective_provider(provider)
    current_effective = _get_effective_provider(get_qdrant_provider())

    # 复用全局 client：同一实际后端且全局 client 已存在时直接返回，
    # 防止两个 AsyncQdrantClient 同时持有同一个本地目录的文件锁。
    if effective == current_effective:
        import gsuid_core.ai_core.rag.base as rag_base

        if rag_base.client is not None:
            return rag_base.client

    if provider == PROVIDER_REMOTE:
        url, api_key = get_remote_connection()
        if not url:
            logger.warning(t("🧠 [Qdrant] 已选择远程模式但未配置 url，回退到本地嵌入式 Qdrant"))
            return AsyncQdrantClient(path=str(LOCAL_QDRANT_DB_PATH))
        logger.info(t("🧠 [Qdrant] 使用远程 Qdrant 服务: {url}", url=url))
        # 默认 5s 在启动高负载窗口会对瞬时调用误报 ReadTimeout，
        # 导致 RAG 步骤判失败、进程"暂不接收 AI 会话"；放宽容忍启动尖峰。
        return AsyncQdrantClient(url=url, api_key=api_key, timeout=30)

    logger.info(
        t("🧠 [Qdrant] 使用本地嵌入式 Qdrant: {LOCAL_QDRANT_DB_PATH}", LOCAL_QDRANT_DB_PATH=LOCAL_QDRANT_DB_PATH)
    )
    return AsyncQdrantClient(path=str(LOCAL_QDRANT_DB_PATH))


# ============== provider 切换状态持久化 ==============
def _write_provider_state(provider: str) -> None:
    try:
        _PROVIDER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROVIDER_STATE_FILE.write_text(
            json.dumps({"provider": provider}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning(t("🧠 [Qdrant] 写入 provider 状态文件失败: {e}", e=e))


def get_last_provider() -> str:
    """上次实际使用的 Qdrant 后端。

    无状态文件时(老用户升级)历史上一律是本地嵌入式 Qdrant，因此默认 local。
    仅捕获文件读取(OSError)与 JSON 解析(ValueError, JSONDecodeError 是其子类)异常，
    这是对外部持久化文件损坏的运维兜底，而非压制类型/属性错误。
    """
    if not _PROVIDER_STATE_FILE.exists():
        return PROVIDER_LOCAL
    try:
        data = json.loads(_PROVIDER_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(t("🧠 [Qdrant] 读取 provider 状态文件失败: {e}", e=e))
        return PROVIDER_LOCAL
    raw = data["provider"] if isinstance(data, dict) and "provider" in data else ""
    last = str(raw).strip().lower()
    return last if last in (PROVIDER_LOCAL, PROVIDER_REMOTE) else PROVIDER_LOCAL


# ============== provider 切换时的数据迁移 ==============
def _to_input_vector(vector: VectorStructOutput) -> VectorStruct:
    """把 scroll 返回的输出向量(VectorStructOutput)适配为 upsert 入参向量(VectorStruct)。

    两者运行时结构一致；仅因 qdrant-client 将输出/输入模型分离、且命名向量 dict 的值类型不变
    (invariant)，需把 dict[str, VectorOutput] 显式重建为入参的 dict[str, Vector]。
    """
    if isinstance(vector, dict):
        named: dict[str, Vector] = {name: vec for name, vec in vector.items()}
        return named
    return vector


async def _copy_collection(source: AsyncQdrantClient, target: AsyncQdrantClient, name: str) -> int:
    """把单个 Collection 从 source 复制到 target（含向量/payload/索引），返回复制点数。

    目标已存在该 Collection 时不重建，仅 upsert（按 id 幂等覆盖），从而支持中断后重试。
    """
    src_info = await source.get_collection(collection_name=name)

    if not await target.collection_exists(name):
        # CollectionParams.vectors / sparse_vectors / on_disk_payload 均为已声明字段，
        # 直接按其静态类型访问；create_collection 的对应入参也接受 None。
        params = src_info.config.params
        await target.create_collection(
            collection_name=name,
            vectors_config=params.vectors,
            sparse_vectors_config=params.sparse_vectors,
            on_disk_payload=params.on_disk_payload,
        )
        # 复制 payload 索引（如 scope_key 的 KEYWORD 索引），保证检索过滤行为一致。
        # payload_schema 为必填字段(Dict)，PayloadIndexInfo.data_type 也是必填枚举。
        for field_name, schema in src_info.payload_schema.items():
            try:
                await target.create_payload_index(
                    collection_name=name,
                    field_name=field_name,
                    field_schema=schema.data_type,
                )
            except Exception as e:
                logger.warning(
                    t(
                        "🧠 [Qdrant] 迁移 Collection {name} 的 payload 索引 {field_name} 失败: {e}",
                        name=name,
                        field_name=field_name,
                        e=e,
                    )
                )
        logger.info(t("🧠 [Qdrant] 迁移创建 Collection: {name}", name=name))

    copied = 0
    next_offset = None
    while True:
        records, next_offset = await source.scroll(
            collection_name=name,
            limit=_MIGRATION_BATCH_SIZE,
            offset=next_offset,
            with_payload=True,
            with_vectors=True,
        )
        points: list[PointStruct] = []
        for r in records:
            if r.vector is None:
                logger.debug(t("🧠 [Qdrant] Collection {name} point {p0} 无向量，跳过迁移", name=name, p0=r.id))
                continue
            points.append(PointStruct(id=r.id, vector=_to_input_vector(r.vector), payload=r.payload))
        if points:
            await target.upsert(collection_name=name, points=points)
            copied += len(points)
        if next_offset is None:
            break
    return copied


def _is_lock_conflict(e: BaseException) -> bool:
    """判断异常是否为本地嵌入式 Qdrant 的目录文件锁冲突。

    qdrant 在 ``.lock`` 被其它实例占用时抛 RuntimeError，文案含 'already accessed'。
    """
    return isinstance(e, RuntimeError) and "already accessed" in str(e)


async def _build_source_with_lock_retry(
    provider: str,
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
) -> AsyncQdrantClient:
    """构造迁移源客户端；本地源被占用(文件锁冲突)时带退避重试。

    重启窗口内旧进程可能尚未完全退出释放本地锁，给它时间后再试，避免一次锁冲突
    就让整条 RAG 初始化失败。仅对锁冲突重试，其它异常立即抛出。
    """
    last_err: BaseException | None = None
    for i in range(attempts):
        try:
            return build_qdrant_client(provider)
        except RuntimeError as e:
            if not _is_lock_conflict(e):
                raise
            last_err = e
            if i < attempts - 1:
                delay = base_delay * (i + 1)
                logger.warning(
                    t(
                        "🧠 [Qdrant] 迁移源(本地)被其它实例占用，{delay:.0f}s 后重试 ({p0}/{attempts}): {e}",
                        delay=delay,
                        p0=i + 1,
                        attempts=attempts,
                        e=e,
                    )
                )
                await asyncio.sleep(delay)
    assert last_err is not None
    raise last_err


def _release_qdrant_client_memory(client: AsyncQdrantClient) -> None:
    """主动断开本地嵌入式 Qdrant client 载入内存的 collection 引用，便于 GC 回收。

    AsyncQdrantLocal.close() 只释放文件锁与 sqlite 句柄，不会清空其 collections——
    其中的 numpy 向量/payload(大库可达数百 MB~GB)会一直驻留到 client 对象被回收，
    且 client 内部存在引用环，仅靠作用域退出的引用计数无法及时释放。这里清空内部容器，
    配合 gc.collect() 尽快回收内存。远程 client 的内部实现不是 AsyncQdrantLocal，跳过。
    """
    inner = client._client
    if isinstance(inner, AsyncQdrantLocal):
        inner.collections.clear()
        inner.aliases.clear()


async def migrate_qdrant_if_provider_changed() -> None:
    """检测 Qdrant 后端是否发生切换，若是则把历史数据迁移到新后端（保留旧后端数据）。

    需在 ``init_embedding_model()`` 之后调用——此时全局 ``client`` 已指向新后端(target)，
    本函数再按 ``get_last_provider()`` 构造旧后端(source) 完成复制。
    """
    current = get_qdrant_provider()
    last = get_last_provider()

    if current == last:
        return

    # 目标是 remote 但 url 缺失：build_qdrant_client 实际回退到了本地，等同未切换，跳过。
    if current == PROVIDER_REMOTE:
        url, _ = get_remote_connection()
        if not url:
            logger.warning(t("🧠 [Qdrant] qdrant_provider=remote 但未配置 url，实际仍使用本地，跳过迁移"))
            return

    # 源是 remote 但 url 缺失：无法连接旧后端读取历史数据，放弃迁移并对齐状态避免反复尝试。
    if last == PROVIDER_REMOTE:
        url, _ = get_remote_connection()
        if not url:
            logger.warning(t("🧠 [Qdrant] 旧后端为 remote 但缺少连接配置，无法迁移历史数据，已对齐状态"))
            _write_provider_state(current)
            return

    import gsuid_core.ai_core.rag.base as rag_base

    target = rag_base.client
    if target is None:
        logger.warning(t("🧠 [Qdrant] 全局 client 尚未初始化，跳过本次迁移"))
        return

    logger.info(
        t(
            "🧠 [Qdrant] 检测到向量库后端切换: {last} -> {current}，开始迁移历史数据(保留原数据)...",
            last=last,
            current=current,
        )
    )
    try:
        source = await _build_source_with_lock_retry(last)
    except RuntimeError as e:
        if _is_lock_conflict(e):
            logger.error(
                t(
                    "🧠 [Qdrant] 本地向量库目录被另一个 Qdrant 实例占用，无法读取历史数据进行迁移。"
                    "常见原因：仍有第二个 gsuid_core 进程在运行(“重启”只会结束当前进程，不会结束重复实例)。"
                    "本次迁移已跳过(状态未对齐)，处理掉冲突进程后下次启动会自动重试。"
                )
            )
            return
        raise
    failed = False
    total = 0
    try:
        try:
            collections = (await source.get_collections()).collections
        except Exception as e:
            logger.error(t("🧠 [Qdrant] 读取旧后端 Collection 列表失败，迁移中止(下次启动重试): {e}", e=e))
            return

        for col in collections:
            try:
                n = await _copy_collection(source, target, col.name)
                total += n
                logger.info(t("🧠 [Qdrant] Collection {p0} 迁移完成: {n} 条", p0=col.name, n=n))
            except Exception as e:
                failed = True
                logger.error(t("🧠 [Qdrant] Collection {p0} 迁移失败(下次启动重试): {e}", p0=col.name, e=e))

        if failed:
            logger.warning(
                t(
                    "🧠 [Qdrant] 向量库迁移存在失败 Collection，已迁移 {total} 条，状态未对齐，下次启动将重试",
                    total=total,
                )
            )
        else:
            _write_provider_state(current)
            logger.success(
                t("🧠 [Qdrant] 向量库迁移完成: 共 {total} 条，源后端({last})数据已保留", total=total, last=last)
            )
    finally:
        # 释放源客户端：local 源会释放本地文件锁，remote 源会关闭 HTTP 连接。
        # 清理失败不影响迁移结果，仅记录 debug，避免在 finally 中抛出掩盖主流程异常。
        try:
            await source.close()
        except Exception as e:
            logger.debug(t("🧠 [Qdrant] 关闭源客户端时出现异常(可忽略): {e}", e=e))
        # close() 不会清空已载入内存的向量/payload，主动断引用 + GC，
        # 避免本地大库迁移完成后内存仍占用到下次重启才释放。
        _release_qdrant_client_memory(source)
        del source
        gc.collect()
