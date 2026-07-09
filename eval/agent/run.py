"""Agent 硬核评测 · 入口。

用法：
  # 干跑（不连 core，仅校验用例/统计规模）——现在就能跑
  python -m eval.agent.run --dry-run

  # 实测（需运行中的 core + 已配置 LLM provider）
  export GSUID_LOCAL_TEST_TOKEN=xxx        # 与 core 的本地测试网关 token 一致
  python -m eval.agent.run --base-url http://127.0.0.1:8765 --token $GSUID_LOCAL_TEST_TOKEN --k 3

  # 接入 LLM 判分（开放题 L3；不接则 judge 类断言一律判失败=更严）
  export GSUID_EVAL_JUDGE_BASE_URL=https://api.xxx/v1
  export GSUID_EVAL_JUDGE_API_KEY=sk-...
  export GSUID_EVAL_JUDGE_MODEL=gpt-4o-mini
"""

from __future__ import annotations

import os
import sys
import json
import asyncio
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from eval.agent.harness import aggregate, score_case_passk  # noqa: E402


def make_judge():
    """按 env 构造 OpenAI 兼容判分器 (prompt)->bool；未配置返回 None（严格：judge 断言判失败）。"""
    base = os.getenv("GSUID_EVAL_JUDGE_BASE_URL")
    key = os.getenv("GSUID_EVAL_JUDGE_API_KEY")
    model = os.getenv("GSUID_EVAL_JUDGE_MODEL", "gpt-4o-mini")
    if not (base and key):
        return None
    import httpx

    def judge(prompt: str) -> bool:
        r = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "你是严格的评测判分器，只输出 PASS 或 FAIL。"},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=60,
        )
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"].strip().upper()
        return txt.startswith("PASS")

    return judge


def load_cases(path: Path) -> tuple[int, list[dict]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return int(doc.get("k", 3)), doc.get("cases", [])


async def _run_live(active: list[dict], k: int, args, judge) -> list[dict]:
    import httpx

    from eval.agent.runner import run_case

    results: list[dict] = []
    async with httpx.AsyncClient() as client:
        for i, c in enumerate(active, 1):
            traces = await run_case(client, args.base_url, c, k, wait=args.wait)
            r = score_case_passk(traces, c.get("expect") or {}, judge=judge)
            results.append(
                {
                    "id": c["id"],
                    "domain": c.get("domain", "?"),
                    "targets": c.get("targets", []),
                    "case_pass": r["case_pass"],
                    "per_run": r["per_run_pass"],
                    "fails": r["fail_reasons"],
                }
            )
            mark = "PASS" if r["case_pass"] else "FAIL"
            print(f"[{i:>2}/{len(active)}] [{mark}] {c['id']:26s} per_run={r['per_run_pass']}")
            if not r["case_pass"] and r["fail_reasons"]:
                print(f"        ↳ {r['fail_reasons'][0][:2]}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(Path(__file__).parent / "cases" / "agent_hard_suite.yaml"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default=os.getenv("GSUID_LOCAL_TEST_TOKEN", ""))
    ap.add_argument("--k", type=int, default=None, help="覆盖 yaml 里的 k（pass^k）")
    ap.add_argument("--wait", type=float, default=75.0, help="B 模式等 session_log 落盘秒数")
    ap.add_argument("--with-fixtures", action="store_true", help="跑 needs_fixture 用例（需自备 fixture）")
    ap.add_argument("--dry-run", action="store_true", help="不连 core，仅校验用例与规模")
    ap.add_argument("--out", default=str(Path(__file__).parent / "report.json"))
    args = ap.parse_args()

    # 共享 http_client 从 env 读 GSUID_LOCAL_TEST_TOKEN；--token 覆盖之
    if args.token:
        os.environ["GSUID_LOCAL_TEST_TOKEN"] = args.token

    k_default, cases = load_cases(Path(args.cases))
    k = args.k or k_default
    judge = make_judge()

    skipped = [c for c in cases if c.get("needs_fixture") and not args.with_fixtures]
    active = [c for c in cases if c not in skipped]

    print(
        f"用例总数={len(cases)}  运行={len(active)}  跳过(needs_fixture)={len(skipped)}  k(pass^k)={k}  "
        f"judge={'ON' if judge else 'OFF(开放题严格判失败)'}\n"
    )

    if args.dry_run:
        from collections import Counter

        dom = Counter(c.get("domain", "?") for c in cases)
        for d, n in sorted(dom.items()):
            print(f"  domain {d:20s} {n} 例")
        # 校验 verifier key 合法
        from eval.agent.harness import VERIFIERS

        bad = []
        for c in cases:
            for kk in c.get("expect") or {}:
                if kk not in VERIFIERS:
                    bad.append((c["id"], kk))
        print(f"\n未知 verifier: {bad if bad else '无（全部用例的 expect 键合法）'}")
        print("dry-run 完成。要实测请去掉 --dry-run 并提供 --base-url/--token（core 需在跑）。")
        return 0

    results = asyncio.run(_run_live(active, k, args, judge))

    agg = aggregate(results)
    Path(args.out).write_text(
        json.dumps({"summary": agg, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n===== 汇总 (pass^k) =====")
    print(f"总通过率: {agg['passed_cases']}/{agg['total_cases']} = {agg['pass_rate'] * 100:.1f}%")
    for d, v in agg["by_domain"].items():
        print(f"  {d:20s} {v['pass']}/{v['total']}  ({v['rate'] * 100:.0f}%)")
    print(f"\n报告已写: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
