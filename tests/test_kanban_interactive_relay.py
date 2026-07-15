"""交互式 create_subagent 的"执行体静默"登记语义回归。

背景（session ...644256 双份播报）：交互式 ``create_subagent(agent_profile=...)`` 会同步等
任务完成、把结论回执给主人格，由主人格转述一次。若 kanban_executor 完成时**又**自动推群，
同一份结论就被推两遍刷屏。修复：dispatcher 把 leaf-root 登记进
``_INTERACTIVE_RELAY_ROOTS``，executor 在终态判定处**读即弃**地消费该登记 → 静默；
主人格侧超时放弃转述时 dispatcher ``discard`` 掉，让 executor 恢复推群兜底。

这里锁死"消费一次"的核心不变量——它是整个无竞态设计的地基。
"""

import pytest

# kanban_executor 的导入链会拉起 skills / web_search 等重依赖；缺可选依赖的精简环境跳过整个文件
# （生产 / CI 有这些依赖时照常运行）。
_ke = pytest.importorskip("gsuid_core.ai_core.planning.kanban_executor")

mark_interactive_relay_root = _ke.mark_interactive_relay_root
discard_interactive_relay_root = _ke.discard_interactive_relay_root
_consume_interactive_relay = _ke._consume_interactive_relay


def test_marked_root_is_consumed_exactly_once() -> None:
    """登记后第一次消费返回 True 并移除；第二次消费返回 False（不会二次静默）。"""
    rid = "root_consume_once_001"
    mark_interactive_relay_root(rid)
    assert _consume_interactive_relay(rid) is True, "首次消费应命中静默"
    assert _consume_interactive_relay(rid) is False, "消费应读即弃，第二次不得再命中"


def test_unmarked_root_never_suppresses() -> None:
    """没登记过的 root（如后台 kanban 定时 tick）永远返回 False → 照常推群。"""
    assert _consume_interactive_relay("root_never_marked_xyz") is False


def test_discard_before_consume_restores_broadcast() -> None:
    """dispatcher 超时 discard 后，executor 消费不到 → 返回 False → 恢复推群兜底。"""
    rid = "root_timeout_discard_002"
    mark_interactive_relay_root(rid)
    discard_interactive_relay_root(rid)
    assert _consume_interactive_relay(rid) is False, "已 discard 的 root 不应再静默"


def test_discard_is_idempotent_and_safe_on_unknown() -> None:
    """discard 不存在的 root 不抛异常（幂等）。"""
    discard_interactive_relay_root("root_not_present_zzz")  # 不应抛
    assert _consume_interactive_relay("root_not_present_zzz") is False
