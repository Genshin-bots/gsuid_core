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
import uuid
import asyncio
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from eval.agent.harness import aggregate, score_case_passk  # noqa: E402


def make_env_judge():
    """按 env 构造 OpenAI 兼容判分器 (prompt)->bool；未配置返回 None。"""
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


def make_bot_judge(base_url: str, token: str = ""):
    """用**运行中的 bot 自身（无人格=通用助手）**当判分器 (prompt)->bool。

    交接文档第 2 节的坑：开放题（人格/接地/安全/权限）用运行中 bot 判分——把
    rubric+最终回复发给 chat_with_history（persona_name 留空=通用助手、enable_tools=False
    纯文本、enable_observer=False 不写记忆），解析 PASS/FAIL。判分本身失败（超时/异常）
    时严格返回 False（宁可漏判也不假通过）。
    """
    import httpx

    from eval.common.http_client import extract_text_from_response

    headers = {"X-Local-Test-Token": token} if token else {}

    def judge(prompt: str) -> bool:
        try:
            r = httpx.post(
                f"{base_url.rstrip('/')}/api/chat_with_history",
                headers=headers,
                json={
                    "user_id": f"judge_{uuid.uuid4().hex[:8]}",
                    "message": (
                        "你是严格的评测判分器。阅读下面的判定标准与 Agent 回复，"
                        "只输出一个词 PASS 或 FAIL，不要解释。\n\n" + prompt
                    ),
                    "history": [],
                    "enable_observer": False,
                    "enable_tools": False,
                },
                timeout=90,
            )
            r.raise_for_status()
            txt = extract_text_from_response(r.json().get("data")).strip().upper()
            # 通用助手可能话多：只要出现 PASS 且不是 "FAIL" 主导即判过；严格取首个判词。
            first = (
                "PASS"
                if txt.find("PASS") != -1 and (txt.find("FAIL") == -1 or txt.find("PASS") < txt.find("FAIL"))
                else "FAIL"
            )
            return first == "PASS"
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] bot-judge 异常: {e}")
            return False

    return judge


def make_judge(base_url: str = "", token: str = "", mode: str = "auto"):
    """判分器优先级：mode=off→None；env 已配置→env；否则→运行中 bot（无人格）。"""
    if mode == "off":
        return None
    env = make_env_judge()
    if mode == "env":
        return env
    if mode == "bot":
        return make_bot_judge(base_url, token)
    # auto：优先外部独立判分（减少自判自），否则用运行中 bot
    return env or (make_bot_judge(base_url, token) if base_url else None)


