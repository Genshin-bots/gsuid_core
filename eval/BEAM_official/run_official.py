"""Official BEAM ladder: 128K (HF 100K) → 500K → 1M → 10M.

Questions are the matching probing_questions on the same conversation, not
the 10M gold set on a plan-1 prefix. Protocol:

  clear → batch_observe(full chat) → rebuild → probe (评测助手 / tools) → rubric judge

Paper 128K is HuggingFace split ``100K`` (20 conv × 20 probes).
"""

from __future__ import annotations

import gc
import os
import sys
import glob
import json
import time
import socket
import asyncio
import argparse
from typing import Literal
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _inherit_core_token() -> None:
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
    if os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip():
        return
    try:
        import psutil
    except ImportError:
        return
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.Error, OSError):
            continue
        if "gsuid_core.core" not in cmd:
            continue
        try:
            env = proc.environ()
        except (psutil.Error, OSError):
            continue
        tok = env.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
        if tok:
            os.environ["GSUID_LOCAL_TEST_TOKEN"] = tok
            return


_inherit_core_token()

import httpx  # noqa: E402

from eval.common import DEFAULT_BASE_URL, load_json  # noqa: E402
from eval.common.beam_runner import (  # noqa: E402
    DEFAULT_TIMEOUT,
    cmd_clear,
    cmd_judge,
    cmd_probe,
    load_beam_row,
    load_beam_plan,
    normalize_plan,
    cmd_ingest_plan,
    parse_time_anchor,
    iter_probing_questions,
    extract_turns_from_plan,
)

try:
    import eval.common.http_client as _http_client

    _http_client._LOCAL_TEST_TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "")
except ImportError:
    pass

Kind = Literal["flat", "plans"]
ScaleKey = Literal["100k", "500k", "1m", "10m"]
SCALE_ORDER: tuple[ScaleKey, ...] = ("100k", "500k", "1m", "10m")
OFF_DATA = os.path.join(_ROOT, "eval", "BEAM_official", "data", "data")


@dataclass(frozen=True)
class ScaleSpec:
    key: ScaleKey
    paper_name: str
    n_conv: int
    kind: Kind
    repo: str
    hf_files: tuple[str, ...]
    parquet_glob: str
    user_prefix: str


SCALES: dict[ScaleKey, ScaleSpec] = {
    "100k": ScaleSpec(
        key="100k",
        paper_name="128K",
        n_conv=20,
        kind="flat",
        repo="Mohammadta/BEAM",
        hf_files=("data/100K-00000-of-00001.parquet",),
        parquet_glob=os.path.join(OFF_DATA, "100K-*.parquet"),
        user_prefix="beam_off_100k",
    ),
    "500k": ScaleSpec(
        key="500k",
        paper_name="500K",
        n_conv=35,
        kind="flat",
        repo="Mohammadta/BEAM",
        hf_files=("data/500K-00000-of-00001.parquet",),
        parquet_glob=os.path.join(OFF_DATA, "500K-*.parquet"),
        user_prefix="beam_off_500k",
    ),
    "1m": ScaleSpec(
        key="1m",
        paper_name="1M",
        n_conv=35,
        kind="flat",
        repo="Mohammadta/BEAM",
        hf_files=("data/1M-00000-of-00001.parquet",),
        parquet_glob=os.path.join(OFF_DATA, "1M-*.parquet"),
        user_prefix="beam_off_1m",
    ),
    "10m": ScaleSpec(
        key="10m",
        paper_name="10M",
        n_conv=10,
        kind="plans",
        repo="Mohammadta/BEAM-10M",
        hf_files=("data/10M-00000-of-00002.parquet", "data/10M-00001-of-00002.parquet"),
        parquet_glob=os.path.join(OFF_DATA, "10M-*.parquet"),
        user_prefix="beam_off_10m",
    ),
}


def _parquet_paths(spec: ScaleSpec) -> list[str]:
    return sorted(p for p in glob.glob(spec.parquet_glob) if os.path.isfile(p))


def _data_glob(spec: ScaleSpec) -> str:
    return spec.parquet_glob


