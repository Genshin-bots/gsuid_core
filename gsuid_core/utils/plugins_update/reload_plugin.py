import sys
import asyncio
import inspect
from types import MethodType, FunctionType
from typing import Optional
from pathlib import Path
from weakref import WeakKeyDictionary
from itertools import groupby
from dataclasses import dataclass

from gsuid_core.sv import SL
from gsuid_core.gss import gss
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.server import GsServer, PluginKindMeta, _DefHook, _module_cache
from gsuid_core.trigger_index import reset_trigger_index


@dataclass
class _StartTaskMeta:
    has_start_hooks: bool
    started: bool = False


@dataclass(frozen=True)
class _ShutdownBatch:
    hooks: list[_DefHook]
    wait_task: asyncio.Task[None] | None = None


# 后台生命周期任务与并发控制表：保证单插件串行重载、保护在途关闭钩子不被取消
_plugin_start_tasks: dict[str, asyncio.Task[None]] = {}
_start_task_meta: dict[asyncio.Task[None], _StartTaskMeta] = {}
_plugin_reload_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()
_active_async_reloads: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, int]] = WeakKeyDictionary()
_active_reload_tasks: set[asyncio.Task[str]] = set()
_plugin_shutdown_tasks: dict[str, asyncio.Task[None]] = {}
_pending_shutdown_hooks: dict[str, list[_ShutdownBatch]] = {}
_RELOAD_SHUTDOWN_TIMEOUT: float = 15.0


def _hook_label(func: object) -> str:
    if isinstance(func, (FunctionType, MethodType)):
        return func.__qualname__
    return repr(func)


def _belongs_to_plugin(module_name: str, plugin_name: str) -> bool:
    """判断某模块名是否属于该插件的模块命名空间, 严格 4 模式, 与 sys.modules 清理保持一致。"""
    return (
        module_name == plugin_name  # 顶层包名
        or module_name.startswith(f"{plugin_name}.")  # 子模块
        or f".{plugin_name}." in module_name  # plugins.MajsoulUID.xxx 形式
        or module_name.endswith(f".{plugin_name}")
    )


def _resolve_func_module(func: object) -> str:
    """取目标可调用对象的 __module__, 依次解包 functools.partial / 绑定方法 / 装饰器包裹。"""
    for attr in (None, "func", "__func__", "__wrapped__"):
        target = func if attr is None else getattr(func, attr, None)
        mod = getattr(target, "__module__", "") or ""
        if mod:
            return mod
    return ""


def _route_owner_module(route: object) -> str:
    """取一条路由的归属模块: Route 看 .endpoint, Mount 看 .app。"""
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None:
        return _resolve_func_module(endpoint)
    return _resolve_func_module(getattr(route, "app", None))


