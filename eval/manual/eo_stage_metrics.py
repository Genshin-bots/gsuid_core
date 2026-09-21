"""A2：打运行中的 core，报 EO 的 recall@pool / inject / skeleton。"""

from __future__ import annotations

import os
import sys
import json
import argparse
from typing import TypedDict

import httpx

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from eval.common import DEFAULT_BASE_URL  # noqa: E402
from eval.common.beam_runner import (  # noqa: E402
    load_beam_row,
    iter_probing_questions,
    extract_standard_answer,
    extract_turns_from_plan,
)
from eval.BEAM_official.oracle import (  # noqa: E402
    gold_phrases,
    map_gold_episode_ids,
    probe_source_chat_ids,
)
from eval.BEAM_official.run_official import (  # noqa: E402
    SCALES,
    _spec,
    _out_dir,
    _user_id,
    _data_glob,
    ensure_data,
    chat_to_plan,
    _inherit_core_token,
)

_inherit_core_token()


class StageRow(TypedDict):
    question_id: str
    conv: int
    n_gold: int
    n_mapped: int
    n_unmap: int
    recall_pool: float
    recall_inject: float
    recall_skeleton: float
    pool_size: int
    inject_chars: int
    mapped: bool


def _headers() -> dict[str, str]:
    tok = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    return {"X-Local-Test-Token": tok} if tok else {}


def _hit_rate(gold_ids: list[str], have: list[str]) -> float:
    if not gold_ids:
        return 0.0
    s = set(have)
    return sum(1 for g in gold_ids if g and g in s) / len(gold_ids)


async def _episodes(client: httpx.AsyncClient, base: str, user_id: str) -> list[dict[str, str]]:
    resp = await client.post(
        f"{base}/api/ai/memory/eval/episodes",
        headers=_headers(),
        json={"user_id": user_id, "limit": 800},
    )
    resp.raise_for_status()
    body = resp.json()
    rows = body["rows"] if "rows" in body and isinstance(body["rows"], list) else []
    out: list[dict[str, str]] = []
    for r in rows:
        if not isinstance(r, dict) or "id" not in r:
            continue
        out.append(
            {
                "id": str(r["id"]),
                "content": str(r["content"]) if "content" in r else "",
                "valid_at": str(r["valid_at"]) if "valid_at" in r else "",
            }
        )
    return out


async def _retrieve(
    client: httpx.AsyncClient, base: str, user_id: str, query: str
) -> tuple[list[str], list[str], list[str], int]:
    resp = await client.post(
        f"{base}/api/ai/memory/eval/retrieve",
        headers=_headers(),
        json={"user_id": user_id, "query": query, "enable_system2": True},
    )
    resp.raise_for_status()
    body = resp.json()
    pool = [str(x) for x in body["pool_ids"]] if "pool_ids" in body and isinstance(body["pool_ids"], list) else []
    inj = [str(x) for x in body["inject_ids"]] if "inject_ids" in body and isinstance(body["inject_ids"], list) else []
    raw_skel = body["skeleton_ids"] if "skeleton_ids" in body else []
    skel = [str(x) for x in raw_skel] if isinstance(raw_skel, list) else []
    chars = int(body["inject_chars"]) if "inject_chars" in body and isinstance(body["inject_chars"], int) else 0
    return pool, inj, skel, chars


