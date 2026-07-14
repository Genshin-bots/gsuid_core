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
    speaker_id = "3100542635"

    scope = make_scope_key(
        ScopeType.GROUP if group_id else ScopeType.USER_GLOBAL,
        group_id if group_id else speaker_id,
    )

    assert scope == "user_global:3100542635"
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
    """两个 observe 调用点 + 一个 dual_route 调用点都必须显式处理私聊为 None。"""
    for rel in ("gsuid_core/handler.py", "gsuid_core/ai_core/handle_ai.py"):
        src = _src(rel)
        assert "str(event.group_id) if event.group_id else None" in src, f"{rel} 未按「私聊传 None」写 group_id"


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
