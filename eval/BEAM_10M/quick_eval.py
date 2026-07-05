"""BEAM 快速子集评测：只对指定类别的探针跑 probe+judge，复用已摄入的全量记忆。

全量摄入（10 plans）持久化在 DB，重启不丢；调参（注入预算 / 记忆使用准则 / 阈值）后**无需
重新摄入**，只需对受影响的少数类别快速重测，几分钟即可验证，而非每次跑满 20 题（~16min）。

用法：
  python eval/BEAM_10M/quick_eval.py --conv 0 --cats abstention,contradiction_resolution
  python eval/BEAM_10M/quick_eval.py --conv 0            # 不传 cats = 全部 20 题
"""

from __future__ import annotations

import os
import sys
import asyncio
import argparse
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from eval.common import DEFAULT_BASE_URL, load_json  # noqa: E402
from eval.BEAM_10M.run_beam_eval import (  # noqa: E402
    USER_ID_TEMPLATE,
    cmd_judge,
    cmd_probe,
    load_beam_dataset,
    iter_probing_questions,
)


async def _run(conv: int, cats: set[str] | None, base_url: str) -> int:
    rows = load_beam_dataset()
    row = rows[conv]
    probes = iter_probing_questions(row)
    if cats:
        probes = [p for p in probes if p[0] in cats]
    if not probes:
        print(f"[quick] 没有匹配的探针（cats={cats}）")
        return 2

    user_id = USER_ID_TEMPLATE.format(conv_id=conv)
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    tag = "_".join(sorted(cats)) if cats else "all"
    answers_file = os.path.join(out_dir, f"quick_answers_{conv}_{tag}.json")
    judge_file = os.path.join(out_dir, f"quick_judge_{conv}_{tag}.json")

    print(f"[quick] conv={conv} 探针 {len(probes)} 题，类别={sorted(cats) if cats else 'ALL'}")
    await cmd_probe(
        base_url=base_url,
        user_id=user_id,
        probes=probes,
        answers_file=answers_file,
        resume=False,
    )
    await cmd_judge(base_url=base_url, answers_file=answers_file, judge_file=judge_file, resume=False)

    recs = load_json(judge_file)
    by = defaultdict(lambda: {"p": 0, "t": 0})
    passed = 0
    for r in recs:
        j = r["judge"]
        c = r["category"]
        by[c]["t"] += 1
        by[c]["p"] += int(bool(j["passed"]))
        passed += int(bool(j["passed"]))
    print("\n" + "=" * 44)
    print(f"[quick] PASS {passed}/{len(recs)} = {passed / max(len(recs), 1) * 100:.0f}%")
    for c in sorted(by):
        s = by[c]
        print(f"  {c:26s} {s['p']}/{s['t']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="BEAM 快速子集评测（复用已摄入记忆）")
    ap.add_argument("--conv", type=int, default=0)
    ap.add_argument("--cats", default="", help="逗号分隔的类别，留空=全部 20 题")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = ap.parse_args()
    cats = {c.strip() for c in args.cats.split(",") if c.strip()} or None
    return asyncio.run(_run(args.conv, cats, args.base_url.rstrip("/")))


if __name__ == "__main__":
    raise SystemExit(main())
