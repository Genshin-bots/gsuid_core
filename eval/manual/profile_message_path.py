"""分阶段剖析：一条消息进入 handle_event 后，CPU 到底花在哪。

回答"匹配提速 8x 能不能变成吞吐"——前提是知道匹配在整条链路里占多少。
只测 handle_event 的**同步段**（到命令分发为止），异步段（DB 写、AI hook、
命令体本身）另作说明。

用法：
    uv run python eval/manual/profile_message_path.py
"""

from __future__ import annotations

import time
from collections.abc import Callable

from gsuid_core.sv import SL, SV, Plugins
from gsuid_core.models import Event, Message
from gsuid_core.trigger import Trigger
from gsuid_core.trigger_index import TriggerIndex
from eval.manual.bench_trigger_match import collect_real_specs

ROUNDS = 300


async def _noop(bot, ev):  # noqa: ANN001, ANN202
    return None


def build_registry() -> tuple[list[Trigger], SV]:
    specs = collect_real_specs()
    tl: dict[str, dict[str, Trigger]] = {}
    out: list[Trigger] = []
    for tname, kw in specs:
        bucket = tl.setdefault(tname, {})
        for p in ("", "原神", "崩铁", "鸣潮"):
            tr = Trigger(tname, kw, _noop, p, False, False)  # type: ignore[arg-type]
            bucket[p + kw] = tr
            out.append(tr)
    sv = SV.__new__(SV, "__prof__")
    sv.name = "__prof__"
    sv.TL = tl
    return out, sv


def bench(fn: Callable[[], int], rounds: int = ROUNDS) -> float:
    start = time.perf_counter()
    for _ in range(rounds):
        fn()
    return (time.perf_counter() - start) / rounds * 1e6


def main() -> None:
    triggers, sv = build_registry()
    saved = dict(SL.lst)
    SL.lst.clear()
    SL.lst[sv.name] = sv
    try:
        index = TriggerIndex()
        text = "今天天气真不错啊大家吃了吗"

        def make_msg() -> dict:
            return {
                "bot_id": "ProfBot",
                "bot_self_id": "99999",
                "msg_id": "p-1",
                "user_type": "group",
                "group_id": "99000003",
                "user_id": "99000003",
                "sender": {"nickname": "p", "user_id": 99000003},
                "user_pm": 6,
                "content": [Message(type="text", data=text)],
            }

        ev = Event("ProfBot", "99999", "p-1", "group", "99000003", "99000003", {"nickname": "p"}, 6)
        ev.raw_text = text
        ev.text = text

        # --- 阶段 1：msg_process 类的同步构建 ---
        def build_event() -> int:
            e = Event("ProfBot", "99999", "p-1", "group", "99000003", "99000003", {"nickname": "p"}, 6)
            e.raw_text += text
            e.text += text
            return len(e.raw_text)

        t_build = bench(build_event)

        # --- 阶段 2：配置类读取（get_user_pml 的形态）---
        from gsuid_core.config import core_config

        masters = core_config.get_config("masters")
        superusers = core_config.get_config("superusers")

        def read_pm_cfg() -> int:
            uid = "99000003"
            if uid in masters:
                return 0
            if uid in superusers:
                return 1
            return 6

        t_cfg = bench(read_pm_cfg)

        # --- 阶段 3：鉴权（_sv_authorized 形态，按 #SV）---
        svs = []
        for i in range(50):
            s = SV.__new__(SV, f"P{i}")
            s.name = f"P{i}"
            s.priority = 5
            s.pm = 6
            s.area = "ALL"
            s.enabled = True
            s.black_list = []
            s.white_list = []
            s.TL = {"command": {}}
            s.plugins = Plugins(name=f"P{i}", pm=6, priority=5, area="SV", force=True)
            svs.append(s)

        from gsuid_core.handler import _sv_authorized

        def auth() -> int:
            n = 0
            for s in svs:
                if _sv_authorized(s, ev, 6):
                    n += 1
            return n

        t_auth = bench(auth)

        # --- 阶段 4：命令匹配（改动前 vs 改动后）---
        def linear() -> int:
            n = 0
            for tr in triggers:
                if tr.check_command(ev):
                    n += 1
            return n

        def indexed() -> int:
            n = 0
            for tr in index.candidates(ev):
                if tr.check_command(ev):
                    n += 1
            return n

        t_linear = bench(linear)
        t_indexed = bench(indexed)

        total_before = t_build + t_cfg + t_auth + t_linear
        total_after = t_build + t_cfg + t_auth + t_indexed

        print("单条消息 handle_event 同步段各阶段耗时（µs）")
        print(f"{'阶段':<34}{'耗时':>10}{'占比(改前)':>12}")
        print(f"{'构建 Event / msg_process 类':<34}{t_build:>10.1f}{t_build / total_before * 100:>11.1f}%")
        print(f"{'配置读取 / get_user_pml 类':<34}{t_cfg:>10.1f}{t_cfg / total_before * 100:>11.1f}%")
        print(f"{'SV 级鉴权 x50':<34}{t_auth:>10.1f}{t_auth / total_before * 100:>11.1f}%")
        print(f"{'命令匹配(改前 全量扫描)':<34}{t_linear:>10.1f}{t_linear / total_before * 100:>11.1f}%")
        print(f"{'命令匹配(改后 索引)':<34}{t_indexed:>10.1f}")
        print("-" * 58)
        print(f"{'同步段合计(改前)':<34}{total_before:>10.1f}")
        print(f"{'同步段合计(改后)':<34}{total_after:>10.1f}")
        print(f"{'同步段提速':<34}{total_before / total_after:>9.2f}x")
        print()
        print(f"匹配占同步段：改前 {t_linear / total_before * 100:.1f}% -> 改后 {t_indexed / total_after * 100:.1f}%")
    finally:
        SL.lst.clear()
        SL.lst.update(saved)

    print()
    print("注意：以上只含 handle_event 的同步段。以下不在其中，但同样吃 CPU：")
    print("  - 每条消息的 CoreUser/CoreGroup 写库（已改为 60s 内存缓冲批量刷，入口零 IO）")
    print("  - AI 入站 hook 扇出（记忆/表情/图片观察）")
    print("  - 历史入库")
    print("  - 命中后命令体本身（日志实测单条 help 端到端 11~14 秒）")
    print("  - 心跳/定时任务/TTL 清理等后台作业（实测 core 空闲即占 ~1.5 核）")


if __name__ == "__main__":
    main()
