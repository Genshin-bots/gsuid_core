"""EO 专项：extract-light / 分段指标 / 只重测 event_ordering。"""

from __future__ import annotations

import os
import sys
import asyncio
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx  # noqa: E402

from eval.common import DEFAULT_BASE_URL  # noqa: E402
from eval.common.beam_runner import (  # noqa: E402
    DEFAULT_TIMEOUT,
    cmd_judge,
    cmd_probe,
    load_beam_row,
    call_chat_with_history,
    iter_probing_questions,
    extract_standard_answer,
    extract_turns_from_plan,
)
from eval.manual.eo_stage_metrics import run_metrics  # noqa: E402
from eval.BEAM_official.run_official import (  # noqa: E402
    SCALES,
    _mark,
    _spec,
    _user_id,
    cmd_ping,
    _progress,
    _data_glob,
    _wait_core,
    ensure_data,
    chat_to_plan,
    _answers_path,
    _answers_sane,
    cmd_judge_conv,
    write_scale_report,
    _inherit_core_token,
    write_ladder_report,
    _strip_category_records,
    _fallback_clock_from_chat,
)

_inherit_core_token()


def _headers() -> dict[str, str]:
    tok = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    return {"X-Local-Test-Token": tok} if tok else {}


async def cmd_extract_light(scale: str, base_url: str, timeout: float) -> int:
    spec = _spec(scale)
    ensure_data(spec)
    ingested = set(_progress(spec)["ingest"])
    if not ingested:
        print("[extract-light] 无已摄入 conv", flush=True)
        return 2
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        for conv in range(spec.n_conv):
            if conv not in ingested:
                continue
            user_id = _user_id(spec, conv)
            print(f"[extract-light] conv={conv} {user_id}", flush=True)
            resp = await client.post(
                f"{base_url}/api/ai/memory/eval/extract_aspects",
                headers=_headers(),
                json={"user_id": user_id, "limit": 16},
            )
            print(f"  status={resp.status_code} body={resp.text[:240]}", flush=True)
            if resp.status_code != 200:
                return 2
    print("[extract-light] done", flush=True)
    return 0


async def cmd_reprobe_only(
    scale: str,
    base_url: str,
    timeout: float,
    only: str,
    fresh: bool = False,
) -> int:
    spec = _spec(scale)
    ensure_data(spec)
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    if not await _wait_core(host, port, timeout=600.0):
        print("[reprobe-eo] core 未就绪", flush=True)
        return 2
    if await cmd_ping(base_url):
        print("[reprobe-eo] ping 失败", flush=True)
        return 2
    ingested = set(_progress(spec)["ingest"])
    if fresh:
        for conv in range(spec.n_conv):
            if conv in ingested:
                n = _strip_category_records(spec, conv, only)
                print(f"[reprobe-eo] fresh conv={conv} stripped={n}", flush=True)
    for conv in range(spec.n_conv):
        if conv not in ingested:
            continue
        if conv in _progress(spec)["finish"]:
            print(f"[reprobe-eo] conv={conv} finish, skip", flush=True)
            continue
        print(f"\n========== EO reprobe conv {conv} ==========", flush=True)
        if not fresh:
            stripped = _strip_category_records(spec, conv, only)
            print(f"[reprobe-eo] stripped={stripped}", flush=True)
        row = load_beam_row(conv, _data_glob(spec), columns=["probing_questions", "chat"])
        probes = [p for p in iter_probing_questions(row) if p[0] == only]
        if not probes:
            print(f"[reprobe-eo] conv={conv} 无 {only}", flush=True)
            continue
        answers = _answers_path(spec, conv)
        fallback = _fallback_clock_from_chat(row["chat"] if "chat" in row else [])
        await cmd_probe(
            base_url=base_url,
            user_id=_user_id(spec, conv),
            probes=probes,
            answers_file=answers,
            timeout=timeout,
            resume=True,
            fallback_clock=fallback,
        )
        if not _answers_sane(answers, len(probes)):
            write_scale_report(spec)
            return 2
        _mark(spec, "probe", conv)
        rc = await cmd_judge_conv(spec, base_url, conv, timeout)
        if rc:
            write_scale_report(spec)
            return rc
        _mark(spec, "finish", conv)
        write_scale_report(spec)
    write_ladder_report()
    print("[reprobe-eo] 完成", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Official BEAM EO extras")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--scale", default="100k", choices=tuple(SCALES))
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("extract-light", "metrics"):
        sub.add_parser(name)
    p_re = sub.add_parser("reprobe")
    p_re.add_argument("--only", default="event_ordering")
    p_re.add_argument("--fresh", action="store_true")
    p_re.add_argument("--oracle-inject", action="store_true")
    p_re.add_argument("--full-context", action="store_true")
    p_gist = sub.add_parser("gist-backfill")
    p_gist.add_argument("--source", default="rule", choices=("rule", "llm"))
    return p


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """允许 ``run_eo.py gist-backfill --scale 100k``：把后置 --scale 挪到子命令前。"""
    raw = list(sys.argv[1:] if argv is None else argv)
    cmds = {"extract-light", "metrics", "reprobe", "gist-backfill"}
    if raw and raw[0] in cmds and "--scale" in raw:
        i = raw.index("--scale")
        if i + 1 < len(raw):
            scale = raw[i : i + 2]
            del raw[i : i + 2]
            raw = scale + raw
    return build_parser().parse_args(raw)