def _spec(key: str) -> ScaleSpec:
    for item in SCALES.values():
        if item.key == key:
            return item
    raise SystemExit(f"unknown scale {key}")


def _out_dir(spec: ScaleSpec) -> str:
    return os.path.join(_ROOT, "eval", "BEAM_official", "results", spec.key)


def _progress_path(spec: ScaleSpec) -> str:
    return os.path.join(_out_dir(spec), "progress.json")


def _answers_path(spec: ScaleSpec, conv: int) -> str:
    return os.path.join(_out_dir(spec), f"answers_{conv}.json")


def _judge_path(spec: ScaleSpec, conv: int) -> str:
    return os.path.join(_out_dir(spec), f"judge_{conv}.json")


def _user_id(spec: ScaleSpec, conv: int) -> str:
    return f"{spec.user_prefix}_{conv}"


def _progress(spec: ScaleSpec) -> dict[str, list[int]]:
    raw = load_json(_progress_path(spec)) if os.path.isfile(_progress_path(spec)) else {}
    out: dict[str, list[int]] = {}
    if not isinstance(raw, dict):
        raw = {}
    for key in ("ingest", "probe", "judge", "finish"):
        ids = raw[key] if key in raw and isinstance(raw[key], list) else []
        out[key] = [int(x) for x in ids if isinstance(x, int)]
    return out


def _save_progress(spec: ScaleSpec, doc: dict[str, list[int]]) -> None:
    os.makedirs(_out_dir(spec), exist_ok=True)
    path = _progress_path(spec)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _mark(spec: ScaleSpec, stage: str, conv: int) -> None:
    doc = _progress(spec)
    ids = doc[stage] if stage in doc else []
    if conv not in ids:
        ids.append(conv)
    doc[stage] = ids
    _save_progress(spec, doc)


def ensure_data(spec: ScaleSpec) -> list[str]:
    paths = _parquet_paths(spec)
    if len(paths) >= len(spec.hf_files):
        return paths
    from huggingface_hub import hf_hub_download

    dest = os.path.join(_ROOT, "eval", "BEAM_official", "data")
    os.makedirs(dest, exist_ok=True)
    print(f"[data] download {spec.repo} {spec.hf_files} -> {dest}", flush=True)
    for fn in spec.hf_files:
        saved = hf_hub_download(repo_id=spec.repo, filename=fn, repo_type="dataset", local_dir=dest)
        print(f"[data] {saved}", flush=True)
    paths = _parquet_paths(spec)
    if not paths:
        raise FileNotFoundError(f"{spec.key} parquet missing after download: {spec.parquet_glob}")
    return paths


def chat_to_plan(chat: object) -> dict[str, object]:
    if not isinstance(chat, list):
        raise SystemExit(f"official chat 不是 list: {type(chat).__name__}")
    plan: dict[str, object] = normalize_plan({"plan_id": 0, "chat": chat})
    return plan


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
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    headers = {"X-Local-Test-Token": token} if token else {}
    url = f"http://{host}:{port}/api/ai/memory/batch_observe"
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        while time.time() < deadline:
            try:
                resp = await client.post(url, headers=headers, json={})
                if resp.status_code != 404:
                    print(f"[wait] eval api ready status={resp.status_code}", flush=True)
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
    return False


