"""BEAM-10M §14 全量图谱构建：可断点续跑的窗口化抽取驱动。

把 conv 的每个 plan 通过 ``/api/ai/memory/batch_observe``（``extract=true``）做
**窗口化实体/边抽取**（Episode 粒度与抽取批次粒度解耦，见文档 §14.1），逐 plan 落状态
文件断点续跑；全部 plan 抽完后触发一次分层图 ``rebuild`` 并轮询至收敛（§14.2 统一 rebuild）。

设计要点（吸取 §13 教训）：
- **逐 plan 一次 HTTP 调用 + 状态文件**：单个后台任务被砍也能从下一个未完成 plan 续跑（W8）。
- **默认 write_episodes=False**：conv0 的 64277 条 granular Episode 已摄入且向量校验为 100%，
  只补抽取，避免重复嵌入 6 万+ 条、也规避 §5 的高并发重嵌入丢向量。需要全新摄入时加
  ``--write-episodes``。
- **抽取窗口化 + 每窗口宽松超时跳过**：服务端实现，单窗口超时只跳过该窗口，不丢整 plan（§14.2）。
- **rebuild 轮询 + 周期性重触发**：重建被 backlog 上限（MAX_ENTITIES_PER_REBUILD=800）分多轮，
  服务端 capped 会自动续调度；驱动每轮再触发一次（lock 幂等）兜底，直到 category 稳定。

用法::

  # 全量：plan 1..10 抽取 → rebuild（断点续跑安全，可重复执行）
  PYTHONIOENCODING=utf-8 python eval/BEAM_10M/ingest_graph.py --conv 0

  # 子集快速验证：只抽 plan 1 的前 8 个窗口，不 rebuild
  PYTHONIOENCODING=utf-8 python eval/BEAM_10M/ingest_graph.py --conv 0 --plans 1 \
      --max-windows 8 --no-rebuild

  # 仅做 rebuild（抽取已完成）
  PYTHONIOENCODING=utf-8 python eval/BEAM_10M/ingest_graph.py --conv 0 --rebuild-only
"""

from __future__ import annotations

import os
import sys
import json
import time
import asyncio
import sqlite3
import argparse
from typing import Any, Dict, List

import httpx

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from eval.common import (  # noqa: E402
    DEFAULT_BASE_URL,
    call_batch_observe,
    call_rebuild_hiergraph,
)
from eval.BEAM_10M.run_beam_eval import (  # noqa: E402
    USER_ID_TEMPLATE,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PARQUET_GLOB,
    _resolve_plans,
    load_beam_dataset,
    parse_time_anchor,
    extract_turns_from_plan,
)

DB_PATH = os.path.join(_PROJECT_ROOT, "data", "GsData.db")


# ─────────────────────────────────────────────
# 状态文件（断点续跑）
# ─────────────────────────────────────────────


def _state_path(output_dir: str, conv: int) -> str:
    return os.path.join(output_dir, f"graph_ingest_state_{conv}.json")


def load_state(output_dir: str, conv: int) -> Dict[str, Any]:
    p = _state_path(output_dir, conv)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("done_plans"), list):
                return d
        except Exception as e:
            print(f"[state] 读取失败（重新开始）: {e}")
    return {"done_plans": [], "plan_stats": {}}