async def cmd_gist_backfill(scale: str, base_url: str, timeout: float, source: str) -> int:
    spec = _spec(scale)
    ensure_data(spec)
    ingested = set(_progress(spec)["ingest"])
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        for conv in range(spec.n_conv):
            if conv not in ingested:
                continue
            user_id = _user_id(spec, conv)
            print(f"[gist-backfill] conv={conv} source={source}", flush=True)
            resp = await client.post(
                f"{base_url}/api/ai/memory/eval/gist_backfill",
                headers=_headers(),
                json={"user_id": user_id, "source": source, "limit": 4000},
            )
            print(f"  status={resp.status_code} body={resp.text[:240]}", flush=True)
            if resp.status_code != 200:
                return 2
    print("[gist-backfill] done", flush=True)
    return 0


async def cmd_ceiling(
    scale: str,
    base_url: str,
    timeout: float,
    *,
    oracle_inject: bool,
    full_context: bool,
) -> int:
    """U_oracle / U_full：gold 原文或整段 chat 当上下文，不走记忆选 N。"""
    from eval.common.io import dump_json
    from eval.BEAM_official.oracle import map_gold_episode_ids, probe_source_chat_ids
    from eval.manual.eo_stage_metrics import _episodes

    spec = _spec(scale)
    ensure_data(spec)
    tag = "uorc" if oracle_inject else "ufull"
    dest_dir = os.path.dirname(_answers_path(spec, 0))
    answers_file = os.path.join(dest_dir, f"eo_{tag}_answers.json")
    judge_file = os.path.join(dest_dir, f"eo_{tag}_judge.json")
    records: list[dict[str, object]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        for conv in range(spec.n_conv):
            if conv not in set(_progress(spec)["ingest"]):
                continue
            user_id = _user_id(spec, conv)
            row = load_beam_row(conv, _data_glob(spec), columns=["probing_questions", "chat"])
            probes = [p for p in iter_probing_questions(row) if p[0] == "event_ordering"]
            eps = await _episodes(client, base_url, user_id) if oracle_inject else []
            chat = row["chat"] if "chat" in row else []
            turns = extract_turns_from_plan(chat_to_plan(chat))
            full_blob = ""
            if full_context and isinstance(chat, list):
                bits_h: list[str] = []
                for turn in chat:
                    if not isinstance(turn, dict):
                        continue
                    role = str(turn["role"]) if "role" in turn else ""
                    content = str(turn["content"]) if "content" in turn else ""
                    if role in ("user", "assistant") and content:
                        bits_h.append(f"{role}: {content}")
                full_blob = "\n".join(bits_h)
            for category, idx, probe in probes:
                question = str(probe["question"]) if "question" in probe else ""
                std = extract_standard_answer(probe, "event_ordering")
                rubric = probe["rubric"] if "rubric" in probe and isinstance(probe["rubric"], list) else []
                rub_s = [str(x) for x in rubric]
                msg = question
                if oracle_inject:
                    src = probe_source_chat_ids(
                        probe if isinstance(probe, dict) else {},
                        dest_dir=dest_dir,
                        conv=conv,
                        q=idx,
                        scale=spec.key,
                    )
                    mapped, _unmap = map_gold_episode_ids(std, rub_s, turns, eps, source_ids=src)
                    by_id = {r["id"]: r["content"] for r in eps if "id" in r}
                    bits = [by_id[eid][:400] for eid in mapped if eid in by_id]
                    if bits:
                        msg = "Relevant user turns in time order:\n- " + "\n- ".join(bits) + "\n\n" + question
                elif full_context and full_blob:
                    msg = "Full conversation (do not use memory):\n" + full_blob + "\n\nQuestion:\n" + question
                print(f"[ceiling] conv={conv} {category}#{idx} oracle={oracle_inject} uid={user_id}", flush=True)
                resp = await call_chat_with_history(
                    client=client,
                    base_url=base_url,
                    user_id=user_id,
                    message=msg,
                    history=[],
                    persona_name="评测助手",
                    timeout=timeout,
                    enable_observer=False,
                    enable_system2=False,
                    enable_tools=False,
                    max_history=0,
                    skip_memory=True,
                )
                text = str(resp["data"]) if "data" in resp else ""
                st = int(resp["status_code"]) if "status_code" in resp and isinstance(resp["status_code"], int) else -1
                records.append(
                    {
                        "question_id": f"{user_id}_{tag}__{category}__{idx}",
                        "category": category,
                        "question": question,
                        "standard_answer": std,
                        "agent_answer": text,
                        "rubric": rub_s,
                        "status_code": st,
                        "user_id": user_id,
                    }
                )
                dump_json(answers_file, records)
    await cmd_judge(base_url=base_url, answers_file=answers_file, judge_file=judge_file, timeout=timeout, resume=False)
    from eval.common.io import load_json

    judged = load_json(judge_file)
    cov: list[float] = []
    tau_off: list[float] = []
    passed = 0
    total = 0
    if isinstance(judged, list):
        for rec in judged:
            if not isinstance(rec, dict):
                continue
            j = rec["judge"] if "judge" in rec else {}
            if not isinstance(j, dict):
                continue
            total += 1
            if bool(j["passed"]) if "passed" in j else False:
                passed += 1
            if "coverage" in j and isinstance(j["coverage"], (int, float)):
                cov.append(float(j["coverage"]))
            off = j["tau_official"] if "tau_official" in j else 0.0
            if isinstance(off, (int, float)):
                tau_off.append(float(off))
    dest = os.path.join(dest_dir, "eo_ceiling.md")
    prev = ""
    if os.path.isfile(dest):
        with open(dest, encoding="utf-8") as f:
            prev = f.read().rstrip() + "\n\n"
    cov_s = f"{100.0 * sum(cov) / len(cov):.1f}%" if cov else "n/a"
    tau_s = f"{sum(tau_off) / len(tau_off):.3f}" if tau_off else "n/a"
    block = [
        f"## {tag}",
        f"- pass {passed}/{total}",
        f"- coverage {cov_s}",
        f"- tau_official {tau_s}",
        f"- answers `{answers_file}`",
        "",
    ]
    with open(dest, "w", encoding="utf-8") as f:
        f.write((prev if prev.startswith("# EO ceiling") else "# EO ceiling\n\n" + prev) + "\n".join(block) + "\n")
    print(f"[ceiling] {tag} {passed}/{total} cov={cov_s} tau={tau_s} -> {dest}", flush=True)
    return 0


async def main_async(args: argparse.Namespace) -> int:
    base = str(args.base_url).rstrip("/")
    timeout = float(args.timeout)
    scale = str(args.scale)
    if args.cmd == "extract-light":
        return await cmd_extract_light(scale, base, timeout)
    if args.cmd == "metrics":
        return await run_metrics(scale, base, min(timeout, 180.0))
    if args.cmd == "gist-backfill":
        return await cmd_gist_backfill(scale, base, timeout, str(args.source))
    if args.cmd == "reprobe":
        if bool(args.oracle_inject) or bool(args.full_context):
            return await cmd_ceiling(
                scale,
                base,
                timeout,
                oracle_inject=bool(args.oracle_inject),
                full_context=bool(args.full_context),
            )
        return await cmd_reprobe_only(
            scale,
            base,
            timeout,
            str(args.only),
            fresh=bool(args.fresh),
        )
    return 1


def main() -> int:
    args = _parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