def _clean_plugin_global_state(plugin_name: str) -> list[_DefHook]:
    """第 3.5 步: 清理插件注册到长生命周期全局单例上的状态。

    覆盖 APScheduler 定时任务 + 监听器 / 生命周期 Hook 集合 / FastAPI web 路由+挂载。
    必须在重新 import 之前调用 —— 带固定 id 的定时任务若不先清, 重新注册会撞
    ConflictingIdError 导致整个插件重载失败。返回被摘除的旧 shutdown Hook 供后续收尾。
    """
    logger.debug(i18n_t("log.plugin.gscore_cleanup_global_registration_start", plugin_name=plugin_name))
    shutdown_hooks: list[_DefHook] = []

    # ① APScheduler 定时任务 + 监听器
    try:
        from apscheduler.jobstores.base import JobLookupError

        from gsuid_core.aps import scheduler

        removed_jobs = []
        for job in list(scheduler.get_jobs()):
            mod = _resolve_func_module(getattr(job, "func", None))
            if mod and _belongs_to_plugin(mod, plugin_name):
                try:
                    scheduler.remove_job(job.id)
                    removed_jobs.append(job.id)
                except JobLookupError:
                    pass
                except Exception as e:
                    logger.warning(
                        i18n_t(
                            "log.plugin.gscore_remove_scheduled_task",
                            plugin_name=plugin_name,
                            p0=job.id,
                            e=e,
                        )
                    )
        if removed_jobs:
            logger.info(
                i18n_t(
                    "log.plugin.gscore_scheduled_tasks_name",
                    plugin_name=plugin_name,
                    p0=len(removed_jobs),
                )
            )
            logger.debug(
                i18n_t(
                    "log.plugin.gscore_ids_scheduled_tasks",
                    plugin_name=plugin_name,
                    removed_jobs=removed_jobs,
                )
            )

        # 监听器: _listeners 私有属性仅用于枚举, 移除走公开的 remove_listener
        removed_listeners = 0
        for cb, _mask in list(getattr(scheduler, "_listeners", [])):
            if _belongs_to_plugin(_resolve_func_module(cb), plugin_name):
                try:
                    scheduler.remove_listener(cb)
                    removed_listeners += 1
                except Exception as e:
                    logger.warning(
                        i18n_t(
                            "log.plugin.gscore_remove_scheduler_listener",
                            plugin_name=plugin_name,
                            e=e,
                        )
                    )
        if removed_listeners:
            logger.info(
                i18n_t(
                    "log.plugin.gscore_removed_listeners_scheduler",
                    plugin_name=plugin_name,
                    removed_listeners=removed_listeners,
                )
            )
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_scheduled_tasks_listeners_fail", plugin_name=plugin_name, e=e))

    # ② 生命周期 Hook 集合 (on_core_start / on_core_start_before / on_core_shutdown)
    try:
        from gsuid_core.server import (
            core_start_def,
            core_shutdown_def,
            core_start_before_def,
        )

        removed_hooks = 0
        for hook_set in (core_start_def, core_start_before_def, core_shutdown_def):
            stale = {h for h in hook_set if _belongs_to_plugin(_resolve_func_module(h.func), plugin_name)}
            if hook_set is core_shutdown_def and stale:
                shutdown_hooks = sorted(stale)
            hook_set -= stale  # 原地差集; stale 取自集合内的同一批对象, 精确移除
            removed_hooks += len(stale)
        if removed_hooks:
            logger.info(
                i18n_t(
                    "log.plugin.gscore_removed_hooks_lifecycle",
                    plugin_name=plugin_name,
                    removed_hooks=removed_hooks,
                )
            )
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_fail_lifecycle_hooks", plugin_name=plugin_name, e=e))

    # ③ FastAPI web 路由 / 挂载
    try:
        from gsuid_core.web_app import app

        original = list(app.router.routes)
        kept = [r for r in original if not _belongs_to_plugin(_route_owner_module(r), plugin_name)]
        if len(kept) != len(original):
            logger.info(
                i18n_t(
                    "log.plugin.gscore_web_routes_mounts",
                    plugin_name=plugin_name,
                    p0=len(original) - len(kept),
                )
            )
            app.router.routes[:] = kept  # 原地替换, 保留列表引用; 无 .endpoint/.app 归属的条目自动保留
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_fail_web_routes", plugin_name=plugin_name, e=e))

    # ④ Agent 环 hook + 套件槽 + AI 工具注册表
    _clean_plugin_agent_state(plugin_name)

    # ⑤ 帮助图缓存与一览注册条目
    try:
        from gsuid_core.help.utils import clean_plugin_help

        clean_plugin_help(plugin_name)
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_fail_help_cache", plugin_name=plugin_name, e=e))

    # ⑥ 插件 Web 页面注册表（静态页 metadata；路由本身由 Core 统一 catch-all 提供）
    try:
        from gsuid_core.webconsole.plugin_page import unregister_plugin_pages

        removed_pages = unregister_plugin_pages(plugin_name)
        if removed_pages:
            logger.info(
                i18n_t(
                    "log.plugin.gscore_plugin_pages_unregistered",
                    plugin_name=plugin_name,
                    removed_pages=removed_pages,
                )
            )
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_fail_plugin_pages", plugin_name=plugin_name, e=e))

    # ⑦ WebConsole 数据库管理模型注册表（避免重载后旧模型类占住同名表缓存）
    if "gsuid_core.webconsole.mount_app" in sys.modules:
        try:
            from gsuid_core.webconsole.mount_app import site

            removed_admins = site.plugins_page.pop(plugin_name, None)
            removed_count = len(removed_admins) if removed_admins else 0
            stale_registered = [
                cls for cls in site._registered if _belongs_to_plugin(_resolve_func_module(cls), plugin_name)
            ]
            for cls in stale_registered:
                site._registered.pop(cls, None)
            removed_count += len(stale_registered)
            if removed_count:
                logger.info(
                    i18n_t(
                        "log.plugin.gscore_admin_models_unregistered",
                        plugin_name=plugin_name,
                        count=removed_count,
                    )
                )
        except Exception as e:
            logger.warning(i18n_t("log.plugin.gscore_fail_admin_models", plugin_name=plugin_name, e=e))

    return shutdown_hooks