def save_state(output_dir: str, conv: int, state: Dict[str, Any]) -> None:
    p = _state_path(output_dir, conv)
    os.makedirs(output_dir, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ─────────────────────────────────────────────
# DB 计数（只读，WAL 下与服务并发安全）
# ─────────────────────────────────────────────


def db_counts(scope_key: str) -> Dict[str, int]:
    uri = f"file:{DB_PATH}?mode=ro"
    out = {"episodes": -1, "entities": -1, "edges": -1, "categories": -1, "max_layer": -1}
    try:
        c = sqlite3.connect(uri, uri=True, timeout=30)
        c.execute("PRAGMA busy_timeout=30000")
    except Exception as e:
        print(f"[db] 连接失败: {e}")
        return out

    def q(sql: str, *a: Any) -> int:
        try:
            r = c.execute(sql, a).fetchone()
            return int(r[0]) if r and r[0] is not None else 0
        except Exception:
            return -1

    out["episodes"] = q("select count(*) from aimemepisode where scope_key=?", scope_key)
    out["entities"] = q("select count(*) from aimementity where scope_key=?", scope_key)
    out["edges"] = q("select count(*) from aimemedge where scope_key=?", scope_key)
    out["categories"] = q("select count(*) from aimemcategory where scope_key=?", scope_key)
    out["max_layer"] = q("select max_layer from aimemhierarchicalgraphmeta where scope_key=?", scope_key)
    c.close()
    return out


# ─────────────────────────────────────────────
# 抽取（逐 plan）
# ─────────────────────────────────────────────


def _plan_payload_turns(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把一个 plan 的所有 turn 规整成 batch_observe 的 payload（含 timestamp）。"""
    payload_turns: List[Dict[str, Any]] = []
    for t in extract_turns_from_plan(plan):
        item: Dict[str, Any] = {"role": t["role"], "content": t["content"]}
        iso = parse_time_anchor(t.get("time_anchor", ""))
        if iso:
            item["timestamp"] = iso
        payload_turns.append(item)
    return payload_turns


async def ingest_segment_extract(
    client: httpx.AsyncClient,
    base_url: str,
    user_id: str,
    payload_turns: List[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    """对一段 turn（一个 plan 或其一个 segment）做窗口化抽取。幂等：实体按 name、边按
    (src,tgt)+精确 fact 归并，重跑不重复污染，故 segment 级续跑安全叠加。"""
    extra = {
        "extract": True,
        "write_episodes": args.write_episodes,
        "extract_window_chars": args.window_chars,
        "extract_window_turns": args.window_turns,
        "extract_window_timeout": args.window_timeout,
        "extract_concurrency": args.concurrency,
        "extract_max_windows": args.max_windows,
    }
    return await call_batch_observe(
        client=client,
        base_url=base_url,
        user_id=user_id,
        turns=payload_turns,
        scope_type="user_global",
        flush=False,
        trigger_rebuild=False,
        timeout=args.timeout,
        extra_payload=extra,
    )


# ─────────────────────────────────────────────
# rebuild + 轮询收敛
# ─────────────────────────────────────────────


async def rebuild_and_wait(base_url: str, scope_key: str, args: argparse.Namespace) -> None:
    print(f"\n[rebuild] 触发分层图重建 scope={scope_key}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        r = await call_rebuild_hiergraph(client, base_url, scope_key)
        print(f"[rebuild] trigger -> {r}")
        t0 = time.time()
        last_cat = -1
        stable = 0
        while time.time() - t0 < args.rebuild_max_seconds:
            await asyncio.sleep(args.poll_interval)
            cnt = db_counts(scope_key)
            elapsed = int(time.time() - t0)
            print(
                f"[rebuild] [{elapsed}s] categories={cnt['categories']} "
                f"max_layer={cnt['max_layer']} entities={cnt['entities']} edges={cnt['edges']}"
            )
            # 周期性重触发：capped 多轮收敛靠服务端自动续调度，这里再触发一次（lock 幂等）兜底
            await call_rebuild_hiergraph(client, base_url, scope_key)
            if cnt["categories"] == last_cat and cnt["categories"] > 0 and (cnt["max_layer"] or 0) > 0:
                stable += 1
                if stable >= args.stable_polls:
                    print(f"[rebuild] 收敛（category 连续 {stable} 轮稳定）")
                    break
            else:
                stable = 0
                last_cat = cnt["categories"]
        print(f"[rebuild] 最终: {db_counts(scope_key)}")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BEAM-10M §14 全量图谱构建驱动")
    p.add_argument("--conv", type=int, default=0)
    p.add_argument("--plans", default="", help="逗号分隔 plan 序号（1..10）；空=全部 1..10")
    p.add_argument("--data", default=DEFAULT_PARQUET_GLOB)
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--timeout", type=float, default=6000.0, help="单 plan 抽取 HTTP 超时（秒）")
    # 窗口化抽取参数
    p.add_argument("--window-chars", type=int, default=12000)
    p.add_argument("--window-turns", type=int, default=20)
    p.add_argument("--window-timeout", type=float, default=300.0)
    p.add_argument("--concurrency", type=int, default=0, help="0=服务端用 llm_semaphore_limit")
    p.add_argument("--max-windows", type=int, default=0, help="仅抽前 N 窗口（子集验证用），0=不限")
    p.add_argument(
        "--segment-turns",
        type=int,
        default=250,
        help="段级 checkpoint 粒度：每段最多 N 个 turn（~N/4 窗口）；中断只丢当前段",
    )
    p.add_argument("--write-episodes", dest="write_episodes", action="store_true", default=False)
    # rebuild
    p.add_argument("--rebuild", dest="rebuild", action="store_true", default=True)
    p.add_argument("--no-rebuild", dest="rebuild", action="store_false")
    p.add_argument("--rebuild-only", dest="rebuild_only", action="store_true", default=False)
    p.add_argument("--poll-interval", type=float, default=30.0)
    p.add_argument("--stable-polls", type=int, default=3)
    p.add_argument("--rebuild-max-seconds", type=float, default=5400.0, help="rebuild 轮询上限（默认 90min）")
    return p


async def main_async(args: argparse.Namespace) -> int:
    user_id = USER_ID_TEMPLATE.format(conv_id=args.conv)
    scope_key = f"user_global:{user_id}"
    plan_ids = [int(x) for x in args.plans.split(",") if x.strip()] if args.plans else list(range(1, 11))

    print(f"[start] conv={args.conv} user_id={user_id} plans={plan_ids}")
    print(f"[start] 初始计数: {db_counts(scope_key)}")

    state = load_state(args.output_dir, args.conv)

    if not args.rebuild_only:
        rows = load_beam_dataset(args.data)
        if args.conv < 0 or args.conv >= len(rows):
            print(f"[error] conv={args.conv} 越界，共 {len(rows)} 条")
            return 2
        row = rows[args.conv]

        # segment 级续跑：把每个 plan 的 turn 切成 ~segment_turns 一段，逐段 checkpoint。
        # 单 plan ~3.5–4.5h > HTTP 超时/易被环境杀；段级落状态后，中断只丢当前段(~数十min)、
        # 不必从 plan 头重跑（幂等叠加，见 §7.2.1）。max_windows 验证模式仍走整 plan 不落状态。
        done_segments: Dict[str, List[int]] = state.setdefault("done_segments", {})
        async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
            for pid in plan_ids:
                if pid in state["done_plans"] and not args.max_windows:
                    print(f"[plan {pid}] 已完成，跳过")
                    continue
                plans = _resolve_plans(row, [pid])
                if not plans:
                    print(f"[plan {pid}] 数据中未找到，跳过")
                    continue
                payload = _plan_payload_turns(plans[0])
                seg = max(1, args.segment_turns)
                # max_windows 验证模式：只跑前一段、不落状态
                segments = (
                    [payload] if args.max_windows else [payload[i : i + seg] for i in range(0, len(payload), seg)]
                )
                pdone = set(done_segments.get(str(pid), []))
                print(
                    f"\n[plan {pid}] turns={len(payload)} segments={len(segments)} "
                    f"(每段≤{seg} turns) 已完成段={sorted(pdone)}"
                )
                for sidx, seg_turns in enumerate(segments):
                    if not args.max_windows and sidx in pdone:
                        continue
                    t0 = time.time()
                    print(f"[plan {pid}/seg {sidx + 1}/{len(segments)}] 抽取 turns={len(seg_turns)} ...")
                    resp = await ingest_segment_extract(client, args.base_url, user_id, seg_turns, args)
                    dt = time.time() - t0
                    if resp.get("status") != 0:
                        print(f"[plan {pid}/seg {sidx + 1}] 失败: {resp.get('msg')} —— 中止，可重跑续跑")
                        return 1
                    ex = (resp.get("data") or {}).get("extract") or {}
                    print(
                        f"[plan {pid}/seg {sidx + 1}] OK {dt:.0f}s windows={ex.get('windows_total')} "
                        f"done={ex.get('windows_done')} failed={ex.get('windows_failed')} "
                        f"+entities={ex.get('entities_added')} +edges={ex.get('edges_added')} | {db_counts(scope_key)}"
                    )
                    if not args.max_windows:
                        pdone.add(sidx)
                        done_segments[str(pid)] = sorted(pdone)
                        save_state(args.output_dir, args.conv, state)
                # 全部段完成 → 标记 plan done
                if not args.max_windows and len(pdone) >= len(segments):
                    if pid not in state["done_plans"]:
                        state["done_plans"].append(pid)
                    save_state(args.output_dir, args.conv, state)
                    print(f"[plan {pid}] 全部 {len(segments)} 段完成 ✓ 累计计数: {db_counts(scope_key)}")

    # rebuild 阶段
    if args.rebuild and not args.max_windows:
        all_done = set(plan_ids).issubset(set(state["done_plans"])) or args.rebuild_only
        if all_done:
            await rebuild_and_wait(args.base_url, scope_key, args)
        else:
            remaining = sorted(set(plan_ids) - set(state["done_plans"]))
            print(f"[rebuild] 跳过：仍有未完成 plan {remaining}")

    print(f"\n[done] 最终计数: {db_counts(scope_key)}")
    print(f"[done] 状态: done_plans={state['done_plans']}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    t0 = time.time()
    rc = asyncio.run(main_async(args))
    print(f"\n[exit] elapsed={time.time() - t0:.0f}s rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
