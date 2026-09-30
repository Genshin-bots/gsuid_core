"""群聊上下文可见性回归锁（§P0/P3/P5）。

背景：生产日志里 39 个 user_input 轮次有 13 轮**完全拿不到 `[与你的对话]` 群转录**，
且全部集中在软触发续聊轮上。模型只拿到一条光消息 + 「承接上文对象/任务」的提示，
于是把指代猜到别人头上（把某人的追问安到另一个 user 的定时任务上），并据此
捏造出「我刚才串月份了」这类不存在的失误。根因是**两个门不一致**：

- 生产侧 `handle_ai` 只在 ``call_to_self or ellipsis_followup or task_management``
  时才调 :func:`build_group_history_block`；
- 消费侧 `context_assembly._SUFFIX_EXEMPT_BLOCKS` 明确豁免 history，却只在
  ``allowed`` 非空时才补回，而未寻址轮返回的恰是空集 —— 豁免机制整体失效。

本文件锁住修好之后的三件事：群转录无条件、口吻锚无条件、出站回复带收件人。
"""

from __future__ import annotations

import ast
import time
import inspect

from gsuid_core.ai_core import handle_ai as handle_ai_mod
from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.outbound import OWNERSHIP_HINT_TTL_SECONDS, _fmt_ago
from gsuid_core.ai_core.history_format import format_history_for_agent


def test_handle_ai_builds_group_history_for_every_group_turn() -> None:
    """群转录不再被"是否被寻址"门控。

    这里走 **AST** 而不是源码窗口：窗口扫描会把「条件写在更前面」和「比较写反」
    一起放过去——`if not event.group_id: ...` 或 `if not event.group_id and x` 都还能
    在调用点前 400 字里找到 `event.group_id`。断言的是**直接包住那次调用的 if 的
    判据本身**，只允许 group_id（可带 bool() 包裹），出现任何别的条件即失败。
    """
    import ast

    tree = ast.parse(inspect.getsource(handle_ai_mod.run_interactive_turn))

    def _is_group_id_only(node: ast.expr) -> bool:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bool":
            return len(node.args) == 1 and _is_group_id_only(node.args[0])
        return isinstance(node, ast.Attribute) and node.attr == "group_id"

    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
        if name == "build_group_history_block":
            calls.append(node)
    assert len(calls) == 1, f"群转录应只有一处装配调用，实际 {len(calls)}"

    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    guard: ast.If | None = None
    node: ast.AST = calls[0]
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If):
            guard = node
            break
    assert guard is not None, "群转录必须在一个 if 里装配（私聊走别的分支）"
    assert _is_group_id_only(guard.test), "群转录必须只以 group_id 为条件"


def test_unaddressed_group_turn_keeps_voice_anchor_and_history() -> None:
    """未寻址轮仍带口吻锚 + 群转录（豁免集不再被空 allowed 吞掉）。"""
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.context_assembly import (
        _SUFFIX_EXEMPT_BLOCKS,
        suffix_allowed_blocks,
    )
    from gsuid_core.ai_core.interaction_scaffold import TurnGraph

    tg = TurnGraph(
        user_type="group",
        message_text="估计不行",
        persona_name="p",
        is_tome=False,
        primary_speaker="u1",
        call_to_self=False,
        ellipsis_followup=False,
        task_management=False,
    )
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=tg, cheap_gate="full")
    allowed = suffix_allowed_blocks(ctx)
    assert isinstance(allowed, frozenset), f"群聊轮应返回允许集，实际 {allowed!r}"
    assert allowed == _SUFFIX_EXEMPT_BLOCKS
    assert "voice_anchor" in allowed and "history" in allowed


def test_assistant_line_renders_addressee_as_ai_arrow() -> None:
    """出站记录带 reply_to_user_id 时，群转录渲染成 `AI→收件人`。

    早先 `reply_to_user_id` 从未被写入，`_make_speaker` 的 AI→ 分支是死代码，模型读
    到的 244 条历史行里 0 条带收件人 —— 读不出"机器人上一条在跟谁说话"。
    """
    t0 = time.time() - 60
    history = [
        MessageRecord(role="user", user_id="u1", user_name="甲", content="在吗", timestamp=t0),
        MessageRecord(
            role="assistant",
            user_id="bot",
            content="唔…在",
            timestamp=t0 + 5,
            metadata={"reply_to_user_id": "u1", "reply_to_user_name": "甲"},
        ),
        MessageRecord(role="user", user_id="u2", user_name="乙", content="我呢", timestamp=t0 + 20),
    ]
    text = format_history_for_agent(history)
    assert "AI→甲(用户ID:u1)" in text
    # 收件人不同则不带箭头，避免"我上一条在对乙说"被误读
    assert "AI→乙" not in text


