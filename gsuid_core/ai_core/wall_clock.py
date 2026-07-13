"""交互式 run 的墙钟软预算时钟(C-4 配套)——把"等人"的时长排除在预算之外。

``gs_agent`` 的 C-4 软预算(``scaffold_wall_clock_budget``)用来治多步任务的
延迟长尾:run 墙钟超预算后注入一次"停止新工具轮、立即收敛作答"提示。但它按
**真实墙钟**计时,于是有一类时间被错误地计进预算:

    **等待人工输入**——``ask_user`` / ``ask_user_form`` 在工具内挂起,等用户
    读问题、点按钮。用户想 30s,预算就被吃掉 30s。用户越慢,Agent 越早被判
    "磨蹭",最后落得"问完问题就被迫收尾、什么也没做"。

这段时间不是模型磨蹭,不该触发收敛。本模块把 C-4 的时钟做成**可暂停**的:
run 开始时 ``gs_agent`` 装一个累加器进 ContextVar,等人期间用
``pause_wall_clock()`` 把这段时长记进累加器,预算判定时扣掉它。

**挂起点已在根上包好**:``Bot.receive_resp`` / ``receive_mutiply_resp`` 真正
等人的那一段自带 ``pause_wall_clock()``,所以任何调用方(内建工具、插件的
``@ai_tools``、能力智能体生成的代码)都自动被排除,无需各自记得包。工具只在
想额外排除**自己的排队时间**时才显式再包一层(如 ``ask_user`` 的会话级串行锁)
——嵌套是安全的,时钟按最外层区间的并集计时。

ContextVar 会被 run 调用树(工具及其 ``create_task`` 子任务)自然继承,因此
调用处不需要拿到 agent 实例就能记账;不在 run 内调用则是安全的空操作。
"""

from __future__ import annotations

import time
from typing import Dict, Optional, AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Token, ContextVar

#: 一个 run 的时钟状态:excluded=已排除秒数,depth=当前挂起层数,started=最外层挂起起点。
Clock = Dict[str, float]

#: 当前 run 的时钟;None = 不在受计时的 run 内(空操作)。
_clock: ContextVar[Optional[Clock]] = ContextVar("ai_wall_clock", default=None)


def install_clock() -> tuple[Clock, Token[Optional[Clock]]]:
    """在 run 起点装一个新时钟,返回(时钟, 还原令牌);由 ``gs_agent.run`` 调用。

    **令牌必须在 run 的 finally 里 ``uninstall_clock``**:嵌套 run(图片理解、
    subagent 工具)跑在同一个 context 里,不还原会把父 run 的时钟永久替换成子
    run 的——父 run 后续的"等人"记进了孤儿时钟,排除就此静默失效。
    """
    clock: Clock = {"excluded": 0.0, "depth": 0.0, "started": 0.0}
    return clock, _clock.set(clock)


def uninstall_clock(token: Token[Optional[Clock]]) -> None:
    """还原上一层 run 的时钟(令牌契约见 ``install_clock``)。"""
    _clock.reset(token)


def excluded_seconds(clock: Optional[Clock]) -> float:
    """时钟里已排除的秒数(容忍 None)。"""
    return clock["excluded"] if clock else 0.0


@asynccontextmanager
async def pause_wall_clock() -> AsyncIterator[None]:
    """在此上下文内流逝的时间不计入 C-4 墙钟软预算。

    ``Bot.receive_resp`` / ``receive_mutiply_resp`` 的等待段已自带本上下文,
    调用方无需重复包;只有想连**自己的排队/串行等待**一起排除时才显式包一层
    (可安全嵌套)。不在 run 内时为空操作。

    Example:
        async with pause_wall_clock():  # 连排队一起排除
            async with session_lock:
                resp = await bot.receive_resp(question, timeout=timeout)
    """
    clock = _clock.get()
    if clock is None:
        yield
        return
    # 只认最外层挂起的区间(并集):并发的两个 ask_user 若各记各的,重叠部分会被
    # 排除两次,excluded 甚至能超过真实墙钟,软预算就永远不触发了。
    if clock["depth"] == 0:
        clock["started"] = time.time()
    clock["depth"] += 1
    try:
        yield
    finally:
        clock["depth"] -= 1
        if clock["depth"] == 0:
            clock["excluded"] += time.time() - clock["started"]


__all__ = ["Clock", "install_clock", "uninstall_clock", "excluded_seconds", "pause_wall_clock"]
