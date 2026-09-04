"""记忆摄入的**落库可靠性**与**私聊 scope 归属**回归测试。

2026-07-15 排查：生产库里真实 QQ 流量的 Episode 数为 **0**，偏好记忆表里 46 条全是评测
数据、一条真实的都没有。两个独立 bug 叠加：

**Bug A（落库）**：flush 是唯一的落库时机，缓冲区在进程内存里。旧实现只有两个出口——
攒满 `batch_max_size`(80) 条，或距上次 flush 满 `batch_interval_seconds`(2 小时)。
于是一段几轮的对话要在内存里躺两小时；core 一重启缓冲区就蒸发。能持久化的 Episode
全部来自 webconsole / 评测端点，因为只有那些路径显式调了 `worker.flush_all()`。
→ 新增 `idle_flush_seconds`：对话静默即落库，且**不打断进行中对话的批量抽取**。

**Bug B（scope）**：`handler.py` / `handle_ai.py` 传 `group_id=event.group_id or event.user_id`，
私聊时 group_id 变成 user_id（非空）→ observer 按 `GROUP if group_id else USER_GLOBAL`
把私聊记忆写进 `group:{user_id}`。而 `AIMemPreference` 明确"主存 USER_GLOBAL"，
`dual_route_retrieve` 也注释着"私聊 group_id 为空 → user_global 是主 scope"。
→ 三个调用点私聊一律传 None。
"""

import ast
from typing import List
from pathlib import Path

from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── Bug B：私聊必须落 USER_GLOBAL ─────────────────────────────────


def test_private_chat_scope_is_user_global() -> None:
    """observer 的 scope 判据：group_id 为空 → USER_GLOBAL。私聊必须走这一支。"""
    group_id = None
    speaker_id = "100000006"

    scope = make_scope_key(
        ScopeType.GROUP if group_id else ScopeType.USER_GLOBAL,
        group_id if group_id else speaker_id,
    )

    assert scope == "user_global:100000006"
    assert not scope.startswith("group:"), "私聊记忆落进 group scope，偏好记忆将永远为空"


def _is_group_id_fallback(node: ast.AST) -> bool:
    """精确识别 `X.group_id or X.user_id` 这个回退写法本身。

    只认「两侧都是属性访问」的形态——黑名单检查里的
    `event.group_id in bl or event.user_id in bl` 是正当写法，不能误报。
    """
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    if len(node.values) != 2:
        return False
    left, right = node.values
    if not (isinstance(left, ast.Attribute) and isinstance(right, ast.Attribute)):
        return False
    return left.attr == "group_id" and right.attr == "user_id"