def _clean_plugin_agent_state(plugin_name: str) -> None:
    """摘掉插件的 Agent 环 hook、让它占用的槽位回落默认套件、卸掉它注册的 AI 工具。

    ``_TOOL_REGISTRY`` 历来不被热重载清理（靠 re-import 覆盖同名符号侥幸没炸），
    这是先于套件化存在的缺陷；套件槽引入后不清就会留下旧占用者的空壳工具。
    """
    try:
        from gsuid_core.ai_core.kits import KIT_SLOTS, get_kit, enable_kit, disable_kit, occupants_of
        from gsuid_core.ai_core.hooks import drop_hooks_for_module
        from gsuid_core.ai_core.register import (
            unregister_tools_of_plugin,
            unregister_entities_of_plugin,
            unregister_ai_register_hooks_of_plugin,
        )

        # 槽位回落：插件套件被卸掉后按配置重新装默认占用者，否则该槽静默变 off
        for slot in KIT_SLOTS:
            for kit_id in occupants_of(slot.name):
                kit = get_kit(kit_id)
                if kit is None or not kit_id.startswith(f"{plugin_name}."):
                    continue
                disable_kit(kit_id)
                if get_kit(slot.default_kit_id) is not None:
                    enable_kit(slot.default_kit_id)

        dropped = drop_hooks_for_module(plugin_name)
        tools = unregister_tools_of_plugin(plugin_name)
        unregister_entities_of_plugin(plugin_name)
        unregister_ai_register_hooks_of_plugin(plugin_name)
        if dropped or tools:
            logger.info(
                i18n_t(
                    "log.plugin.gscore_cleanup_agent_hooks_tools",
                    plugin_name=plugin_name,
                    hooks=dropped,
                    tools=tools,
                )
            )
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_fail_agent_hooks", plugin_name=plugin_name, e=e))


def _snapshot_plugin_route_anchor(plugin_name: str) -> Optional[int]:
    """记录该插件在 app.router.routes 中最早一条路由的位置, 供重导入后回插用。

    必须在 _clean_plugin_global_state 之前调用 —— 清理后位置就丢了。
    """
    try:
        from gsuid_core.web_app import app

        for i, r in enumerate(app.router.routes):
            if _belongs_to_plugin(_route_owner_module(r), plugin_name):
                return i
    except Exception:
        pass
    return None


def _restore_plugin_routes_position(plugin_name: str, anchor: Optional[int]) -> None:
    """把重导入后 append 到末尾的插件路由, 移回原 anchor 位置。

    重载时 @app.get(...) 装饰器把新路由追加到 routes 末尾, Starlette 按 list 顺序首匹配,
    若启动时排在该插件之后的其它插件含 catch-all 路径参数路由, 重载后该 catch-all 会
    抢先命中, 把本插件更具体的路径吃掉。保住 anchor 位置, 整张表对其它插件的相对顺序
    就和重载前一致。

    必须在事件循环主线程同步调用 (`reload_plugin` 本身就是这种形态); 若被 `asyncio.
    to_thread` 等机制甩到线程池, 失去 GIL 单线程保护后 routes 的整表替换不再原子,
    可能与其它协程的路由注册产生竞争。
    """
    if anchor is None:
        return
    try:
        from gsuid_core.web_app import app

        routes = app.router.routes
        owned_idx_set = {i for i, r in enumerate(routes) if _belongs_to_plugin(_route_owner_module(r), plugin_name)}
        if not owned_idx_set:
            return
        if min(owned_idx_set) <= anchor:
            return  # 已经在原位或更前, 无需调整
        owned = [routes[i] for i in sorted(owned_idx_set)]
        rest = [r for i, r in enumerate(routes) if i not in owned_idx_set]
        # cleanup 后 list 变短, anchor 可能越过 len(rest), 截一下保证 slice 合法
        insert_at = min(anchor, len(rest))
        # 单次切片赋值替代 pop+insert 序列, 在事件循环主线程里逻辑上原子
        routes[:] = rest[:insert_at] + owned + rest[insert_at:]
        logger.debug(
            i18n_t(
                "log.plugin.gscore_reinserted_routes_name",
                plugin_name=plugin_name,
                p0=len(owned),
                insert_at=insert_at,
            )
        )
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_restoring_route_positions_fail", plugin_name=plugin_name, e=e))


