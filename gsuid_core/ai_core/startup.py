"""AI 核心统一初始化入口。

历史上各 AI 子系统（RAG / Persona / Memory / Meme / MCP / 统计 / 定时任务）
各自通过 @on_core_start 注册启动钩子，并在同步插件加载阶段被 import，
导致 sklearn / sentence-transformers / buildin_tools 等重依赖在 WS 服务
启动前被同步加载，阻塞启动十余秒。

现改为：
- 各子系统的 startup 模块不再注册 on_core_start，仅提供普通的初始化函数；
- 由本模块注册唯一的 on_core_start 钩子 init_ai_core()，在 WS 服务启动后的
  后台阶段统一触发重依赖导入，并按依赖顺序调用各子系统的初始化函数。

这样既不阻塞 WS 启动，也让所有 AI 初始化逻辑集中在一处，便于维护：
新增 AI 子系统时，只需在 _INIT_STEPS 中追加一项即可。
"""

import time
import asyncio
from typing import Optional

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start, on_core_shutdown


def _import_ai_heavy_deps():
    """同步触发 AI 重依赖导入（sklearn / sentence-transformers / buildin_tools 等）。

    这些 import 是 CPU 密集的同步操作，放在事件循环线程里会冻住 loop，
    导致 uvicorn 无法绑定 socket。由 init_ai_core 通过 to_thread 调用。
    """
    import gsuid_core.ai_core.handle_ai  # noqa: F401
    import gsuid_core.ai_core.buildin_tools  # noqa: F401


async def _init_rag():
    from gsuid_core.ai_core.rag.startup import init_all

    await init_all()


async def _init_persona():
    from gsuid_core.ai_core.persona.startup import init_default_personas

    await init_default_personas()


async def _init_scheduled_task():
    from gsuid_core.ai_core.scheduled_task.startup import init_scheduled_tasks

    await init_scheduled_tasks()


async def _init_planning():
    from gsuid_core.ai_core.planning.startup import init_planning

    await init_planning()


async def _init_memory():
    from gsuid_core.ai_core.memory.startup import init_memory_system

    await init_memory_system()


async def _init_mcp_tools():
    from gsuid_core.ai_core.mcp.startup import init_mcp_tools

    await init_mcp_tools()


async def _init_meme():
    from gsuid_core.ai_core.meme.startup import init_meme_module

    await init_meme_module()


async def _init_statistics():
    from gsuid_core.ai_core.statistics.startup import init_ai_core_statistics

    await init_ai_core_statistics()


async def _init_mcp_server():
    from gsuid_core.ai_core.mcp.server import init_mcp_server

    await init_mcp_server()


_AI_CORE_READY = False
_AI_CORE_INITIALIZING = False
_AI_CORE_READY_EVENT: Optional[asyncio.Event] = None


def _get_ready_event() -> asyncio.Event:
    global _AI_CORE_READY_EVENT
    if _AI_CORE_READY_EVENT is None:
        _AI_CORE_READY_EVENT = asyncio.Event()
    return _AI_CORE_READY_EVENT


def is_ai_core_ready() -> bool:
    """AI 核心是否已完成启动初始化。"""
    return _AI_CORE_READY


def is_ai_core_initializing() -> bool:
    """AI 核心是否仍在启动初始化/迁移中。"""
    return _AI_CORE_INITIALIZING


async def wait_ai_core_ready(timeout: float = 300.0) -> bool:
    """等待 AI 核心启动初始化完成，避免迁移期间处理聊天触发旧向量查询。"""
    if _AI_CORE_READY:
        return True
    if not _AI_CORE_INITIALIZING:
        return False
    try:
        await asyncio.wait_for(_get_ready_event().wait(), timeout=timeout)
        return _AI_CORE_READY
    except asyncio.TimeoutError:
        return False


# 各 AI 子系统初始化步骤，按依赖顺序排列：
# RAG 先初始化 Embedding 模型，Memory / Meme 依赖其结果；
# MCP Server 依赖 MCP 工具先完成注册。
_INIT_STEPS = [
    ("RAG", _init_rag),
    ("Persona", _init_persona),
    ("定时任务", _init_scheduled_task),
    ("长任务编排", _init_planning),
    ("Memory", _init_memory),
    ("MCP 工具", _init_mcp_tools),
    ("Meme", _init_meme),
    ("统计", _init_statistics),
    ("MCP Server", _init_mcp_server),
]


