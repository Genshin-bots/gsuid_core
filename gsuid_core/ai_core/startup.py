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

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start


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


# 各 AI 子系统初始化步骤，按依赖顺序排列：
# RAG 先初始化 Embedding 模型，Memory / Meme 依赖其结果；
# MCP Server 依赖 MCP 工具先完成注册。
_INIT_STEPS = [
    ("RAG", _init_rag),
    ("Persona", _init_persona),
    ("定时任务", _init_scheduled_task),
    ("Memory", _init_memory),
    ("MCP 工具", _init_mcp_tools),
    ("Meme", _init_meme),
    ("统计", _init_statistics),
    ("MCP Server", _init_mcp_server),
]


@on_core_start
async def init_ai_core():
    """AI 核心统一初始化（后台执行，不阻塞 WS 启动）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    enable_ai = ai_config.get_config("enable").data
    if not enable_ai:
        logger.info("🧠 [AI Core] AI总开关已关闭，跳过 AI 重依赖导入与子系统初始化")
        return

    start = time.time()
    logger.info("🧠 [AI Core] 开始后台初始化 AI 核心...")

    # 触发 AI 重依赖导入（sklearn / sentence-transformers / buildin_tools 等）。
    # 放到独立线程执行，避免同步 import 冻住事件循环、阻塞 WS 服务启动。
    import_start = time.time()
    try:
        await asyncio.to_thread(_import_ai_heavy_deps)
    except Exception as e:
        logger.exception(f"🧠 [AI Core] AI 重依赖导入失败, 初始化中止: {e}")
        return

    logger.debug(f"🧠 [AI Core] AI 重依赖导入完成, 耗时: {time.time() - import_start:.2f}秒")

    # 按依赖顺序依次初始化各子系统，单个失败不影响后续步骤
    for name, step in _INIT_STEPS:
        step_start = time.time()
        try:
            await step()
            logger.info(f"🧠 [AI Core] {name} 初始化完成, 耗时: {time.time() - step_start:.2f}秒")
        except Exception as e:
            logger.exception(f"🧠 [AI Core] {name} 初始化失败: {e}")

    logger.success(f"🧠 [AI Core] AI 核心初始化全部完成, 总耗时: {time.time() - start:.2f}秒")