def _get_reload_lock(plugin_name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if loop not in _plugin_reload_locks:
        _plugin_reload_locks[loop] = {}
    locks = _plugin_reload_locks[loop]
    if plugin_name not in locks:
        locks[plugin_name] = asyncio.Lock()
    return locks[plugin_name]


def _enter_async_reload(loop: asyncio.AbstractEventLoop, plugin_name: str) -> None:
    if loop not in _active_async_reloads:
        _active_async_reloads[loop] = {}
    counts = _active_async_reloads[loop]
    counts[plugin_name] = (counts[plugin_name] if plugin_name in counts else 0) + 1


def _leave_async_reload(loop: asyncio.AbstractEventLoop, plugin_name: str) -> None:
    if loop not in _active_async_reloads:
        return
    counts = _active_async_reloads[loop]
    if plugin_name not in counts:
        return
    remaining = counts[plugin_name] - 1
    if remaining <= 0:
        counts.pop(plugin_name, None)
    else:
        counts[plugin_name] = remaining


def _is_async_reload_active(plugin_name: str) -> bool:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if loop in _active_async_reloads:
        counts = _active_async_reloads[loop]
        if plugin_name in counts and counts[plugin_name] > 0:
            return True
    if loop in _plugin_reload_locks:
        locks = _plugin_reload_locks[loop]
        if plugin_name in locks and locks[plugin_name].locked():
            return True
    return False


def _finish_reload_task(
    loop: asyncio.AbstractEventLoop,
    plugin_name: str,
    task: asyncio.Task[str],
) -> None:
    _active_reload_tasks.discard(task)
    _leave_async_reload(loop, plugin_name)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            i18n_t(
                "log.plugin.gscore_reload_task_fail",
                plugin_name=plugin_name,
                res=repr(exc),
            )
        )


def _discard_start_task(plugin_name: str, task: asyncio.Task[None]) -> None:
    """启动 Hook 后台任务结束后摘除句柄并消费未检索异常。"""
    _start_task_meta.pop(task, None)
    if plugin_name in _plugin_start_tasks and _plugin_start_tasks[plugin_name] is task:
        _plugin_start_tasks.pop(plugin_name, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            i18n_t(
                "log.plugin.gscore_start_task_fail",
                plugin_name=plugin_name,
                res=repr(exc),
            )
        )


def _discard_shutdown_task(plugin_name: str, task: asyncio.Task[None]) -> None:
    if plugin_name in _plugin_shutdown_tasks and _plugin_shutdown_tasks[plugin_name] is task:
        _plugin_shutdown_tasks.pop(plugin_name, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            i18n_t(
                "log.plugin.gscore_shutdown_task_fail",
                plugin_name=plugin_name,
                res=repr(exc),
            )
        )


async def _await_previous_task(plugin_name: str, prev_task: asyncio.Task[None] | None) -> None:
    if prev_task is None or prev_task.done():
        return
    done, _ = await asyncio.wait({prev_task}, timeout=_RELOAD_SHUTDOWN_TIMEOUT)
    if prev_task not in done:
        logger.warning(
            i18n_t(
                "log.plugin.gscore_prev_start_task_timeout",
                plugin_name=plugin_name,
                timeout=_RELOAD_SHUTDOWN_TIMEOUT,
            )
        )


def _cancel_start_task_sync(plugin_name: str) -> tuple[asyncio.Task[None] | None, bool]:
    """取消上一轮尚未跑完的启动任务, 返回 (旧任务, 旧版启动钩子是否从未开始执行)。"""
    old = _plugin_start_tasks[plugin_name] if plugin_name in _plugin_start_tasks else None
    if old is None or old.done():
        return None, False
    meta = _start_task_meta[old] if old in _start_task_meta else None
    logger.debug(i18n_t("log.plugin.gscore_cancelling_unfinished_startup_ok", plugin_name=plugin_name))
    old.cancel()
    skip_shutdown = meta is not None and meta.has_start_hooks and not meta.started
    return old, skip_shutdown