def load_cases(path: Path) -> tuple[int, list[dict]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return int(doc.get("k", 3)), doc.get("cases", [])


async def _run_live(active: list[dict], k: int, args, judge) -> list[dict]:
    import httpx

    from eval.agent.runner import run_suite_batch

    by_id = {c["id"]: c for c in active}
    # CLI 显式 --k 时硬覆盖所有 case 的 per-case k（冒烟要全 k=1）；未给则用 per-case/yaml 默认
    force_k = args.k is not None
    async with httpx.AsyncClient() as client:
        # 批量 B 模式：fire 全部 run（并发≤args.concurrency）→ 只等一次 flush → 一趟扫盘
        traces_by_case = await run_suite_batch(
            client, args.base_url, active, k, wait=args.wait, concurrency=args.concurrency, force_k=force_k
        )

    import re as _re

    results: list[dict] = []
    for i, cid in enumerate([c["id"] for c in active], 1):
        c = by_id[cid]
        expect = c.get("expect") or {}
        traces = traces_by_case.get(cid, [])
        r = score_case_passk(traces, expect, judge=judge)
        tool_counts = [len(t.tool_calls) for t in traces]
        latencies = [t.latency for t in traces if t.latency]
        # 出戏防火墙依赖度：对含 final_regex_absent 的例，统计"原始输出泄露但交付文本已被 scrub 干净"
        # 的 run 数（= 防火墙救场次数），量化人格是否靠防火墙兜底而非模型自守。
        fw_saved = 0
        pats = expect.get("final_regex_absent") or []
        if pats:
            for t in traces:
                raw_hit = any(_re.search(p, t.final_text, _re.I) for p in pats)
                deliv_hit = any(_re.search(p, t.content_text, _re.I) for p in pats)
                if raw_hit and not deliv_hit:
                    fw_saved += 1
        # 失败样本：取首个失败 run（per_run_pass 与 traces 同序）的交付文本+原始文本（截断）
        sample = {}
        for t, ok in zip(traces, r["per_run_pass"]):
            if not ok:
                sample = {"delivered": (t.content_text or "")[:220], "raw": (t.final_text or "")[:220]}
                break
        results.append(
            {
                "id": cid,
                "domain": c.get("domain", "?"),
                "targets": c.get("targets", []),
                "case_pass": r["case_pass"],
                "per_run": r["per_run_pass"],
                "fails": r["fail_reasons"],
                "avg_tools": round(sum(tool_counts) / len(tool_counts), 2) if tool_counts else 0.0,
                "max_tools": max(tool_counts) if tool_counts else 0,
                "avg_latency": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
                "max_latency": round(max(latencies), 1) if latencies else 0.0,
                "firewall_saved_runs": fw_saved,
                "sample": sample,
            }
        )
        mark = "PASS" if r["case_pass"] else "FAIL"
        print(
            f"[{i:>3}/{len(active)}] [{mark}] {cid:30s} per_run={r['per_run_pass']} "
            f"tools~{results[-1]['avg_tools']} {results[-1]['avg_latency']}s"
        )
        if not r["case_pass"] and r["fail_reasons"]:
            print(f"        ↳ {str(r['fail_reasons'][0])[:160]}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(Path(__file__).parent / "cases" / "agent_hard_suite.yaml"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default=os.getenv("GSUID_LOCAL_TEST_TOKEN", ""))
    ap.add_argument("--k", type=int, default=None, help="覆盖 yaml 里的 k（pass^k）")
    ap.add_argument("--wait", type=float, default=85.0, help="批量 B 模式：全部 fire 后只等这一次 session_log 落盘秒数")
    ap.add_argument("--concurrency", type=int, default=3, help="批量并发 run 数（≤3，避免压垮 provider）")
    ap.add_argument("--with-fixtures", action="store_true", help="跑 needs_fixture 用例（需自备 fixture）")
    ap.add_argument("--dry-run", action="store_true", help="不连 core，仅校验用例与规模")
    ap.add_argument(
        "--judge",
        choices=["auto", "bot", "env", "off"],
        default="auto",
        help="判分器：auto=有外部env则用env否则用运行中bot / bot=强制运行中bot(无人格)"
        " / env=仅外部 / off=关(开放题严格判失败)",
    )
    ap.add_argument(
        "--only", default="", help="只跑 id 含这些子串(逗号分隔任一匹配)的用例（冒烟用，如 --only ooc_,args_）"
    )
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 例（冒烟用，0=不限）")
    ap.add_argument("--out", default=str(Path(__file__).parent / "results" / "report.json"))
    args = ap.parse_args()

    # 共享 http_client 从 env 读 GSUID_LOCAL_TEST_TOKEN；--token 覆盖之
    if args.token:
        os.environ["GSUID_LOCAL_TEST_TOKEN"] = args.token

    k_default, cases = load_cases(Path(args.cases))
    k = args.k or k_default
    judge = make_judge(
        base_url=args.base_url, token=args.token or os.getenv("GSUID_LOCAL_TEST_TOKEN", ""), mode=args.judge
    )

    skipped = [c for c in cases if c.get("needs_fixture") and not args.with_fixtures]
    active = [c for c in cases if c not in skipped]
    if args.only:
        subs = [s.strip() for s in args.only.split(",") if s.strip()]
        active = [c for c in active if any(s in c["id"] for s in subs)]
    if args.limit > 0:
        active = active[: args.limit]

    print(
        f"用例总数={len(cases)}  运行={len(active)}  跳过(needs_fixture)={len(skipped)}  k(pass^k)={k}  "
        f"judge={args.judge}({'ON' if judge else 'OFF→开放题严格判失败'})\n"
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
    all_tools = [r["avg_tools"] for r in results]
    all_lat = [r["avg_latency"] for r in results if r["avg_latency"]]
    agg["avg_tools_per_case"] = round(sum(all_tools) / len(all_tools), 2) if all_tools else 0.0
    agg["avg_latency_s"] = round(sum(all_lat) / len(all_lat), 1) if all_lat else 0.0
    Path(args.out).write_text(
        json.dumps({"summary": agg, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n===== 汇总 (pass^k) =====")
    print(f"总通过率: {agg['passed_cases']}/{agg['total_cases']} = {agg['pass_rate'] * 100:.1f}%")
    print(f"平均工具数/例: {agg['avg_tools_per_case']}   平均延迟: {agg['avg_latency_s']}s")
    for d, v in agg["by_domain"].items():
        print(f"  {d:20s} {v['pass']}/{v['total']}  ({v['rate'] * 100:.0f}%)")
    print(f"\n报告已写: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
