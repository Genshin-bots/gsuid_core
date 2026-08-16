import sys
import asyncio
from typing import Dict, Optional
from itertools import groupby

from gsuid_core.sv import SL
from gsuid_core.gss import gss
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.server import GsServer, _module_cache

# 第五步后台启动 Hook 的任务句柄表：① 保留引用防止任务被 GC ② 快速重载时取消上一轮未跑完的
_plugin_start_tasks: Dict[str, asyncio.Task] = {}


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


def _clean_plugin_global_state(plugin_name: str) -> None:
    """第 3.5 步: 清理插件注册到长生命周期全局单例上的状态。

    覆盖 APScheduler 定时任务 + 监听器 / 生命周期 Hook 集合 / FastAPI web 路由+挂载。
    必须在重新 import 之前调用 —— 带固定 id 的定时任务若不先清, 重新注册会撞
    ConflictingIdError 导致整个插件重载失败。三段各自独立 try/except, 互不影响。
    """
    logger.debug(i18n_t("log.plugin.gscore_cleanup_global_registration_start", plugin_name=plugin_name))

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


def _clean_plugin_agent_state(plugin_name: str) -> None:
    """摘掉插件的 Agent 环 hook、让它占用的槽位回落默认套件、卸掉它注册的 AI 工具。

    ``_TOOL_REGISTRY`` 历来不被热重载清理（靠 re-import 覆盖同名符号侥幸没炸），
    这是先于套件化存在的缺陷；套件槽引入后不清就会留下旧占用者的空壳工具。
    """
    try:
        from gsuid_core.ai_core.kits import KIT_SLOTS, get_kit, enable_kit, disable_kit, occupants_of
        from gsuid_core.ai_core.hooks import drop_hooks_for_module
        from gsuid_core.ai_core.register import unregister_tools_of_plugin, unregister_entities_of_plugin

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


def _discard_start_task(plugin_name: str, task: asyncio.Task) -> None:
    """启动 Hook 后台任务结束后, 从句柄表里摘除自己 (仅当还是当前这个任务时)。"""
    if _plugin_start_tasks.get(plugin_name) is task:
        _plugin_start_tasks.pop(plugin_name, None)