async def _cancel_and_wait_start_task(plugin_name: str) -> bool:
    """取消并等待上一轮启动任务退出, 返回是否应跳过旧 shutdown（未启动过）。"""
    old = _plugin_start_tasks[plugin_name] if plugin_name in _plugin_start_tasks else None
    if old is None or old.done():
        return False
    meta = _start_task_meta[old] if old in _start_task_meta else None
    logger.debug(i18n_t("log.plugin.gscore_cancelling_unfinished_startup_ok", plugin_name=plugin_name))
    old.cancel()
    await _await_previous_task(plugin_name, old)
    return meta is not None and meta.has_start_hooks and not meta.started


def _attach_late_failure_logger(task: asyncio.Task[object], plugin_name: str, hook: _DefHook) -> None:
    def _on_done(done_task: asyncio.Task[object]) -> None:
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if exc is not None:
            logger.warning(
                i18n_t(
                    "log.plugin.gscore_fail_running_shutdown_hook",
                    plugin_name=plugin_name,
                    hook=_hook_label(hook.func),
                    res=repr(exc),
                )
            )

    task.add_done_callback(_on_done)


async def _invoke_shutdown_hook(hook: _DefHook) -> object:
    if inspect.iscoroutinefunction(hook.func):
        return await hook.func()
    return await asyncio.to_thread(hook.func)