async def cmd_ping(base_url: str) -> int:
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    headers = {"X-Local-Test-Token": token} if token else {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(
            f"{base_url}/api/chat_with_history",
            headers=headers,
            json={
                "user_id": "beam_off_ping",
                "message": "ping",
                "history": [],
                "persona_name": "评测助手",
                "enable_observer": False,
                "enable_tools": False,
            },
        )
        print(f"[ping] chat_with_history={resp.status_code} body={resp.text[:240]}", flush=True)
        if resp.status_code != 200:
            return 2
        print("[ping] OK", flush=True)
        return 0


async def cmd_ingest(
    spec: ScaleSpec,
    base_url: str,
    conv: int,
    timeout: float,
    *,
    force: bool,
    extract: bool = False,
) -> int:
    user_id = _user_id(spec, conv)
    prog = _progress(spec)
    if conv in prog["ingest"] and not force:
        print(f"[ingest] {spec.key} conv={conv} 已完成，跳过", flush=True)
        return 0
    parquet = _data_glob(spec)
    cleared = await cmd_clear(base_url, user_id, timeout=timeout)
    st = cleared["status"] if isinstance(cleared, dict) and "status" in cleared else 1
    if st != 0:
        print(f"[ingest] {spec.key} conv={conv} clear 失败", flush=True)
        return 2
    if spec.kind == "flat":
        row = load_beam_row(conv, parquet, columns=["chat"])
        plan = chat_to_plan(row["chat"] if "chat" in row else [])
        del row
        gc.collect()
        r = await cmd_ingest_plan(
            base_url=base_url,
            user_id=user_id,
            plan=plan,
            flush=True,
            trigger_rebuild=True,
            extract=extract,
            timeout=timeout,
        )
        del plan
        gc.collect()
        resp = r["response"] if isinstance(r, dict) and "response" in r else {}
        if not isinstance(resp, dict) or ("status" in resp and resp["status"] != 0) or "status" not in resp:
            print(f"[ingest] {spec.key} conv={conv} 失败 {resp!r}", flush=True)
            return 2
    else:
        for i, pid in enumerate(range(1, 11)):
            plan = load_beam_plan(conv, pid, parquet)
            r = await cmd_ingest_plan(
                base_url=base_url,
                user_id=user_id,
                plan=plan,
                flush=True,
                trigger_rebuild=(i == 9),
                extract=extract,
                timeout=timeout,
            )
            del plan
            gc.collect()
            resp = r["response"] if isinstance(r, dict) and "response" in r else {}
            if not isinstance(resp, dict) or ("status" in resp and resp["status"] != 0) or "status" not in resp:
                print(f"[ingest] {spec.key} conv={conv} plan={pid} 失败 {resp!r}", flush=True)
                return 2
    _mark(spec, "ingest", conv)
    print(f"[ingest] {spec.key} conv={conv} done", flush=True)
    return 0


def _answers_sane(path: str, n_expect: int) -> bool:
    recs = load_json(path)
    if not isinstance(recs, list) or len(recs) < n_expect:
        n = 0 if not isinstance(recs, list) else len(recs)
        print(f"[sanity] 答卷条数 {n} < {n_expect}", flush=True)
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
            qid = a["question_id"] if "question_id" in a else "?"
            print(f"[sanity] FAIL {qid} status={status} {text[:120]!r}", flush=True)
    print(f"[sanity] bad={bad}/{n_expect}", flush=True)
    return bad == 0


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
    print(f"[judge] {passed}/{total}  {path}", flush=True)
    for c in sorted(by):
        print(f"  {c:30s} {by[c][0]}/{by[c][1]}", flush=True)
    return passed, total


def write_scale_report(spec: ScaleSpec) -> tuple[int, int]:
    lines: list[str] = [
        f"# Official BEAM {spec.paper_name} ({spec.key})",
        "",
        "题目：Mohammadta/BEAM 同 split 对话 + 配套 probing_questions（每 conv 20 题）。",
        "口径：生产 Chat（评测助手 + enable_tools + memory_eval=False + clock_at）。",
        f"user_id=`{spec.user_prefix}_<conv>`。输出 `{_out_dir(spec)}`。",
        "",
    ]
    passed_all = 0
    total_all = 0
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    eo_cov: list[float] = []
    eo_tau: list[float] = []
    eo_tau_off: list[float] = []
    conv_rows: list[str] = []
    for conv in range(spec.n_conv):
        jp = _judge_path(spec, conv)
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
            if cat == "event_ordering":
                if "coverage" in j and isinstance(j["coverage"], (int, float)):
                    eo_cov.append(float(j["coverage"]))
                if "tau" in j and isinstance(j["tau"], (int, float)):
                    eo_tau.append(float(j["tau"]))
                tau_off = j["tau_official"] if "tau_official" in j else None
                if not isinstance(tau_off, (int, float)) and "align" in j and isinstance(j["align"], list):
                    from eval.common.judge import parse_align_list, official_tau_from_align

                    tau_off = official_tau_from_align(parse_align_list(j["align"], len(j["align"])))
                if isinstance(tau_off, (int, float)):
                    eo_tau_off.append(float(tau_off))
        passed_all += ok
        total_all += n
        conv_rows.append(f"| {conv} | {ok}/{n} | |")
    pct = f"{100.0 * passed_all / total_all:.1f}%" if total_all else "n/a"
    lines.append(f"**总分：{passed_all}/{total_all}（{pct}）**。应有 {spec.n_conv * 20} 题。")
    if "event_ordering" in by_cat:
        eo_pass, eo_n = by_cat["event_ordering"]
        lines.append(f"**EO 严格 pass：**{eo_pass}/{eo_n}（全对齐且 τ=1 且 rubric 全中）")
    if eo_cov:
        cov_pct = 100.0 * sum(eo_cov) / len(eo_cov)
        lines.append(f"**EO coverage：**{cov_pct:.1f}%（{len(eo_cov)} 题对齐率）")
    if eo_tau_off:
        lines.append(
            f"**EO Kendall τ-b（官方口径）：**{sum(eo_tau_off) / len(eo_tau_off):.3f}"
            f"（{len(eo_tau_off)} 题；未全对齐记 0）"
        )
    if eo_tau:
        tau_mean = sum(eo_tau) / len(eo_tau)
        lines.append(f"**EO Kendall τ-b（全对齐）：**{tau_mean:.3f}（{len(eo_tau)} 题）")
    elif eo_cov:
        lines.append("**EO Kendall τ-b（全对齐）：**—（尚无全部对齐的题）")
    gold_p = os.path.join(_out_dir(spec), f"eo_gold_turns_{spec.key}.json")
    l2_hits: list[float] = []
    if os.path.isfile(gold_p):
        gdoc = load_json(gold_p)
        if isinstance(gdoc, dict):
            for conv in range(spec.n_conv):
                ap = _answers_path(spec, conv)
                if not os.path.isfile(ap):
                    continue
                recs = load_json(ap)
                if not isinstance(recs, list):
                    continue
                for rec in recs:
                    if not isinstance(rec, dict) or rec.get("category") != "event_ordering":
                        continue
                    qid = str(rec["question_id"]) if "question_id" in rec else ""
                    raw_g = gdoc[qid] if qid in gdoc else None
                    eids: list[str] = []
                    if isinstance(raw_g, dict) and "episode_ids" in raw_g:
                        eids = [str(x) for x in raw_g["episode_ids"]]
                    elif isinstance(raw_g, list):
                        eids = [str(x) for x in raw_g]
                    if not eids:
                        continue
                    inj = rec["inject_ids"] if "inject_ids" in rec and isinstance(rec["inject_ids"], list) else []
                    have = {str(x) for x in inj}
                    l2_hits.append(sum(1 for g in eids if g in have) / len(eids))
    if l2_hits:
        lines.append(f"**EO L2（已映射集合）：**{100.0 * sum(l2_hits) / len(l2_hits):.1f}%（{len(l2_hits)} 题）")
    lines.extend(["", "## 分 conversation", "", "| conv | 分数 | 备注 |", "|------|------|------|"])
    lines.extend(conv_rows)
    lines.extend(["", "## 按类", "", "| 类别 | 过线 |", "|------|------|"])
    for c in sorted(by_cat):
        extra = ""
        if c == "event_ordering" and eo_cov:
            extra = f" · coverage {100.0 * sum(eo_cov) / len(eo_cov):.1f}%"
            if eo_tau_off:
                extra += f" · τ官 {sum(eo_tau_off) / len(eo_tau_off):.3f}"
            if eo_tau:
                extra += f" · τ齐 {sum(eo_tau) / len(eo_tau):.3f}"
        lines.append(f"| {c} | {by_cat[c][0]}/{by_cat[c][1]}{extra} |")
    path = os.path.join(_out_dir(spec), "report.md")
    os.makedirs(_out_dir(spec), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[report] {spec.paper_name} {passed_all}/{total_all} ({pct}) -> {path}", flush=True)
    return passed_all, total_all


def write_ladder_report() -> None:
    lines = [
        "# Official BEAM ladder",
        "",
        "顺序：128K (HF 100K) → 500K → 1M → 10M。每档用配套对话和探针，不是 10M 金标套小规模前缀。",
        "",
        "| 规模 | 论文名 | conv × 题 | 分数 |",
        "|------|--------|-----------|------|",
    ]
    for key in SCALE_ORDER:
        spec = SCALES[key]
        report = os.path.join(_out_dir(spec), "report.md")
        score = "—"
        if os.path.isfile(report):
            text = Path(report).read_text(encoding="utf-8")
            for ln in text.splitlines():
                if ln.startswith("**总分："):
                    score = ln.strip("*。")
                    break
        lines.append(f"| {spec.key} | {spec.paper_name} | {spec.n_conv} × 20 | {score} |")
    dest = os.path.join(_ROOT, "eval", "BEAM_official", "results", "ladder_report.md")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[report] ladder -> {dest}", flush=True)


def _fallback_clock_from_chat(chat: object) -> str | None:
    """探针缺 time_anchor 时，用对话最后一条有时间戳的 turn 当 clock_at。"""
    last: str | None = None
    for t in extract_turns_from_plan(chat_to_plan(chat)):
        iso = parse_time_anchor(str(t["time_anchor"] if "time_anchor" in t else ""))
        if iso:
            last = iso
    return last


async def cmd_probe_conv(
    spec: ScaleSpec,
    base_url: str,
    conv: int,
    timeout: float,
    *,
    concurrency: int = 7,
) -> int:
    row = load_beam_row(conv, _data_glob(spec), columns=["probing_questions", "chat"])
    probes = iter_probing_questions(row)
    if len(probes) != 20:
        print(f"[probe] {spec.key} conv={conv} 题数={len(probes)} 期望 20", flush=True)
        if len(probes) == 0:
            return 2
    answers = _answers_path(spec, conv)
    fallback = _fallback_clock_from_chat(row["chat"] if "chat" in row else [])
    print(f"[probe] {spec.key} conv={conv} fallback_clock={fallback}", flush=True)
    await cmd_probe(
        base_url=base_url,
        user_id=_user_id(spec, conv),
        probes=probes,
        answers_file=answers,
        timeout=timeout,
        resume=True,
        fallback_clock=fallback,
        concurrency=concurrency,
    )
    _mark(spec, "probe", conv)
    if not _answers_sane(answers, len(probes)):
        return 2
    return 0


async def cmd_judge_conv(
    spec: ScaleSpec,
    base_url: str,
    conv: int,
    timeout: float,
    *,
    concurrency: int = 7,
) -> int:
    answers = _answers_path(spec, conv)
    if not os.path.isfile(answers):
        print(f"[judge] 缺答卷 {answers}", flush=True)
        return 2
    await cmd_judge(
        base_url=base_url,
        answers_file=answers,
        judge_file=_judge_path(spec, conv),
        timeout=timeout,
        resume=True,
        concurrency=concurrency,
    )
    _summarize_judge(_judge_path(spec, conv))
    _mark(spec, "judge", conv)
    return 0


async def cmd_conv(
    spec: ScaleSpec,
    base_url: str,
    conv: int,
    timeout: float,
    *,
    force_ingest: bool,
    extract: bool = False,
    concurrency: int = 7,
) -> int:
    prog = _progress(spec)
    if conv in prog["finish"] and not force_ingest:
        print(f"[conv] {spec.key} conv={conv} 已完成，跳过", flush=True)
        return 0
    rc = await cmd_ingest(spec, base_url, conv, timeout, force=force_ingest, extract=extract)
    if rc:
        return rc
    rc = await cmd_probe_conv(spec, base_url, conv, timeout, concurrency=concurrency)
    if rc:
        return rc
    rc = await cmd_judge_conv(spec, base_url, conv, timeout, concurrency=concurrency)
    if rc:
        return rc
    _mark(spec, "finish", conv)
    write_scale_report(spec)
    return 0


def _unmark_probe(spec: ScaleSpec, conv: int) -> None:
    doc = _progress(spec)
    for stage in ("probe", "judge", "finish"):
        doc[stage] = [c for c in doc[stage] if c != conv]
    _save_progress(spec, doc)
    for path in (_answers_path(spec, conv), _judge_path(spec, conv)):
        if os.path.isfile(path):
            os.remove(path)


def _strip_category_records(spec: ScaleSpec, conv: int, category: str) -> int:
    """只删指定类别与 ERROR 行，保留其它题，避免 EO-only 冲掉整份答卷。"""
    from eval.common.io import dump_json, load_json

    removed = 0
    for path in (_answers_path(spec, conv), _judge_path(spec, conv)):
        if not os.path.isfile(path):
            continue
        recs = load_json(path)
        if not isinstance(recs, list):
            continue
        kept: list[object] = []
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            cat = str(rec["category"]) if "category" in rec else ""
            text = str(rec["agent_answer"]) if "agent_answer" in rec else ""
            status = rec["status_code"] if "status_code" in rec else 200
            if cat == category or text.startswith("[ERROR]") or status not in (200, 0):
                removed += 1
                continue
            kept.append(rec)
        dump_json(path, kept)
    doc = _progress(spec)
    for stage in ("probe", "judge", "finish"):
        doc[stage] = [c for c in doc[stage] if c != conv]
    _save_progress(spec, doc)
    return removed


async def cmd_reprobe(
    spec: ScaleSpec,
    base_url: str,
    timeout: float,
    *,
    concurrency: int = 7,
) -> int:
    """已摄入的 conv 只重测，不 clear、不重灌。"""
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    print(f"[reprobe] {spec.paper_name} wait {host}:{port}", flush=True)
    if not await _wait_core(host, port, timeout=600.0):
        print("[reprobe] core 未就绪，退出", flush=True)
        return 2
    if await cmd_ping(base_url):
        print("[reprobe] ping 失败，退出", flush=True)
        return 2
    ingested = set(_progress(spec)["ingest"])
    if not ingested:
        print(f"[reprobe] {spec.key} 无已摄入 conv", flush=True)
        return 2
    for conv in range(spec.n_conv):
        if conv not in ingested:
            print(f"[reprobe] {spec.key} conv={conv} 未摄入，跳过", flush=True)
            continue
        print(f"\n========== reprobe {spec.paper_name} conv {conv}/{spec.n_conv - 1} ==========", flush=True)
        _unmark_probe(spec, conv)
        rc = await cmd_probe_conv(spec, base_url, conv, timeout, concurrency=concurrency)
        if rc:
            print(f"[reprobe] {spec.key} 停在 conv={conv} rc={rc}", flush=True)
            write_scale_report(spec)
            write_ladder_report()
            return rc
        rc = await cmd_judge_conv(spec, base_url, conv, timeout, concurrency=concurrency)
        if rc:
            print(f"[reprobe] {spec.key} judge 停在 conv={conv} rc={rc}", flush=True)
            write_scale_report(spec)
            write_ladder_report()
            return rc
        _mark(spec, "finish", conv)
        write_scale_report(spec)
    write_ladder_report()
    print(f"[reprobe] {spec.paper_name} 完成", flush=True)
    return 0


async def cmd_all(
    spec: ScaleSpec,
    base_url: str,
    timeout: float,
    *,
    force_ingest: bool,
    extract: bool = False,
    concurrency: int = 7,
) -> int:
    ensure_data(spec)
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    print(f"[all] {spec.paper_name} wait {host}:{port}", flush=True)
    if not await _wait_core(host, port, timeout=600.0):
        print("[all] core 未就绪，退出", flush=True)
        return 2
    if await cmd_ping(base_url):
        print("[all] ping 失败，退出", flush=True)
        return 2
    for conv in range(spec.n_conv):
        print(f"\n========== {spec.paper_name} conv {conv}/{spec.n_conv - 1} ==========", flush=True)
        rc = await cmd_conv(
            spec,
            base_url,
            conv,
            timeout,
            force_ingest=force_ingest,
            extract=extract,
            concurrency=concurrency,
        )
        if rc:
            print(f"[all] {spec.key} 停在 conv={conv} rc={rc}", flush=True)
            write_scale_report(spec)
            write_ladder_report()
            return rc
    write_scale_report(spec)
    write_ladder_report()
    print(f"[all] {spec.paper_name} {spec.n_conv} conv × 20 完成", flush=True)
    return 0


async def cmd_ladder(base_url: str, timeout: float, *, force_ingest: bool, concurrency: int = 7) -> int:
    for key in SCALE_ORDER:
        spec = SCALES[key]
        print(f"\n########## LADDER {spec.paper_name} ##########", flush=True)
        rc = await cmd_all(spec, base_url, timeout, force_ingest=force_ingest, concurrency=concurrency)
        if rc:
            print(f"[ladder] 停在 {spec.paper_name}", flush=True)
            return rc
    print("[ladder] 128K → 500K → 1M → 10M 完成", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Official BEAM 128K/500K/1M/10M")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--force-ingest", action="store_true")
    p.add_argument(
        "--concurrency",
        type=int,
        default=11,
        help="probe/judge 并发（1~12）。默认 11，可提到 12 打满 MiniMax-M3",
    )
    p.add_argument(
        "--extract",
        action="store_true",
        help="摄入时开启 §14 窗口化实体/边抽取（重灌图谱用；默认关，与原协议一致）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download")
    sub.add_parser("ping")
    p_all = sub.add_parser("all")
    p_all.add_argument("--scale", required=True, choices=SCALE_ORDER)
    p_conv = sub.add_parser("conv")
    p_conv.add_argument("--scale", required=True, choices=SCALE_ORDER)
    p_conv.add_argument("--conv", type=int, required=True)
    sub.add_parser("ladder")
    p_reprobe = sub.add_parser("reprobe")
    p_reprobe.add_argument("--scale", required=True, choices=SCALE_ORDER)
    p_rep = sub.add_parser("report")
    p_rep.add_argument("--scale", default="", choices=("", *SCALE_ORDER))
    return p


async def main_async(args: argparse.Namespace) -> int:
    base_url = str(args.base_url).rstrip("/")
    timeout = float(args.timeout)
    force = bool(args.force_ingest)
    extract = bool(args.extract)
    concurrency = max(1, min(12, int(args.concurrency)))
    if args.cmd == "download":
        for key in SCALE_ORDER:
            ensure_data(SCALES[key])
        return 0
    if args.cmd == "ping":
        return await cmd_ping(base_url)
    if args.cmd == "report":
        scale = str(args.scale)
        if scale:
            write_scale_report(_spec(scale))
        write_ladder_report()
        return 0
    if args.cmd == "all":
        return await cmd_all(
            _spec(str(args.scale)),
            base_url,
            timeout,
            force_ingest=force,
            extract=extract,
            concurrency=concurrency,
        )
    if args.cmd == "conv":
        spec = _spec(str(args.scale))
        ensure_data(spec)
        return await cmd_conv(
            spec,
            base_url,
            int(args.conv),
            timeout,
            force_ingest=force,
            extract=extract,
            concurrency=concurrency,
        )
    if args.cmd == "ladder":
        return await cmd_ladder(base_url, timeout, force_ingest=force, concurrency=concurrency)
    if args.cmd == "reprobe":
        spec = _spec(str(args.scale))
        ensure_data(spec)
        return await cmd_reprobe(spec, base_url, timeout, concurrency=concurrency)
    print(f"unknown cmd {args.cmd}", flush=True)
    return 1


def main() -> int:
    args = build_parser().parse_args()
    t0 = time.time()
    rc = asyncio.run(main_async(args))
    print(f"\n[Done] elapsed={time.time() - t0:.1f}s rc={rc}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
