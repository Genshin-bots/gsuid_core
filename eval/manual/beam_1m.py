"""BEAM 分阶段评测。默认只灌 plan 1（约 1.2M）；20 道探针金标覆盖 10 个 plan，
完整作答用 ``--plans 1,2,3,4,5,6,7,8,9,10 --out eval/BEAM_official/results/10m``。

阶段：
  ping     确认 core / local-test gate
  ingest   clear + 摄入指定 plan + rebuild
  smoke5   先测 5 题并 judge
  domain   再测一个完整类别并 judge
  finish   该 conv 剩余探针 + judge
  conv     ingest → smoke5 → domain → finish（单对话）
  all      conv 0..9

默认输出 ``eval/BEAM_official/results/1m/``，不覆盖历史 ``answers_0.json``。
探针走生产 Chat：评测助手 + enable_tools + memory_eval=False + clock_at。
"""

from __future__ import annotations

import gc
import os
import sys
import json
import time
import socket
import asyncio
import argparse
from typing import Any
from collections import defaultdict
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx  # noqa: E402

from eval.common import DEFAULT_BASE_URL, load_json  # noqa: E402
from eval.common.beam_runner import (  # noqa: E402
    DEFAULT_TIMEOUT,
    USER_ID_TEMPLATE,
    cmd_clear,
    cmd_judge,
    cmd_probe,
    load_beam_row,
    load_beam_plan,
    cmd_ingest_plan,
    iter_probing_questions,
)

OUT_DIR = os.path.join(_ROOT, "eval", "BEAM_official", "results", "1m")
PROGRESS = os.path.join(OUT_DIR, "progress.json")
DEFAULT_DOMAIN = "information_extraction"
DEFAULT_PLANS = "1"


def _configure_out(out: str) -> None:
    global OUT_DIR, PROGRESS
    if not out:
        return
    OUT_DIR = out if os.path.isabs(out) else os.path.join(_ROOT, out)
    PROGRESS = os.path.join(OUT_DIR, "progress.json")


def _parse_plans(raw: str) -> list[int]:
    ids = [int(x) for x in raw.split(",") if x.strip()]
    if not ids:
        raise SystemExit("--plans 为空")
    return ids


def _progress() -> dict[str, Any]:
    if os.path.isfile(PROGRESS):
        raw = load_json(PROGRESS)
        if isinstance(raw, dict):
            return raw
    return {"ingest": [], "smoke5": [], "domain": [], "finish": []}


def _save_progress(doc: dict[str, Any]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)


def _answers_path(conv: int) -> str:
    return os.path.join(OUT_DIR, f"answers_{conv}.json")


def _judge_path(conv: int) -> str:
    return os.path.join(OUT_DIR, f"judge_{conv}.json")


def _mark(stage: str, conv: int) -> None:
    doc = _progress()
    ids = doc[stage] if stage in doc and isinstance(doc[stage], list) else []
    if conv not in ids:
        ids.append(conv)
    doc[stage] = ids
    _save_progress(doc)