async def _run_shutdown_hooks(plugin_name: str, shutdown_hooks: list[_DefHook]) -> None:
    if not shutdown_hooks:
        return
    logger.info(
        i18n_t(
            "log.plugin.gscore_running_shutdown_hooks",
            plugin_name=plugin_name,
            count=len(shutdown_hooks),
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _RELOAD_SHUTDOWN_TIMEOUT
    failed = 0
    for priority, group in groupby(shutdown_hooks, key=lambda h: h.priority):
        group_hooks = list(group)
        remaining = deadline - loop.time()
        if remaining <= 0:
            failed += len(group_hooks)
            logger.warning(
                i18n_t(
                    "log.plugin.gscore_shutdown_hooks_budget_exhausted",
                    plugin_name=plugin_name,
                    timeout=_RELOAD_SHUTDOWN_TIMEOUT,
                    hooks=[_hook_label(h.func) for h in group_hooks],
                )
            )
            continue
        logger.debug(
            i18n_t(
                "log.plugin.gscore_running_shutdown_hook",
                plugin_name=plugin_name,
                priority=priority,
                hooks=[_hook_label(h.func) for h in group_hooks],
            )
        )
        tasks: list[asyncio.Task[object]] = [asyncio.create_task(_invoke_shutdown_hook(h)) for h in group_hooks]
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        if pending:
            timed_out_hooks: list[str] = []
            for h, t in zip(group_hooks, tasks):
                if t in pending:
                    timed_out_hooks.append(_hook_label(h.func))
                    _attach_late_failure_logger(t, plugin_name, h)
                    t.cancel()
            # 给可取消协程极短收尾窗口, 不死等不可取消的阻塞线程池任务
            await asyncio.wait(pending, timeout=0.05)
            failed += len(pending)
            logger.warning(
                i18n_t(
                    "log.plugin.gscore_shutdown_hooks_timeout",
                    plugin_name=plugin_name,
                    timeout=_RELOAD_SHUTDOWN_TIMEOUT,
                    hooks=timed_out_hooks,
                )
            )
        for h, t in zip(group_hooks, tasks):
            if t not in done or t.cancelled():
                continue
            res = t.exception()
            if res is not None:
                failed += 1
                logger.warning(
                    i18n_t(
                        "log.plugin.gscore_fail_running_shutdown_hook",
                        plugin_name=plugin_name,
                        hook=_hook_label(h.func),
                        res=repr(res),
                    )
                )
    if failed:
        logger.warning(
            i18n_t(
                "log.plugin.gscore_shutdown_hooks_failed",
                plugin_name=plugin_name,
                failed=failed,
            )
        )
    else:
        logger.success(i18n_t("log.plugin.gscore_shutdown_hooks_done", plugin_name=plugin_name))


async def _shutdown_worker(plugin_name: str) -> None:
    """受保护的单插件关闭串行队列：不被快速重载取消, 逐批出队确保不丢钩子。"""
    while True:
        batches = _pending_shutdown_hooks[plugin_name] if plugin_name in _pending_shutdown_hooks else None
        if not batches:
            _pending_shutdown_hooks.pop(plugin_name, None)
            return
        batch = batches.pop(0)
        await _await_previous_task(plugin_name, batch.wait_task)
        await _run_shutdown_hooks(plugin_name, batch.hooks)


def _enqueue_shutdown_hooks(
    plugin_name: str,
    shutdown_hooks: list[_DefHook],
    wait_task: asyncio.Task[None] | None = None,
) -> bool:
    if not shutdown_hooks:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(i18n_t("log.plugin.gscore_shutdown_no_event_loop", plugin_name=plugin_name))
        return False

    batch = _ShutdownBatch(hooks=list(shutdown_hooks), wait_task=wait_task)
    if plugin_name in _pending_shutdown_hooks:
        _pending_shutdown_hooks[plugin_name].append(batch)
    else:
        _pending_shutdown_hooks[plugin_name] = [batch]

    existing = _plugin_shutdown_tasks[plugin_name] if plugin_name in _plugin_shutdown_tasks else None
    if existing is None or existing.done():
        task = loop.create_task(_shutdown_worker(plugin_name))
        _plugin_shutdown_tasks[plugin_name] = task
        task.add_done_callback(lambda t: _discard_shutdown_task(plugin_name, t))
    return True


async def _drain_pending_shutdown_task(plugin_name: str) -> None:
    """等待该插件受保护的关闭队列排空；shield 防止取消启动任务时波及在途关闭。"""
    while True:
        task = _plugin_shutdown_tasks[plugin_name] if plugin_name in _plugin_shutdown_tasks else None
        if task is None or task.done():
            return
        await asyncio.shield(task)


def _schedule_plugin_start_hooks(
    plugin_name: str,
    prev_start_task: asyncio.Task[None] | None = None,
) -> bool:
    """后台调度新插件的 @on_ai_register 与 @on_core_start 钩子。"""
    try:
        from gsuid_core.server import core_start_def
        from gsuid_core.ai_core.register import run_ai_register_hooks, has_pending_ai_register_hooks

        plugin_hooks = sorted(
            h for h in core_start_def if _belongs_to_plugin(_resolve_func_module(h.func), plugin_name)
        )
        has_ai_hooks = has_pending_ai_register_hooks(plugin_name)
        if not plugin_hooks and not has_ai_hooks:
            logger.debug(i18n_t("log.plugin.gscore_name_core_start", plugin_name=plugin_name))
            return False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(i18n_t("log.plugin.gscore_running_event_loop_ok", plugin_name=plugin_name))
            return False

        meta = _StartTaskMeta(has_start_hooks=bool(plugin_hooks))

        async def _runner() -> None:
            # 让出一拍, 避免同 tick 已排队等待重载锁的下一轮重载被抢先打日志或抢跑
            await asyncio.sleep(0)
            await _await_previous_task(plugin_name, prev_start_task)
            await _drain_pending_shutdown_task(plugin_name)
            if has_ai_hooks:
                await run_ai_register_hooks(plugin_name)
            if not plugin_hooks:
                return

            async def _invoke_start(hook: _DefHook) -> object:
                meta.started = True
                if inspect.iscoroutinefunction(hook.func):
                    return await hook.func()
                return await asyncio.to_thread(hook.func)

            logger.info(
                i18n_t(
                    "log.plugin.gscore_running_startup_hooks",
                    plugin_name=plugin_name,
                    p0=len(plugin_hooks),
                )
            )
            failed = 0
            # 按 priority 分组, 组内并发、组间串行 (与 core_start_execute 一致)
            for priority, group in groupby(plugin_hooks, key=lambda h: h.priority):
                group_hooks = list(group)
                logger.debug(
                    i18n_t(
                        "log.plugin.gscore_running_startup_hook",
                        plugin_name=plugin_name,
                        priority=priority,
                        p0=[_hook_label(h.func) for h in group_hooks],
                    )
                )
                results = await asyncio.gather(
                    *[_invoke_start(h) for h in group_hooks],
                    return_exceptions=True,
                )
                for h, res in zip(group_hooks, results):
                    if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                        failed += 1
                        logger.warning(
                            i18n_t(
                                "log.plugin.gscore_fail_running_startup_hook",
                                plugin_name=plugin_name,
                                p0=_hook_label(h.func),
                                res=repr(res),
                            )
                        )
            if failed:
                logger.warning(
                    i18n_t(
                        "log.plugin.gscore_startup_hooks_name",
                        plugin_name=plugin_name,
                        failed=failed,
                    )
                )
            else:
                logger.success(i18n_t("log.plugin.gscore_startup_hooks_name_2", plugin_name=plugin_name))

        task = loop.create_task(_runner())
        _plugin_start_tasks[plugin_name] = task
        _start_task_meta[task] = meta
        task.add_done_callback(lambda t: _discard_start_task(plugin_name, t))
        return True
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_scheduling_startup_hooks_fail", plugin_name=plugin_name, e=e))
        return False


def _prepare_reload(plugin_name: str) -> tuple[PluginKindMeta | None, list[tuple[str, Path, str]]] | str:
    """第 0 步：解析路径并预检可加载模块，失败时返回错误文案且不触碰运行时状态。"""
    plugin_path = GsServer.resolve_plugin_path(plugin_name)
    if plugin_path is None:
        return f"❌ 插件{plugin_name}不存在!"

    from gsuid_core.server import read_meta_plugin_info

    meta_info = read_meta_plugin_info(plugin_path)
    module_list = gss.load_plugin(plugin_path)
    if module_list is None:
        return f"❌ 未知的插件类型 {plugin_name}"
    if isinstance(module_list, str):
        return module_list
    if not module_list:
        return f"❌ 插件{plugin_name}无可加载模块!"
    return meta_info, module_list


def _detach_plugin_registrations(
    plugin_name: str,
    meta_info: PluginKindMeta | None,
) -> tuple[Optional[int], list[_DefHook]]:
    """断流并清理插件在全局单例上的注册，保留 sys.modules 供旧 shutdown 执行。"""
    from gsuid_core.meta_plugins import unregister_meta_plugin
    from gsuid_core.utils.backup.hints import unregister_backup_paths

    sv_names_to_del = [sv_name for sv_name, sv in SL.lst.items() if sv.self_plugin_name == plugin_name]
    plugins_to_del = {sv.plugins for sv in SL.lst.values() if sv.self_plugin_name == plugin_name}

    for sv_name in sv_names_to_del:
        sv = SL.lst.pop(sv_name)
        # 清除 is_initialized，否则 SV.__init__ 重载时会被跳过
        sv.is_initialized = False

    for plugins in plugins_to_del:
        SL.detail_lst.pop(plugins, None)

    SL.plugins.pop(plugin_name, None)
    reset_trigger_index()

    for plugins in plugins_to_del:
        if plugins.name:
            unregister_backup_paths(plugins.name)
    unregister_backup_paths(plugin_name)

    if meta_info is not None:
        unregister_meta_plugin(plugin_name)

    route_anchor = _snapshot_plugin_route_anchor(plugin_name)
    shutdown_hooks = _clean_plugin_global_state(plugin_name)
    return route_anchor, shutdown_hooks


def _purge_plugin_modules(plugin_name: str) -> None:
    """清理插件及其全部子模块在 sys.modules 与 _module_cache 中的缓存。"""
    stale_modules = [k for k in sys.modules if _belongs_to_plugin(k, plugin_name)]
    for k in stale_modules:
        sys.modules.pop(k, None)

    stale_cache = [k for k in list(_module_cache) if plugin_name in k]
    for k in stale_cache:
        _module_cache.pop(k, None)
    logger.debug(
        i18n_t(
            "log.plugin.gscore_name_sys_modules",
            plugin_name=plugin_name,
            p0=len(stale_modules),
            p1=len(stale_cache),
        )
    )


def _import_plugin_modules(
    plugin_name: str,
    meta_info: PluginKindMeta | None,
    module_list: list[tuple[str, Path, str]],
    route_anchor: Optional[int],
) -> Optional[str]:
    """重新导入插件模块并恢复路由位置；导入失败时回滚半导入状态并返回错误文案。"""
    import_error: Optional[str] = None
    for module_name, filepath, _type in module_list:
        try:
            gss.cached_import(module_name, filepath, _type)
        except Exception as e:
            logger.exception(i18n_t("log.plugin.module_name_fail", module_name=module_name, e=e))
            import_error = f"❌ 重载失败: {e}"
            break

    if import_error is not None:
        # 回滚本次半导入已注册的 SV / Hook / 路由 / 管理模型与子模块，避免残留半残态
        _detach_plugin_registrations(plugin_name, meta_info)
        _purge_plugin_modules(plugin_name)
        return import_error

    if meta_info is not None:
        gss.bind_loaded_meta_plugin(meta_info, module_list)

    _restore_plugin_routes_position(plugin_name, route_anchor)
    return None


async def _reload_plugin_locked(plugin_name: str) -> str:
    async with _get_reload_lock(plugin_name):
        logger.info(i18n_t("log.plugin.plugin_name_3", plugin_name=plugin_name))

        prepared = _prepare_reload(plugin_name)
        if isinstance(prepared, str):
            return prepared
        meta_info, module_list = prepared

        skip_shutdown = await _cancel_and_wait_start_task(plugin_name)
        await _drain_pending_shutdown_task(plugin_name)

        route_anchor, shutdown_hooks = _detach_plugin_registrations(plugin_name, meta_info)
        if not skip_shutdown:
            await _run_shutdown_hooks(plugin_name, shutdown_hooks)

        _purge_plugin_modules(plugin_name)
        import_error = _import_plugin_modules(plugin_name, meta_info, module_list, route_anchor)
        if import_error is not None:
            return import_error

        _schedule_plugin_start_hooks(plugin_name)

        logger.success(i18n_t("log.plugin.plugin_name", plugin_name=plugin_name))
        return f"✨ 已重载插件 {plugin_name}!"


async def reload_plugin_async(plugin_name: str) -> str:
    """异步热重载插件（推荐主入口）。

    按单插件锁串行化：先断流并在旧 sys.modules 仍存活时等待旧 @on_core_shutdown 完成，
    再清空模块缓存并重导新代码，最后后台调度新插件的 @on_ai_register 与 @on_core_start。
    外层 shield 防止调用方取消导致已断流的插件停留在半卸载状态。
    """
    loop = asyncio.get_running_loop()
    _enter_async_reload(loop, plugin_name)
    task = loop.create_task(_reload_plugin_locked(plugin_name))
    _active_reload_tasks.add(task)
    task.add_done_callback(lambda t: _finish_reload_task(loop, plugin_name, t))
    return await asyncio.shield(task)


def reload_plugin(plugin_name: str) -> str:
    """同步热重载兼容入口（异步上下文请优先使用 ``await reload_plugin_async``）。

    受同步签名限制，旧 @on_core_shutdown 会在清理 sys.modules 与重新导入之后由受保护的
    后台串行队列执行，因此钩子内延迟导入可能命中新模块；需要完整隔离请用异步版本。
    """
    logger.info(i18n_t("log.plugin.plugin_name_3", plugin_name=plugin_name))
    if _is_async_reload_active(plugin_name):
        logger.warning(i18n_t("log.plugin.gscore_sync_reload_while_async_active", plugin_name=plugin_name))
        return f"❌ 插件 {plugin_name} 正在重载中，请稍后再试!"

    prepared = _prepare_reload(plugin_name)
    if isinstance(prepared, str):
        return prepared
    meta_info, module_list = prepared

    prev_start, skip_shutdown = _cancel_start_task_sync(plugin_name)
    route_anchor, shutdown_hooks = _detach_plugin_registrations(plugin_name, meta_info)
    _purge_plugin_modules(plugin_name)

    import_error = _import_plugin_modules(plugin_name, meta_info, module_list, route_anchor)
    queued_shutdown = False
    if not skip_shutdown:
        queued_shutdown = _enqueue_shutdown_hooks(plugin_name, shutdown_hooks, wait_task=prev_start)
    if import_error is not None:
        return import_error

    # 若 shutdown 已入队，由关闭队列单点等待 prev_start，避免与启动任务重复等待和重复告警
    _schedule_plugin_start_hooks(plugin_name, prev_start_task=None if queued_shutdown else prev_start)

    logger.success(i18n_t("log.plugin.plugin_name", plugin_name=plugin_name))
    return f"✨ 已重载插件 {plugin_name}!"