def test_turn_reply_metadata_records_group_addressee() -> None:
    """群聊交互回复带上收件人；私聊不带（天然 1:1，不需要）。"""
    from gsuid_core.models import Event
    from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

    group_ev = Event(bot_id="b", bot_self_id="s", group_id="g1", user_id="u9", user_type="group")
    group_ev.sender = {"nickname": "甲"}
    assert turn_reply_metadata(group_ev) == {"reply_to_user_id": "u9", "reply_to_user_name": "甲"}

    direct_ev = Event(bot_id="b", bot_self_id="s", user_id="u9", user_type="direct")
    assert turn_reply_metadata(direct_ev) == {}
    assert turn_reply_metadata(None) == {}


def test_main_interactive_path_passes_reply_metadata(monkeypatch) -> None:
    """交互**主路径**必须带收件人（回归锁：这里曾整段漏接）。

    群里默认的发言方式就是 iter 里 ``_commit_streamed_or_send``；早先它只挂了暂扣
    原文 / 假完成气泡 / 纠正补发几条支线，主路径的 ``commit_streamed_history`` 与
    ``send_chat_result`` 都没传 metadata，于是 244 条历史行里 0 条带收件人——只测
    helper 返回值抓不到这种漏接，所以两路都真跑一次。
    """
    import asyncio

    from gsuid_core.bot import Bot, _Bot
    from gsuid_core.models import Event
    from gsuid_core.ai_core.agent_run import loop as loop_mod
    from gsuid_core.ai_core.agent_run.loop import LoopPhase
    from gsuid_core.ai_core.agent_run.state import RunOnceState

    ev = Event(bot_id="b", bot_self_id="s", group_id="g1", user_id="u9", user_type="group")
    st = RunOnceState(
        user_message="在吗",
        bot=Bot(_Bot("probe"), ev),
        ev=ev,
        rag_context=None,
        tools=[],
        return_mode="by_bot",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
    )
    phase = LoopPhase.__new__(LoopPhase)
    phase._run_sent_texts = set()

    # 1) 非流式：生产 WS 群聊走的就是这一支
    sent: list[dict[str, str]] = []

    async def _fake_send(
        _bot: object,
        _text: str,
        ev: object = None,
        extra_metadata: dict[str, str] | None = None,
        **_kw: object,
    ) -> None:
        sent.append(extra_metadata or {})

    monkeypatch.setattr(loop_mod, "send_chat_result", _fake_send)
    asyncio.run(phase._commit_streamed_or_send(st, "在", already_streamed=False))
    assert sent == [{"reply_to_user_id": "u9"}], sent

    # 2) 流式：commit 那一路同样要带（HTTP/SSE 路径由 capture_bot 真正实现）
    st.outbound_stream = True
    committed: list[dict[str, str]] = []

    async def _fake_commit(_text: str, extra_metadata: dict[str, str] | None = None) -> None:
        committed.append(extra_metadata or {})

    assert st.bot is not None
    monkeypatch.setattr(st.bot, "commit_streamed_history", _fake_commit)
    asyncio.run(phase._commit_streamed_or_send(st, "在", already_streamed=True))
    assert committed == [{"reply_to_user_id": "u9"}], committed
    assert st.main_channel_sends == 2