def test_no_memory_call_site_falls_back_group_id_to_user_id() -> None:
    """锁死回归：记忆链路的调用点不许把 group_id 回退成 user_id。

    `event.group_id or event.user_id` 在私聊时让 group_id 变成非空的 user_id，
    直接改变 observer / dual_route 的 scope 分支语义（4 个调用点全踩过）。
    """
    offenders: List[str] = []
    for rel in ("gsuid_core/handler.py", "gsuid_core/ai_core/handle_ai.py"):
        tree = ast.parse(_src(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and _is_group_id_fallback(node):
                offenders.append(f"{rel}:{node.lineno}  {ast.unparse(node)}")

    assert not offenders, "group_id 又被回退成 user_id 了（私聊记忆会掉进 group scope）：\n" + "\n".join(offenders)


def test_the_fallback_detector_actually_detects() -> None:
    """防止上面那条测试因为识别不出该写法而"空过"。"""
    bad = ast.parse("x = event.group_id or event.user_id").body[0]
    assert isinstance(bad, ast.Assign)
    assert _is_group_id_fallback(bad.value)

    ok = ast.parse("x = event.group_id in bl or event.user_id in bl").body[0]
    assert isinstance(ok, ast.Assign)
    assert not _is_group_id_fallback(ok.value), "黑名单检查被误报了"


def test_memory_call_sites_pass_none_for_private() -> None:
    """所有记忆调用点都必须显式处理私聊为 None。

    锁点变更：observe / 检索的调用点已迁进 ``kits/memory/kit.py``（H00 / H05），
    scope 构造集中在 ``cog_scope_from_ctx`` 与 ``AgentHookContext.group_id``——
    锁跟着代码搬（§9.3），不是删掉。
    """
    # 内核侧仍有 group_id 传参的地方
    kernel = _src("gsuid_core/ai_core/handle_ai.py")
    assert "str(event.group_id) if event.group_id else None" in kernel, "handle_ai 未按「私聊传 None」写 group_id"

    # 套件侧：group_id 一律取自 ctx.group_id（该属性私聊恒 None）
    kit = _src("gsuid_core/ai_core/kits/memory/kit.py")
    assert "group_id=ctx.group_id" in kit, "memory 套件未走 ctx.group_id"
    assert "group_id=ctx.group_id or" not in kit, "memory 套件把 group_id 回退了"

    # Context 的 group_id 属性本身必须私聊恒 None（这是上面那条能成立的前提）
    hooks = _src("gsuid_core/ai_core/hooks/models.py")
    assert "if self.ev is None or not self.ev.group_id:" in hooks
    assert "return None" in hooks

    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext
    from gsuid_core.ai_core.cognition import CogScope
    from gsuid_core.ai_core.kits.memory.kit import cog_scope_from_ctx

    ctx = AgentHookContext(point=AgentHookPoint.RETRIEVE_CONTEXT)
    scope = cog_scope_from_ctx(ctx)
    assert isinstance(scope, CogScope) and scope.group_id is None and scope.is_private


# ── Bug A：静默落库 ────────────────────────────────────────────────


class _FakeWorker:
    """只复刻 _should_flush_on_timer 依赖的状态，避免拉起真实 worker（要 DB/事件循环）。"""

    def __init__(self) -> None:
        self._last_flush: dict = {}
        self._last_activity: dict = {}


def _should_flush(worker, scope: str, now: float) -> bool:
    from gsuid_core.ai_core.memory.ingestion.worker import IngestionWorker

    return IngestionWorker._should_flush_on_timer(worker, scope, now)  # type: ignore[arg-type]


def test_idle_scope_is_flushed_without_waiting_two_hours(monkeypatch) -> None:
    """对话静默 → 落库。不静默等 2 小时，core 一重启记忆就没了。"""
    from gsuid_core.ai_core.memory import config as mem_config

    monkeypatch.setattr(mem_config.memory_config, "idle_flush_seconds", 180, raising=False)

    w = _FakeWorker()
    now = 10_000.0
    w._last_flush["group:1"] = now - 60  # 刚 flush 过，远未到 2 小时窗口
    w._last_activity["group:1"] = now - 200  # 但已经静默 200s > 180s

    assert _should_flush(w, "group:1", now), "静默的对话没有落库——旧 bug 复发"


def test_active_conversation_is_not_flushed_midway(monkeypatch) -> None:
    """对话进行中不打断批量：还在说话就不 flush，抽取仍是整段一次调用。"""
    from gsuid_core.ai_core.memory import config as mem_config

    monkeypatch.setattr(mem_config.memory_config, "idle_flush_seconds", 180, raising=False)

    w = _FakeWorker()
    now = 10_000.0
    w._last_flush["group:1"] = now - 60
    w._last_activity["group:1"] = now - 20  # 20 秒前还在说话

    assert not _should_flush(w, "group:1", now), "对话还在进行就 flush 了，白白拆碎批量抽取"


def test_long_running_scope_still_hits_the_window_ceiling(monkeypatch) -> None:
    """持续刷屏的 scope（永不静默）仍由 batch_interval_seconds 兜底落一次。"""
    from gsuid_core.ai_core.memory import config as mem_config

    monkeypatch.setattr(mem_config.memory_config, "idle_flush_seconds", 180, raising=False)
    monkeypatch.setattr(mem_config.memory_config, "batch_interval_seconds", 7200, raising=False)

    w = _FakeWorker()
    now = 10_000.0
    w._last_flush["group:1"] = now - 7300  # 距上次 flush 超过 2 小时
    w._last_activity["group:1"] = now - 5  # 一直在刷屏，从不静默

    assert _should_flush(w, "group:1", now), "刷屏 scope 连兜底窗口都没触发"


def test_idle_flush_can_be_disabled(monkeypatch) -> None:
    """idle_flush_seconds=0 → 完全退回旧行为（留一条退路）。"""
    from gsuid_core.ai_core.memory import config as mem_config

    monkeypatch.setattr(mem_config.memory_config, "idle_flush_seconds", 0, raising=False)
    monkeypatch.setattr(mem_config.memory_config, "batch_interval_seconds", 7200, raising=False)

    w = _FakeWorker()
    now = 10_000.0
    w._last_flush["group:1"] = now - 60
    w._last_activity["group:1"] = now - 9999  # 静默很久，但开关关了

    assert not _should_flush(w, "group:1", now)


def test_location_self_report_is_high_not_low_chitchat() -> None:
    """「我在广州噢」曾因 <10 字且无 6 字实体特征被打 LOW，闲聊进行中又要等 3 分钟才落库。"""
    from gsuid_core.ai_core.memory.observer import (
        _classify_value_tier,
        detect_location_self_report,
    )

    assert detect_location_self_report("我在广州噢")
    assert detect_location_self_report("我住在杭州")
    assert detect_location_self_report("我在广州")
    assert not detect_location_self_report("我在忙")
    assert not detect_location_self_report("我在开会啊")
    assert not detect_location_self_report("我在上班啊")
    assert not detect_location_self_report("唔…不知道")
    assert _classify_value_tier("我在广州噢") == "HIGH"


def test_unflushed_queue_is_visible_to_retrieval() -> None:
    """检索必须看见尚未 idle-flush 的原文，否则 coreclear 后「刚说的城市」会丢。"""
    import queue as sync_queue
    from datetime import datetime, timezone

    from gsuid_core.ai_core.memory.observer import (
        ObservationRecord,
        get_observation_queue,
        pending_episodes_for_scopes,
    )

    scope = "user_global:user_web_pending_test"
    rec = ObservationRecord(
        raw_content="我在广州噢",
        speaker_id="user_web_pending_test",
        group_id=None,
        scope_key=scope,
        timestamp=datetime.now(timezone.utc),
        message_type="private_msg",
        value_tier="HIGH",
    )
    q = get_observation_queue()
    q.put_nowait(rec)
    restored: list[ObservationRecord] = []
    try:
        eps = pending_episodes_for_scopes([scope])
        assert any("广州" in ep["content"] for ep in eps)
    finally:
        while True:
            try:
                item = q.get_nowait()
            except sync_queue.Empty:
                break
            if item is not rec and isinstance(item, ObservationRecord):
                restored.append(item)
        for item in restored:
            q.put_nowait(item)


def test_pending_skips_low_chitchat() -> None:
    """LOW 闲聊不得挤掉未落库的 HIGH 地点。"""
    import queue as sync_queue
    from datetime import datetime, timezone

    from gsuid_core.ai_core.memory.observer import (
        ObservationRecord,
        get_observation_queue,
        pending_episodes_for_scopes,
    )

    scope = "user_global:user_web_pending_low"
    ts = datetime.now(timezone.utc)
    low = ObservationRecord(
        raw_content="哈哈",
        speaker_id="user_web_pending_low",
        group_id=None,
        scope_key=scope,
        timestamp=ts,
        message_type="private_msg",
        value_tier="LOW",
    )
    high = ObservationRecord(
        raw_content="我在广州噢",
        speaker_id="user_web_pending_low",
        group_id=None,
        scope_key=scope,
        timestamp=ts,
        message_type="private_msg",
        value_tier="HIGH",
    )
    q = get_observation_queue()
    q.put_nowait(low)
    q.put_nowait(high)
    restored: list[ObservationRecord] = []
    try:
        eps = pending_episodes_for_scopes([scope])
        texts = [ep["content"] for ep in eps]
        assert any("广州" in t for t in texts)
        assert not any(t.strip() == "哈哈" for t in texts)
    finally:
        while True:
            try:
                item = q.get_nowait()
            except sync_queue.Empty:
                break
            if item is not low and item is not high and isinstance(item, ObservationRecord):
                restored.append(item)
        for item in restored:
            q.put_nowait(item)


def test_voice_anchor_budget_fits_anchor_plus_voice() -> None:
    """口吻截断包装后常约 102 字；旧预算 100 导致每轮 warning 两次。"""
    from gsuid_core.ai_core.kits.base import BLOCK_CHAR_BUDGET, _apply_block_budget

    typical = "x" * 102
    assert typical == _apply_block_budget("voice_anchor", typical)
    assert BLOCK_CHAR_BUDGET["voice_anchor"] >= 180