def _run_plugin_start_hooks(plugin_name: str) -> None:
    """第 5 步: 重载完成后, 重新执行该插件的 @on_core_start hook。

    补全 "reload = 插件重新加载" 的语义 —— 插件代码已换新, 其初始化也应重新跑一遍。
    只跑被重载插件的 hook (按 func.__module__ 过滤), 不调全局 core_start_execute();
    后台 create_task 执行、不阻塞 reload_plugin; 不跑 @on_core_start_before。
    """
    try:
        from gsuid_core.server import core_start_def

        # 第 3.5 步②已清掉旧 hook、第四步重新 import 注册了 fresh hook, 这里过滤到的就是 fresh 的
        plugin_hooks = sorted(
            h for h in core_start_def if _belongs_to_plugin(_resolve_func_module(h.func), plugin_name)
        )
        if not plugin_hooks:
            logger.debug(i18n_t("log.plugin.gscore_name_core_start", plugin_name=plugin_name))
            return

        async def _runner():
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
                        p0=[getattr(h.func, "__qualname__", h.func) for h in group_hooks],
                    )
                )
                results = await asyncio.gather(
                    *[
                        h.func() if asyncio.iscoroutinefunction(h.func) else asyncio.to_thread(h.func)
                        for h in group_hooks
                    ],
                    return_exceptions=True,
                )
                for h, res in zip(group_hooks, results):
                    if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                        failed += 1
                        logger.warning(
                            i18n_t(
                                "log.plugin.gscore_fail_running_startup_hook",
                                plugin_name=plugin_name,
                                p0=getattr(h.func, "__qualname__", h.func),
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

        # 快速重载场景: 取消上一轮还没跑完的
        old = _plugin_start_tasks.get(plugin_name)
        if old is not None and not old.done():
            logger.debug(i18n_t("log.plugin.gscore_cancelling_unfinished_startup_ok", plugin_name=plugin_name))
            old.cancel()

        try:
            task = asyncio.get_running_loop().create_task(_runner())
        except RuntimeError:
            logger.warning(i18n_t("log.plugin.gscore_running_event_loop_ok", plugin_name=plugin_name))
            return
        # 保留引用防止任务被 GC; 完成后从句柄表摘除
        _plugin_start_tasks[plugin_name] = task
        task.add_done_callback(lambda t: _discard_start_task(plugin_name, t))
    except Exception as e:
        logger.warning(i18n_t("log.plugin.gscore_scheduling_startup_hooks_fail", plugin_name=plugin_name, e=e))


def reload_plugin(plugin_name: str) -> str:
    logger.info(i18n_t("log.plugin.plugin_name_3", plugin_name=plugin_name))

    # ──────────────────────────────────────────
    # 第 0 步：先解析磁盘路径（plugins/ 与 buildin_plugins/）
    # 必须在任何清理之前完成 —— 否则路径解析失败会留下「已卸载、无法恢复」的空壳，
    # 直到进程重启（core_command 等内置插件此前正中此坑）。
    # ──────────────────────────────────────────
    plugin_path = GsServer.resolve_plugin_path(plugin_name)
    if plugin_path is None:
        return f"❌ 插件{plugin_name}不存在!"

    # 预检可加载模块列表（不 import）；空列表或错误信息直接返回，不触碰运行时状态
    module_list = gss.load_plugin(plugin_path)
    if module_list is None:
        return f"❌ 未知的插件类型 {plugin_name}"
    if isinstance(module_list, str):
        return module_list  # load_plugin 已经返回了错误信息
    if not module_list:
        return f"❌ 插件{plugin_name}无可加载模块!"

    # ──────────────────────────────────────────
    # 第一步：收集该插件下所有 SV 和 Plugins 对象
    # ──────────────────────────────────────────
    sv_names_to_del = [sv_name for sv_name, sv in SL.lst.items() if sv.self_plugin_name == plugin_name]
    plugins_to_del = {sv.plugins for sv in SL.lst.values() if sv.self_plugin_name == plugin_name}

    # ──────────────────────────────────────────
    # 第二步：清理 SL 三张表
    # ──────────────────────────────────────────
    for sv_name in sv_names_to_del:
        sv = SL.lst.pop(sv_name)
        # 清除 is_initialized，否则 SV.__init__ 重载时会被跳过
        sv.is_initialized = False

    for plugins in plugins_to_del:
        SL.detail_lst.pop(plugins, None)

    SL.plugins.pop(plugin_name, None)

    # ──────────────────────────────────────────
    # 第三步：清理 sys.modules 和 _module_cache
    # 必须覆盖所有子模块，不能只清入口
    # ──────────────────────────────────────────
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

    # ──────────────────────────────────────────
    # 第 3.5 步：清理插件注册到全局单例上的状态（定时任务+监听器 / 生命周期 Hook / web 路由）
    # 必须在重新 import 之前，否则带固定 id 的定时任务会撞 ConflictingIdError
    # 路由位置先 snapshot 一下, 第 4.5 步要用来把新路由放回原位
    # ──────────────────────────────────────────
    route_anchor = _snapshot_plugin_route_anchor(plugin_name)
    _clean_plugin_global_state(plugin_name)

    # ──────────────────────────────────────────
    # 第四步：重新加载（使用第 0 步已解析的 Path，避免仅查 plugins/ 漏掉内置插件）
    # ──────────────────────────────────────────
    for module_name, filepath, _type in module_list:
        try:
            gss.cached_import(module_name, filepath, _type)
        except Exception as e:
            logger.exception(i18n_t("log.plugin.module_name_fail", module_name=module_name, e=e))
            return f"❌ 重载失败: {e}"

    # ──────────────────────────────────────────
    # 第 4.5 步：把刚 append 到末尾的新路由放回原 anchor 位置
    # 保住与其它插件 (尤其是带 catch-all path 参数的) 的相对顺序
    # ──────────────────────────────────────────
    _restore_plugin_routes_position(plugin_name, route_anchor)

    # ──────────────────────────────────────────
    # 第五步：重载完成后，重跑该插件的 @on_core_start hook（补全「插件加载」语义）
    # ──────────────────────────────────────────
    _run_plugin_start_hooks(plugin_name)

    logger.success(i18n_t("log.plugin.plugin_name", plugin_name=plugin_name))
    return f"✨ 已重载插件 {plugin_name}!"