async def cmd_ping(base_url: str) -> int:
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    headers = {"X-Local-Test-Token": token} if token else {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        try:
            spec = await client.get(f"{base_url}/openapi.json")
            print(f"[ping] openapi={spec.status_code}")
        except httpx.HTTPError as e:
            print(f"[ping] openapi skip: {e}")
        resp = await client.post(
            f"{base_url}/api/chat_with_history",
            headers=headers,
            json={
                "user_id": "beam_1m_ping",
                "message": "ping",
                "history": [],
                "persona_name": "评测助手",
                "enable_observer": False,
                "enable_tools": False,
            },
        )
        print(f"[ping] chat_with_history={resp.status_code} body={resp.text[:240]}")
        if resp.status_code != 200:
            return 2
        print("[ping] OK")
        return 0


def _load_row(conv: int, *, full: bool) -> dict[str, Any]:
    cols = None if full else ["probing_questions"]
    return load_beam_row(conv, columns=cols)


async def _wait_core(host: str, port: int, timeout: float = 300.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                break
        except OSError:
            await asyncio.sleep(2.0)
    else:
        return False
    # uvicorn 会先听端口，评测路由后挂；404 表示还没就绪或 gate 未开。
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    headers = {"X-Local-Test-Token": token} if token else {}
    url = f"http://{host}:{port}/api/ai/memory/batch_observe"
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        while time.time() < deadline:
            try:
                resp = await client.post(url, headers=headers, json={})
                if resp.status_code != 404:
                    print(f"[campaign] eval api ready status={resp.status_code}", flush=True)
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
    return False


def _write_10m_report() -> None:
    """汇总 10m 答卷；缺 judge 的 conv 仍占一行。"""
    lines: list[str] = [
        "# BEAM-10M（10 plan 累计）汇总",
        "",
        "口径：生产 Chat（评测助手 + enable_tools + memory_eval=False + clock_at）。",
        "每条 conversation 灌入 plan 1–10 后再答同一套 20 题。输出目录 `eval/BEAM_official/results/10m/`。",
        "不覆盖 `results/1m/` 与历史 `answers_0.json`。",
        "",
    ]
    passed_all = 0
    total_all = 0
    ooc = 0
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    conv_rows: list[str] = []
    for conv in range(10):
        jp = _judge_path(conv)
        ap = _answers_path(conv)
        if not os.path.isfile(jp):
            conv_rows.append(f"| {conv} | — | 缺 judge |")
            continue
        recs = load_json(jp)
        if not isinstance(recs, list):
            conv_rows.append(f"| {conv} | — | judge 损坏 |")
            continue
        ok = 0
        n = 0
        for r in recs:
            if not isinstance(r, dict):
                continue
            j = r["judge"] if "judge" in r else {}
            if not isinstance(j, dict):
                continue
            cat = str(r["category"]) if "category" in r else "?"
            hit = bool(j["passed"]) if "passed" in j else False
            by_cat[cat][1] += 1
            by_cat[cat][0] += int(hit)
            ok += int(hit)
            n += 1
        passed_all += ok
        total_all += n
        if os.path.isfile(ap):
            answers = load_json(ap)
            if isinstance(answers, list):
                for a in answers:
                    if not isinstance(a, dict):
                        continue
                    text = str(a["agent_answer"] if "agent_answer" in a else "")
                    if "这个不太想说呢" in text:
                        ooc += 1
        conv_rows.append(f"| {conv} | {ok}/{n} | |")
    pct = f"{100.0 * passed_all / total_all:.1f}%" if total_all else "n/a"
    lines.append(f"**总分：{passed_all}/{total_all}（{pct}）**。输出闸「这个不太想说呢」{ooc}/{total_all}。")
    lines.extend(["", "## 分 conversation", "", "| conv | 分数 | 备注 |", "|------|------|------|"])
    lines.extend(conv_rows)
    lines.extend(["", "## 按类", "", "| 类别 | 过线 |", "|------|------|"])
    for c in sorted(by_cat):
        lines.append(f"| {c} | {by_cat[c][0]}/{by_cat[c][1]} |")
    lines.extend(
        [
            "",
            "## 对照",
            "",
            "1M 官方 ladder 见 `eval/BEAM_official/results/1m/`。",
            "",
        ]
    )
    path = os.path.join(OUT_DIR, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[campaign] wrote {path}")


def _write_run_report(plan_ids: list[int]) -> None:
    """汇总当前 OUT_DIR 的 10 个 conv judge。1M / 多 plan 共用。"""
    plans = ",".join(str(x) for x in plan_ids)
    title = "BEAM-1M（只灌 plan 1）" if plan_ids == [1] else f"BEAM plans={plans}"
    lines: list[str] = [
        f"# {title} 汇总",
        "",
        "口径：生产 Chat（评测助手 + enable_tools + memory_eval=False + clock_at）。",
        f"plans={plans}。输出目录 `{OUT_DIR}`。",
        "",
    ]
    passed_all = 0
    total_all = 0
    ooc = 0
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    conv_rows: list[str] = []
    for conv in range(10):
        jp = _judge_path(conv)
        ap = _answers_path(conv)
        if not os.path.isfile(jp):
            conv_rows.append(f"| {conv} | — | 缺 judge |")
            continue
        recs = load_json(jp)
        if not isinstance(recs, list):
            conv_rows.append(f"| {conv} | — | judge 损坏 |")
            continue
        ok = 0
        n = 0
        for r in recs:
            if not isinstance(r, dict):
                continue
            j = r["judge"] if "judge" in r else {}
            if not isinstance(j, dict):
                continue
            cat = str(r["category"]) if "category" in r else "?"
            hit = bool(j["passed"]) if "passed" in j else False
            by_cat[cat][1] += 1
            by_cat[cat][0] += int(hit)
            ok += int(hit)
            n += 1
        passed_all += ok
        total_all += n
        if os.path.isfile(ap):
            answers = load_json(ap)
            if isinstance(answers, list):
                for a in answers:
                    if not isinstance(a, dict):
                        continue
                    text = str(a["agent_answer"] if "agent_answer" in a else "")
                    if "这个不太想说呢" in text:
                        ooc += 1
        conv_rows.append(f"| {conv} | {ok}/{n} | |")
    pct = f"{100.0 * passed_all / total_all:.1f}%" if total_all else "n/a"
    lines.append(f"**总分：{passed_all}/{total_all}（{pct}）**。输出闸「这个不太想说呢」{ooc}/{total_all}。")
    lines.extend(["", "## 分 conversation", "", "| conv | 分数 | 备注 |", "|------|------|------|"])
    lines.extend(conv_rows)
    lines.extend(["", "## 按类", "", "| 类别 | 过线 |", "|------|------|"])
    for c in sorted(by_cat):
        lines.append(f"| {c} | {by_cat[c][0]}/{by_cat[c][1]} |")
    path = os.path.join(OUT_DIR, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[report] {passed_all}/{total_all} ({pct}) -> {path}", flush=True)


async def cmd_ingest(
    base_url: str,
    conv: int,
    timeout: float,
    *,
    force: bool,
    plan_ids: list[int],
    clear: bool = True,
) -> int:
    user_id = USER_ID_TEMPLATE.format(conv_id=conv)
    prog = _progress()
    if conv in prog.get("ingest", []) and not force:
        print(f"[ingest] conv={conv} 已标记完成，跳过（--force 可重灌）")
        return 0
    if clear:
        cleared = await cmd_clear(base_url, user_id, timeout=timeout)
        st = cleared["status"] if isinstance(cleared, dict) and "status" in cleared else 1
        if st != 0:
            print(f"[ingest] conv={conv} clear 失败，停止以免往脏库上灌")
            return 2
    results: list[dict[str, Any]] = []
    n = len(plan_ids)
    for i, pid in enumerate(plan_ids):
        one = load_beam_plan(conv, pid)
        r = await cmd_ingest_plan(
            base_url=base_url,
            user_id=user_id,
            plan=one,
            flush=True,
            trigger_rebuild=(i + 1 == n),
            timeout=timeout,
        )
        results.append(r)
        del one
        gc.collect()
        resp = r["response"] if isinstance(r, dict) and "response" in r else {}
        if not isinstance(resp, dict) or resp.get("status") != 0:
            break
    failed = [
        r
        for r in results
        if not isinstance(r, dict) or not isinstance(r.get("response"), dict) or r["response"].get("status") != 0
    ]
    if failed:
        print(f"[ingest] conv={conv} 失败 {failed!r}")
        return 2
    _mark("ingest", conv)
    print(f"[ingest] conv={conv} plans={plan_ids} done")
    return 0


def _summarize_judge(path: str) -> tuple[int, int]:
    recs = load_json(path)
    if not isinstance(recs, list):
        return 0, 0
    passed = 0
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in recs:
        if not isinstance(r, dict):
            continue
        j = r["judge"] if "judge" in r else {}
        if not isinstance(j, dict):
            continue
        cat = str(r["category"]) if "category" in r else "?"
        ok = bool(j["passed"]) if "passed" in j else False
        by[cat][1] += 1
        by[cat][0] += int(ok)
        passed += int(ok)
    total = sum(v[1] for v in by.values())
    print(f"[judge] {passed}/{total}  {path}")
    for c in sorted(by):
        print(f"  {c:30s} {by[c][0]}/{by[c][1]}")
    return passed, total


def _answers_sane(path: str, n_expect: int) -> bool:
    recs = load_json(path)
    if not isinstance(recs, list) or len(recs) < n_expect:
        print(f"[sanity] 答卷条数 {0 if not isinstance(recs, list) else len(recs)} < {n_expect}")
        return False
    bad = 0
    for a in recs[-n_expect:]:
        if not isinstance(a, dict):
            bad += 1
            continue
        status = a["status_code"] if "status_code" in a else -1
        text = str(a["agent_answer"] if "agent_answer" in a else "")
        if status not in (200, 0) or text.startswith("[ERROR]") or not text.strip():
            bad += 1
            print(f"[sanity] FAIL {a.get('question_id')} status={status} {text[:120]!r}")
    print(f"[sanity] bad={bad}/{n_expect}")
    return bad == 0


async def cmd_smoke5(base_url: str, conv: int, timeout: float) -> int:
    user_id = USER_ID_TEMPLATE.format(conv_id=conv)
    row = _load_row(conv, full=False)
    probes = iter_probing_questions(row)[:5]
    if len(probes) < 5:
        print(f"[smoke5] 只有 {len(probes)} 题")
        return 2
    answers = _answers_path(conv)
    await cmd_probe(
        base_url=base_url,
        user_id=user_id,
        probes=probes,
        answers_file=answers,
        timeout=timeout,
        resume=True,
    )
    if not _answers_sane(answers, 5):
        return 2
    await cmd_judge(
        base_url=base_url,
        answers_file=answers,
        judge_file=_judge_path(conv),
        timeout=timeout,
        resume=True,
    )
    _summarize_judge(_judge_path(conv))
    _mark("smoke5", conv)
    return 0


async def cmd_domain(base_url: str, conv: int, timeout: float, cat: str) -> int:
    user_id = USER_ID_TEMPLATE.format(conv_id=conv)
    row = _load_row(conv, full=False)
    probes = [p for p in iter_probing_questions(row) if p[0] == cat]
    if not probes:
        print(f"[domain] 无类别 {cat}")
        return 2
    answers = _answers_path(conv)
    before = len(load_json(answers)) if os.path.isfile(answers) else 0
    await cmd_probe(
        base_url=base_url,
        user_id=user_id,
        probes=probes,
        answers_file=answers,
        timeout=timeout,
        resume=True,
    )
    after = len(load_json(answers)) if os.path.isfile(answers) else 0
    added = max(after - before, 0)
    if added and not _answers_sane(answers, added):
        return 2
    await cmd_judge(
        base_url=base_url,
        answers_file=answers,
        judge_file=_judge_path(conv),
        timeout=timeout,
        resume=True,
    )
    _summarize_judge(_judge_path(conv))
    _mark("domain", conv)
    return 0


async def cmd_finish(base_url: str, conv: int, timeout: float) -> int:
    user_id = USER_ID_TEMPLATE.format(conv_id=conv)
    row = _load_row(conv, full=False)
    probes = iter_probing_questions(row)
    answers = _answers_path(conv)
    await cmd_probe(
        base_url=base_url,
        user_id=user_id,
        probes=probes,
        answers_file=answers,
        timeout=timeout,
        resume=True,
    )
    await cmd_judge(
        base_url=base_url,
        answers_file=answers,
        judge_file=_judge_path(conv),
        timeout=timeout,
        resume=True,
    )
    _summarize_judge(_judge_path(conv))
    _mark("finish", conv)
    return 0


async def cmd_conv(
    base_url: str,
    conv: int,
    timeout: float,
    cat: str,
    *,
    force_ingest: bool,
    skip_smoke: bool,
    plan_ids: list[int],
) -> int:
    rc = await cmd_ingest(base_url, conv, timeout, force=force_ingest, plan_ids=plan_ids)
    if rc:
        return rc
    if not skip_smoke:
        rc = await cmd_smoke5(base_url, conv, timeout)
        if rc:
            print("[conv] smoke5 未通过，停下。修好后再跑。")
            return rc
        rc = await cmd_domain(base_url, conv, timeout, cat)
        if rc:
            print("[conv] domain 未通过，停下。")
            return rc
    return await cmd_finish(base_url, conv, timeout)


async def cmd_all(
    base_url: str,
    timeout: float,
    cat: str,
    *,
    force_ingest: bool,
    plan_ids: list[int],
) -> int:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    print(f"[all] wait {host}:{port}", flush=True)
    if not await _wait_core(host, port, timeout=600.0):
        print("[all] core 未就绪，退出")
        return 2
    for conv in range(10):
        print(f"\n========== conv {conv}/9 ==========")
        skip_smoke = conv > 0
        rc = await cmd_conv(
            base_url,
            conv,
            timeout,
            cat,
            force_ingest=force_ingest,
            skip_smoke=skip_smoke,
            plan_ids=plan_ids,
        )
        if rc:
            print(f"[all] 停在 conv={conv} rc={rc}")
            _write_run_report(plan_ids)
            return rc
    _write_run_report(plan_ids)
    print(f"[all] 10 个 conv × plans={plan_ids} 完成")
    return 0


async def cmd_campaign(
    base_url: str,
    timeout: float,
    cat: str,
    plan_ids: list[int],
) -> int:
    """conv0 续灌 plan 6–10（不清库）→ smoke/domain/finish；conv 1–9 全量 10 plan。"""
    full_plans = plan_ids if len(plan_ids) == 10 else list(range(1, 11))
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    print(f"[campaign] wait {host}:{port}", flush=True)
    if not await _wait_core(host, port, timeout=600.0):
        print("[campaign] core 未监听，退出")
        return 2
    prog = _progress()
    if 0 not in (prog.get("ingest") or []):
        print("[campaign] conv=0 resume plans=6-10 no-clear", flush=True)
        rc = await cmd_ingest(base_url, 0, timeout, force=False, plan_ids=[6, 7, 8, 9, 10], clear=False)
        if rc:
            return rc
        prog = _progress()
    if 0 not in (prog.get("smoke5") or []):
        rc = await cmd_smoke5(base_url, 0, timeout)
        if rc:
            print("[campaign] smoke5 未通过，停下。")
            _write_10m_report()
            return rc
        prog = _progress()
    if 0 not in (prog.get("domain") or []):
        rc = await cmd_domain(base_url, 0, timeout, cat)
        if rc:
            print("[campaign] domain 未通过，停下。")
            _write_10m_report()
            return rc
        prog = _progress()
    if 0 not in (prog.get("finish") or []):
        rc = await cmd_finish(base_url, 0, timeout)
        if rc:
            _write_10m_report()
            return rc
    for conv in range(1, 10):
        prog = _progress()
        if conv in (prog.get("finish") or []):
            print(f"[campaign] conv={conv} 已完成，跳过", flush=True)
            continue
        print(f"\n========== conv {conv}/9 ==========", flush=True)
        rc = await cmd_conv(
            base_url,
            conv,
            timeout,
            cat,
            force_ingest=False,
            skip_smoke=True,
            plan_ids=full_plans,
        )
        if rc:
            print(f"[campaign] 停在 conv={conv} rc={rc}")
            _write_10m_report()
            return rc
    _write_10m_report()
    print("[campaign] 10m 完成")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BEAM 分阶段评测")
    p.add_argument(
        "stage",
        choices=("ping", "ingest", "smoke5", "domain", "finish", "conv", "all", "campaign"),
    )
    p.add_argument("--conv", type=int, default=0)
    p.add_argument("--cat", default=DEFAULT_DOMAIN)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--force-ingest", action="store_true")
    p.add_argument("--no-clear", dest="clear", action="store_false", help="ingest 不清该 user 记忆（续灌）")
    p.add_argument("--plans", default=DEFAULT_PLANS, help="逗号分隔，1-indexed，如 1 或 1,2,3,4,5,6,7,8,9,10")
    p.add_argument("--out", default="", help="答卷目录，默认 eval/BEAM_official/results/1m")
    p.set_defaults(clear=True)
    return p


async def main_async(args: argparse.Namespace) -> int:
    _configure_out(args.out)
    os.makedirs(OUT_DIR, exist_ok=True)
    plan_ids = _parse_plans(args.plans)
    base = args.base_url.rstrip("/")
    if args.stage == "ping":
        return await cmd_ping(base)
    if args.stage == "ingest":
        return await cmd_ingest(
            base,
            args.conv,
            args.timeout,
            force=args.force_ingest,
            plan_ids=plan_ids,
            clear=args.clear,
        )
    if args.stage == "smoke5":
        return await cmd_smoke5(base, args.conv, args.timeout)
    if args.stage == "domain":
        return await cmd_domain(base, args.conv, args.timeout, args.cat)
    if args.stage == "finish":
        return await cmd_finish(base, args.conv, args.timeout)
    if args.stage == "conv":
        return await cmd_conv(
            base,
            args.conv,
            args.timeout,
            args.cat,
            force_ingest=args.force_ingest,
            skip_smoke=False,
            plan_ids=plan_ids,
        )
    if args.stage == "campaign":
        return await cmd_campaign(base, args.timeout, args.cat, plan_ids)
    return await cmd_all(base, args.timeout, args.cat, force_ingest=args.force_ingest, plan_ids=plan_ids)


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
