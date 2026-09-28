"""测量命令循环的另一半成本：_sv_authorized 的逐 SV 鉴权。

循环 = O(#SV) 次 _sv_authorized + O(#Trigger) 次 check_command。
索引只解决后半段；这里量化前半段，判断它是否值得一并处理。
"""

from __future__ import annotations

import time

from gsuid_core.sv import SV, Plugins
from gsuid_core.models import Event as _Ev
from gsuid_core.handler import _sv_authorized

N_SV = 50
ROUNDS = 20000


def build_svs() -> list[SV]:
    out: list[SV] = []
    for i in range(N_SV):
        sv = SV.__new__(SV, f"Bench{i}")
        sv.name = f"Bench{i}"
        sv.priority = 5
        sv.pm = 6
        sv.area = "ALL"
        sv.enabled = True
        sv.black_list = []
        sv.white_list = []
        sv.TL = {"command": {}}
        sv.plugins = Plugins(name=f"Bench{i}", pm=6, priority=5, area="SV", force=True)
        out.append(sv)
    return out


def main() -> None:
    svs = build_svs()
    ev = _Ev("bench", "bench", "1", "group", "g1", "u1", {"nickname": "n"}, 6)

    def full_loop() -> int:
        n = 0
        for sv in svs:
            if _sv_authorized(sv, ev, 6):
                n += 1
        return n

    def plain_attr() -> int:
        n = 0
        for sv in svs:
            if sv.enabled and 6 <= sv.pm:
                n += 1
        return n

    def cache_probe() -> int:
        n = 0
        for sv in svs:
            n += 1
        return n

    a = time.perf_counter()
    for _ in range(ROUNDS):
        full_loop()
    ta = (time.perf_counter() - a) / ROUNDS * 1e6

    a = time.perf_counter()
    for _ in range(ROUNDS):
        plain_attr()
    tb = (time.perf_counter() - a) / ROUNDS * 1e6

    a = time.perf_counter()
    for _ in range(ROUNDS):
        cache_probe()
    tc = (time.perf_counter() - a) / ROUNDS * 1e6

    print(f"{N_SV} 个 SV / {ROUNDS} 轮\n")
    print(f"{'写法':<52}{'每次(µs)':>11}")
    print(f"{'完整 _sv_authorized 逐个判断':<52}{ta:>11.1f}")
    print(f"{'只比 enabled + pm':<52}{tb:>11.1f}")
    print(f"{'裸循环（仅下界参照）':<52}{tc:>11.1f}")

    print(f"\n鉴权占循环比例：{ta / tc:.0f}x 于裸循环；")
    print(f"对比 check_command 侧（892 触发器约 258~543µs），鉴权 {ta:.0f}µs 约占 {ta / 300 * 100:.0f}%。")
    print("→ 结论：#SV 只有几十，鉴权不是主瓶颈；")
    print("  但它对 #SV 是 O(n)，插件越多越贵，可按 (user_pm,user_type,group,user) 缓存。")


if __name__ == "__main__":
    main()
