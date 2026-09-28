"""对两次 cProfile 快照做差，隔离"消息处理"本身的耗时。

为什么要做差：core 空转时就有 ~1 核的定时任务/心跳/TTL，直接看绝对排名会被
启动与后台作业淹没。压消息前取基线、压完取快照，只留下这批消息新增的开销。

注意：`pstats.Stats(a, b)` 是"合并"不是"做差"（第二个位置参数其实是输出流），
`Stats.stats` / `Stats.ct` 又不在 typeshed stub 里。所以这里用 marshal 直读 .prof
文件，逐层 isinstance 收窄，不用 cast / Any。

用法：
    uv run python eval/manual/analyze_core_profile.py <baseline.prof> <loaded.prof>
"""

from __future__ import annotations

import sys
import marshal
from pathlib import Path

# profile 文件里的函数键：(文件名, 行号, 函数名)
FuncKey = tuple[str, int, str]
# 每个函数的统计：调用数 / tottime / cumtime
FuncStat = tuple[int, float, float]

# 关注的环节：显示名 -> 匹配关键字（对文件路径或函数名做子串匹配）
WATCH: list[tuple[str, str]] = [
    ("① 消息入口 handle_event", "handle_event"),
    ("② AI 入站 hook 扇出 fire_hooks", "fire_hooks"),
    ("③ 历史入库 add_message", "add_message"),
    ("④ 用户/群记账入口", "_schedule_user_group_write"),
    ("⑤ 缓冲刷写", "_flush_user_group_buffer"),
    ("⑥ 命令匹配 check_command", "check_command"),
    ("⑦ 候选索引 candidates", "candidates"),
    ("⑧ msg_process 建 Event", "msg_process"),
    ("⑨ 命令体（trigger 的 func）", "modify_func"),
    ("⑩ 出图/渲染", "render_html_to_bytes"),
    ("⑪ 写库 insert_user", "insert_user"),
    ("⑫ 写库 insert_group", "insert_group"),
    ("⑬ 记忆摄取", "observe"),
    ("⑭ 日志", "logger.py"),
    ("⑮ 事件循环调度", "base_events"),
]


def _read(path: Path) -> dict[FuncKey, FuncStat]:
    """读一份 .prof，返回 {func_key: (ncalls, tottime, cumtime)}。

    文件结构是 (version, timestamp, {func: (cc, nc, tt, ct, callers)})，
    这里逐层 isinstance 收窄，不让 Any 往下流。
    """
    with path.open("rb") as f:
        raw: object = marshal.load(f)
    if not isinstance(raw, tuple) or len(raw) < 3:
        return {}
    table = raw[2]
    if not isinstance(table, dict):
        return {}
    out: dict[FuncKey, FuncStat] = {}
    for key, val in table.items():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        name, lineno, func = key
        if not isinstance(name, str) or not isinstance(lineno, int) or not isinstance(func, str):
            continue
        if not isinstance(val, tuple) or len(val) < 4:
            continue
        _cc, nc, tt, ct = val[0], val[1], val[2], val[3]
        if not isinstance(nc, int) or not isinstance(tt, float) or not isinstance(ct, float):
            continue
        out[(name, lineno, func)] = (nc, tt, ct)
    return out


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    bstats = _read(Path(sys.argv[1]))
    lstats = _read(Path(sys.argv[2]))

    rows: list[tuple[float, int, str, str]] = []
    for func, (nc, tt, _ct) in lstats.items():
        b_nc, b_tt, _b_ct = bstats.get(func, (0, 0.0, 0.0))
        d_tt = tt - b_tt
        d_nc = nc - b_nc
        if d_tt > 0.0005 or d_nc != 0:
            rows.append((d_tt, d_nc, f"{Path(func[0]).name}:{func[1]}({func[2]})", func[0]))
    rows.sort(key=lambda r: r[0], reverse=True)

    total = sum(r[0] for r in rows)
    print("=" * 84)
    print(f"压测期间新增耗时（tottime 差值）  合计 {total:.2f}s")
    print("=" * 84)
    print(f"{'函数':<62}{'tottime(s)':>11}{'占比':>8}{'ncalls':>9}")
    for tt, nc, short, _fp in rows[:28]:
        share = tt / total * 100 if total else 0
        print(f"{short[-61:]:<62}{tt:>11.2f}{share:>7.1f}%{nc:>9}")

    print()
    print("=" * 84)
    print("按关注环节归并（tottime 差值）")
    print("=" * 84)
    print(f"{'环节':<34}{'tottime(s)':>12}{'占比':>8}{'ncalls':>9}")
    for label, key in WATCH:
        tt = 0.0
        nc = 0
        for d_tt, d_nc, _short, fp in rows:
            if key in fp or key in _short:
                tt += d_tt
                nc += d_nc
        if nc:
            share = tt / total * 100 if total else 0
            print(f"{label:<34}{tt:>12.2f}{share:>7.1f}%{nc:>9}")

    print()
    print("=" * 84)
    print("cumtime 差值前 20（看调用链上的大头）")
    print("=" * 84)
    crows: list[tuple[float, int, str]] = []
    for func, (nc, _tt, ct) in lstats.items():
        _b_nc, _b_tt, b_ct = bstats.get(func, (0, 0.0, 0.0))
        d_ct = ct - b_ct
        if d_ct > 0.01:
            crows.append((d_ct, nc, f"{Path(func[0]).name}:{func[1]}({func[2]})"))
    crows.sort(key=lambda r: r[0], reverse=True)
    print(f"{'函数':<62}{'cumtime(s)':>12}{'ncalls':>9}")
    for ct, nc, short in crows[:20]:
        print(f"{short[-61:]:<62}{ct:>12.2f}{nc:>9}")


if __name__ == "__main__":
    main()
