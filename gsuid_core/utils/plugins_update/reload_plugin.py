import sys
import asyncio
from typing import Dict, Optional
from itertools import groupby

from gsuid_core.sv import SL
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.server import _module_cache

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
    logger.debug(f"🧹 [GsCore] 开始清理插件 {plugin_name} 的全局注册状态...")

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
                    logger.warning(f"🧹 [GsCore] 移除插件 {plugin_name} 的旧定时任务 {job.id} 失败: {e}")
        if removed_jobs:
            logger.info(f"🧹 [GsCore] 已清理插件 {plugin_name} 的 {len(removed_jobs)} 个旧定时任务")
            logger.debug(f"🧹 [GsCore] {plugin_name} 被清理的定时任务 id: {removed_jobs}")

        # 监听器: _listeners 私有属性仅用于枚举, 移除走公开的 remove_listener
        removed_listeners = 0
        for cb, _mask in list(getattr(scheduler, "_listeners", [])):
            if _belongs_to_plugin(_resolve_func_module(cb), plugin_name):
                try:
                    scheduler.remove_listener(cb)
                    removed_listeners += 1
                except Exception as e:
                    logger.warning(f"🧹 [GsCore] 移除插件 {plugin_name} 的旧 scheduler 监听器失败: {e}")
        if removed_listeners:
            logger.info(f"🧹 [GsCore] 已清理插件 {plugin_name} 的 {removed_listeners} 个旧 scheduler 监听器")
    except Exception as e:
        logger.warning(f"🧹 [GsCore] 清理插件 {plugin_name} 的定时任务/监听器时异常: {e}")

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
            logger.info(f"🧹 [GsCore] 已清理插件 {plugin_name} 的 {removed_hooks} 个旧生命周期 Hook")
    except Exception as e:
        logger.warning(f"🧹 [GsCore] 清理插件 {plugin_name} 的生命周期 Hook 时异常: {e}")

    # ③ FastAPI web 路由 / 挂载
    try:
        from gsuid_core.web_app import app

        original = list(app.router.routes)
        kept = [r for r in original if not _belongs_to_plugin(_route_owner_module(r), plugin_name)]
        if len(kept) != len(original):
            logger.info(f"🧹 [GsCore] 已清理插件 {plugin_name} 的 {len(original) - len(kept)} 条旧 web 路由/挂载")
            app.router.routes[:] = kept  # 原地替换, 保留列表引用; 无 .endpoint/.app 归属的条目自动保留
    except Exception as e:
        logger.warning(f"🧹 [GsCore] 清理插件 {plugin_name} 的 web 路由时异常: {e}")


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
        logger.debug(f"🧹 [GsCore] 已将插件 {plugin_name} 的 {len(owned)} 条新路由回插到 index {insert_at}")
    except Exception as e:
        logger.warning(f"🧹 [GsCore] 回插插件 {plugin_name} 路由位置时异常: {e}")


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
            logger.debug(f"♻ [GsCore] 插件 {plugin_name} 无 @on_core_start hook, 跳过启动 Hook 重跑")
            return

        async def _runner():
            logger.info(f"♻ [GsCore] 重载后执行插件 {plugin_name} 的启动 Hook ({len(plugin_hooks)} 个)...")
            failed = 0
            # 按 priority 分组, 组内并发、组间串行 (与 core_start_execute 一致)
            for priority, group in groupby(plugin_hooks, key=lambda h: h.priority):
                group_hooks = list(group)
                logger.debug(
                    f"♻ [GsCore] 执行插件 {plugin_name} 优先级 {priority} 的启动 Hook: "
                    f"{[getattr(h.func, '__qualname__', h.func) for h in group_hooks]}"
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
                            f"♻ [GsCore] 插件 {plugin_name} 启动 Hook "
                            f"{getattr(h.func, '__qualname__', h.func)} 执行异常: {res!r}"
                        )
            if failed:
                logger.warning(f"♻ [GsCore] 插件 {plugin_name} 启动 Hook 执行完成, {failed} 个异常")
            else:
                logger.success(f"♻ [GsCore] 插件 {plugin_name} 启动 Hook 执行完成")

        # 快速重载场景: 取消上一轮还没跑完的
        old = _plugin_start_tasks.get(plugin_name)
        if old is not None and not old.done():
            logger.debug(f"♻ [GsCore] 取消插件 {plugin_name} 上一轮未完成的启动 Hook 任务")
            old.cancel()

        try:
            task = asyncio.get_running_loop().create_task(_runner())
        except RuntimeError:
            logger.warning(f"♻ [GsCore] 无运行中的事件循环, 插件 {plugin_name} 的启动 Hook 未执行")
            return
        # 保留引用防止任务被 GC; 完成后从句柄表摘除
        _plugin_start_tasks[plugin_name] = task
        task.add_done_callback(lambda t: _discard_start_task(plugin_name, t))
    except Exception as e:
        logger.warning(f"♻ [GsCore] 调度插件 {plugin_name} 的启动 Hook 时异常: {e}")


def reload_plugin(plugin_name: str) -> str:
    logger.info(f"🔔 正在重载插件 {plugin_name}...")

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
        f"🔔 [GsCore] 插件 {plugin_name} 已清理 {len(stale_modules)} 个 sys.modules 条目、"
        f"{len(stale_cache)} 个 _module_cache 条目"
    )

    # ──────────────────────────────────────────
    # 第 3.5 步：清理插件注册到全局单例上的状态（定时任务+监听器 / 生命周期 Hook / web 路由）
    # 必须在重新 import 之前，否则带固定 id 的定时任务会撞 ConflictingIdError
    # 路由位置先 snapshot 一下, 第 4.5 步要用来把新路由放回原位
    # ──────────────────────────────────────────
    route_anchor = _snapshot_plugin_route_anchor(plugin_name)
    _clean_plugin_global_state(plugin_name)

    # ──────────────────────────────────────────
    # 第四步：重新加载
    # ──────────────────────────────────────────
    module_list = gss.load_plugin(plugin_name)

    if module_list is None:
        return f"❌ 未知的插件类型 {plugin_name}"
    if isinstance(module_list, str):
        return module_list  # load_plugin 已经返回了错误信息

    for module_name, filepath, _type in module_list:
        try:
            gss.cached_import(module_name, filepath, _type)
        except Exception as e:
            logger.exception(f"❌ 重载模块 {module_name} 失败: {e}")
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

    logger.success(f"✨ 已重载插件 {plugin_name}")
    return f"✨ 已重载插件 {plugin_name}!"