async def run_metrics(scale: str, base_url: str, timeout: float) -> int:
    spec = _spec(scale)
    ensure_data(spec)
    parquet = _data_glob(spec)
    dest_dir = _out_dir(spec)
    os.makedirs(dest_dir, exist_ok=True)
    gold_map: dict[str, dict[str, list[str]]] = {}
    rows_out: list[StageRow] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        cache: dict[int, list[dict[str, str]]] = {}
        for conv in range(spec.n_conv):
            user_id = _user_id(spec, conv)
            row = load_beam_row(conv, parquet, columns=["probing_questions", "chat"])
            probes = iter_probing_questions(row)
            if conv not in cache:
                cache[conv] = await _episodes(client, base_url, user_id)
                print(f"[episodes] conv={conv} user_turns={len(cache[conv])}", flush=True)
            for category, idx, probe in probes:
                if category != "event_ordering":
                    continue
                qid = f"{user_id}__{category}__{idx}"
                question = str(probe["question"]) if "question" in probe else ""
                std = extract_standard_answer(probe, category)
                rubric = probe["rubric"] if "rubric" in probe and isinstance(probe["rubric"], list) else []
                chat = row["chat"] if "chat" in row else []
                turns = extract_turns_from_plan(chat_to_plan(chat))
                src = probe_source_chat_ids(
                    probe if isinstance(probe, dict) else {},
                    dest_dir=dest_dir,
                    conv=conv,
                    q=idx,
                    scale=spec.key,
                )
                mapped, unmap = map_gold_episode_ids(std, [str(x) for x in rubric], turns, cache[conv], source_ids=src)
                phrases = gold_phrases(std, [str(x) for x in rubric])
                gold_map[qid] = {"episode_ids": list(mapped), "unmap": list(unmap), "chat_ids": list(src)}
                pool, inj, skel, chars = await _retrieve(client, base_url, user_id, question)
                rec: StageRow = {
                    "question_id": qid,
                    "conv": conv,
                    "n_gold": len(src) if src else (len(phrases) if phrases else len(rubric)),
                    "n_mapped": len(mapped),
                    "n_unmap": len(unmap),
                    "recall_pool": _hit_rate(mapped, pool),
                    "recall_inject": _hit_rate(mapped, inj),
                    "recall_skeleton": _hit_rate(mapped, skel),
                    "pool_size": len(pool),
                    "inject_chars": chars,
                    "mapped": bool(mapped) and not unmap,
                }
                rows_out.append(rec)
                print(
                    f"[eo] {qid} gold={rec['n_mapped']}/{rec['n_gold']} "
                    f"pool={rec['recall_pool']:.2f} inj={rec['recall_inject']:.2f} "
                    f"skel={rec['recall_skeleton']:.2f} n_pool={rec['pool_size']}",
                    flush=True,
                )
    gold_path = os.path.join(dest_dir, f"eo_gold_turns_{spec.key}.json")
    with open(gold_path, "w", encoding="utf-8") as f:
        json.dump(gold_map, f, ensure_ascii=False, indent=2)
    n = len(rows_out)
    mapped_rows = [r for r in rows_out if r["n_mapped"] > 0]
    mn = len(mapped_rows)
    pool_m = sum(r["recall_pool"] for r in mapped_rows) / mn if mn else 0.0
    inj_m = sum(r["recall_inject"] for r in mapped_rows) / mn if mn else 0.0
    skel_m = sum(r["recall_skeleton"] for r in mapped_rows) / mn if mn else 0.0
    full_map = sum(1 for r in rows_out if r["mapped"])
    mapped_m = sum(r["n_mapped"] for r in rows_out)
    gold_n = sum(r["n_gold"] for r in rows_out)
    report = [
        f"# EO stage metrics ({spec.key})",
        "",
        f"题目 {n}；全映射题 {full_map}/{n}；gold 条 {mapped_m}/{gold_n}",
        f"L1/L2 只在已映射集合（{mn} 题）上平均，空映射不计入。",
        f"recall@pool **{100.0 * pool_m:.1f}%**",
        f"recall@inject **{100.0 * inj_m:.1f}%**",
        f"recall@skeleton **{100.0 * skel_m:.1f}%**",
        "",
        "| qid | gold | unmap | pool | inject | skeleton | pool_n | chars |",
        "|-----|------|-------|------|--------|----------|--------|-------|",
    ]
    for r in rows_out:
        report.append(
            f"| {r['question_id']} | {r['n_mapped']}/{r['n_gold']} | {r['n_unmap']} | "
            f"{r['recall_pool']:.2f} | {r['recall_inject']:.2f} | {r['recall_skeleton']:.2f} | "
            f"{r['pool_size']} | {r['inject_chars']} |"
        )
    from eval.BEAM_official.oracle import gold_struct_path

    struct_p = gold_struct_path(dest_dir, spec.key)
    report.extend(["", "## 非单调 gold（墙钟序可能 ≠ 金标序）", ""])
    if os.path.isfile(struct_p):
        raw_g = json.loads(open(struct_p, encoding="utf-8").read())
        report.append("| conv | q | ids | sessions |")
        report.append("|------|---|-----|----------|")
        n_nm = 0
        if isinstance(raw_g, list):
            for row in raw_g:
                if not isinstance(row, dict):
                    continue
                if "mono" in row and row["mono"] is False:
                    n_nm += 1
                    ids = row["ids"] if "ids" in row else []
                    sess = row["sessions"] if "sessions" in row else []
                    report.append(
                        f"| {row['conv'] if 'conv' in row else '?'} | "
                        f"{row['q'] if 'q' in row else '?'} | {ids} | {sess} |"
                    )
        report.append("")
        report.append(f"非单调 {n_nm} 题。代码仍按 (valid_at, turn_index) 排。")
    out_md = os.path.join(dest_dir, "eo_stage_metrics.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    dump = os.path.join(dest_dir, "eo_stage_metrics.json")
    with open(dump, "w", encoding="utf-8") as f:
        json.dump(rows_out, f, ensure_ascii=False, indent=2)
    print(f"[metrics] pool={pool_m:.3f} inject={inj_m:.3f} skeleton={skel_m:.3f} -> {out_md}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EO funnel metrics (no answer LLM)")
    p.add_argument("--scale", default="100k", choices=tuple(SCALES))
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--timeout", type=float, default=120.0)
    return p


def main() -> int:
    args = build_parser().parse_args()
    import asyncio

    return asyncio.run(run_metrics(str(args.scale), str(args.base_url).rstrip("/"), float(args.timeout)))


if __name__ == "__main__":
    raise SystemExit(main())