@on_core_start
async def init_ai_core():
    """AI 核心统一初始化（后台执行，不阻塞 WS 启动）。"""
    global _AI_CORE_READY, _AI_CORE_INITIALIZING

    # 防止 on_core_start 多次触发导致并发初始化：两条流水线会让 RAG / Memory 同时
    # 初始化同一个本地 Qdrant 并并发写入集合，触发文件锁冲突
    # "Storage folder ... is already accessed by another instance of Qdrant client"。
    # 下面的状态判断与 _AI_CORE_INITIALIZING 置位之间不存在 await，asyncio 协作式调度下
    # 是原子的；后到的协程会在首个 await 让出后看到标记并直接退出，从而保证整条初始化串行。
    if _AI_CORE_READY or _AI_CORE_INITIALIZING:
        logger.debug("🧠 [AI Core] 初始化已在进行或已完成，跳过本次重复触发")
        return

    from gsuid_core.ai_core.configs.ai_config import ai_config

    enable_ai = ai_config.get_config("enable").data
    if not enable_ai:
        _AI_CORE_READY = True
        _AI_CORE_INITIALIZING = False
        _get_ready_event().set()
        logger.info("🧠 [AI Core] AI总开关已关闭，跳过 AI 重依赖导入与子系统初始化")
        return

    _AI_CORE_READY = False
    _AI_CORE_INITIALIZING = True
    _get_ready_event().clear()
    start = time.time()
    logger.info("🧠 [AI Core] 开始后台初始化 AI 核心...")

    # 触发 AI 重依赖导入（sklearn / sentence-transformers / buildin_tools 等）。
    # 放到独立线程执行，避免同步 import 冻住事件循环、阻塞 WS 服务启动。
    import_start = time.time()
    try:
        await asyncio.to_thread(_import_ai_heavy_deps)
    except Exception as e:
        logger.exception(f"🧠 [AI Core] AI 重依赖导入失败, 初始化中止: {e}")
        _AI_CORE_INITIALIZING = False
        _get_ready_event().set()
        return

    logger.debug(f"🧠 [AI Core] AI 重依赖导入完成, 耗时: {time.time() - import_start:.2f}秒")

    # 按依赖顺序依次初始化各子系统，单个失败不影响后续步骤；但 AI Core 只有全部步骤成功才标记 ready。
    init_failed = False
    try:
        for name, step in _INIT_STEPS:
            step_start = time.time()
            try:
                logger.info(f"🧠 [AI Core] 开始初始化 {name}...")
                step_task = asyncio.create_task(step())
                while not step_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(step_task), timeout=60.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"🧠 [AI Core] {name} 初始化仍在执行中，已耗时: {time.time() - step_start:.2f}秒"
                        )
                await step_task
                logger.info(f"🧠 [AI Core] {name} 初始化完成, 耗时: {time.time() - step_start:.2f}秒")
            except Exception as e:
                init_failed = True
                logger.exception(f"🧠 [AI Core] {name} 初始化失败: {e}")

        if init_failed:
            logger.warning(
                f"🧠 [AI Core] AI 核心初始化存在失败步骤，总耗时: {time.time() - start:.2f}秒，暂不接收 AI 会话"
            )
        else:
            logger.success(f"🧠 [AI Core] AI 核心初始化全部完成, 总耗时: {time.time() - start:.2f}秒")
    finally:
        _AI_CORE_READY = not init_failed
        _AI_CORE_INITIALIZING = False
        _get_ready_event().set()


@on_core_shutdown
async def flush_ai_sessions_on_shutdown() -> None:
    """框架关闭时，强制把所有活跃 AI 会话的日志落盘。

    依赖 `AISessionRegistry.shutdown_all()` 逐个调用 logger.close()，
    确保未达到 10 分钟兜底间隔的会话也能保留完整日志。
    """
    try:
        from gsuid_core.ai_core.session_registry import get_ai_session_registry

        registry = get_ai_session_registry()
        await registry.stop_cleanup_loop()
        closed = registry.shutdown_all()
        if closed:
            logger.info(f"📝 [AISessionLogger] 关闭流程已持久化 {closed} 个活跃会话")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"📝 [AISessionLogger] 关闭流程持久化失败: {e}")