def test_every_user_visible_send_carries_the_addressee() -> None:
    """AI 链路上**每一处**出站都必须带收件人（类级锁，防再漏）。

    早先只挂了暂扣原文 / 假完成 / 纠正补发几条支线，主路径与出戏兜底、改写补发、
    交付发送、发送工具全都没传，群转录里于是清一色 ``AI:``。逐点补总会再漏，
    所以这里按 AST 扫全部出站点：``send_chat_result`` / ``commit_streamed_history``
    的每一次调用都必须显式带 ``extra_metadata``。

    ``proactive/emitter.py`` 故意不在列：主动播报没有"这轮在回谁"，它自带
    ``proactive*`` metadata。
    """
    import ast
    import pathlib

    root = pathlib.Path(inspect.getfile(handle_ai_mod)).resolve().parent
    targets = [
        root / "agent_run" / "loop.py",
        root / "agent_run" / "settle.py",
        root / "gs_agent.py",
        root / "turn_pipeline.py",
        root / "buildin_tools" / "message_sender.py",
    ]
    seen = 0
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 污点传播：直接调 turn_reply_metadata 的表达式，以及由它派生出来的变量名
        tainted: set[str] = {ast.unparse(n) for n in ast.walk(tree) if _calls_addressee_helper(n)}
        for _ in range(3):  # 赋值链最多传播几跳就够，多跳是设计问题不是接线问题
            for n in ast.walk(tree):
                if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.expr):
                    continue
                expr = ast.unparse(n.value)
                if not any(t in expr for t in tainted):
                    continue
                for tgt in n.targets:
                    if isinstance(tgt, ast.Name) and tgt.id not in tainted:
                        tainted.add(tgt.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name not in ("send_chat_result", "commit_streamed_history"):
                continue
            seen += 1
            meta = next((k for k in node.keywords if k.arg == "extra_metadata"), None)
            assert meta is not None, f"{path.name}:{node.lineno} {name} 没带收件人"
            # 光有这个关键字不够：传 `extra_metadata={}` 也能骗过，值必须取自收件人助手
            val = ast.unparse(meta.value)
            assert any(t in val for t in tainted), f"{path.name}:{node.lineno} 收件人非收件人助手: {val}"
    assert seen >= 10, f"只扫到 {seen} 处出站调用，扫描范围可能没对上源码"


def _calls_addressee_helper(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "turn_reply_metadata"


def test_ownership_hint_has_time_bound_and_relative_time(monkeypatch) -> None:
    """归属提示必须有时间上界，且改用相对时间而不是断言"你刚把"。

    关键在**真调用** ``ownership_hint`` 喂一条过期审计：只扫源码的话，把 TTL 判断
    挪到别处、或把比较写反，断言都照样绿。
    """
    import asyncio
    import inspect as _inspect

    from gsuid_core.models import Event
    from gsuid_core.ai_core import outbound as outbound_mod
    from gsuid_core.ai_core.database.outbound import OutboundAudit

    assert 0 < OWNERSHIP_HINT_TTL_SECONDS <= 900.0
    assert _fmt_ago(30) == ""
    assert _fmt_ago(90) == "1 分钟前"
    assert _fmt_ago(600) == "10 分钟前"

    ev = Event(bot_id="b", bot_self_id="s", group_id="g1", user_id="u9", user_type="group")

    def _row(ts: int) -> OutboundAudit:
        return OutboundAudit(
            group_id="g1",
            text="",
            image_handles="",
            topic="那张图",
            target_user="u_other",
            target_name="别人",
            owner_user_id="u9",
            ts=ts,
        )

    async def _recent(*_a: object, **_k: object) -> list[OutboundAudit]:
        return rows

    monkeypatch.setattr(outbound_mod, "_db_ready", lambda: True)
    monkeypatch.setattr(OutboundAudit, "recent_for_group", staticmethod(_recent))

    now = int(time.time())
    # 过期：一条 73 分钟前的交付曾被拿来持续宣告"你正在跟别人对话"
    rows = [_row(now - 73 * 60)]
    assert asyncio.run(outbound_mod.ownership_hint(ev)) == ""

    # TTL 内：带相对时间，且不说"你刚把"
    rows = [_row(now - 90)]
    fresh = asyncio.run(outbound_mod.ownership_hint(ev))
    assert "1 分钟前" in fresh and "你把" in fresh, fresh
    assert "你刚把" not in fresh, fresh

    src = _inspect.getsource(outbound_mod.ownership_hint)
    assert "OWNERSHIP_HINT_TTL_SECONDS" in src, "归属提示必须受时间上界约束"
    assert "_fmt_ago" in src, "必须带相对时间"
