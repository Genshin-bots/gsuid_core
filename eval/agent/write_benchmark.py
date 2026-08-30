# ruff: noqa: E501
"""从 Agent k=3 / LongMemEval 结果 JSON 生成 ``docs/BENCHMARK.md``。

用法::

    uv run python -m eval.agent.write_benchmark
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "BENCHMARK.md"
AGENT_RESULTS = ROOT / "eval" / "agent" / "results"
LM_RESULTS = ROOT / "eval" / "longmemeval" / "results"
META_PATH = AGENT_RESULTS / "_benchmark_20260828_meta.json"

HIST_K3 = AGENT_RESULTS / "_reallog_k3_loop2.json"
HIST_K1 = AGENT_RESULTS / "_reallog_k1_fix234.json"


def _load_json(path: Path) -> object | None:
    if not path.exists():
        return None
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _pct(n: int, d: int) -> str:
    if d <= 0:
        return "n/a"
    return f"{n / d * 100:.1f}%"


def _fmt_int(n: int) -> str:
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fail_kind(fails: object) -> str:
    if not fails:
        return "unknown"
    first = fails[0] if isinstance(fails, list) and fails else fails
    if isinstance(first, list) and first:
        first = first[0]
    text = str(first)
    if ":" in text:
        return text.split(":", 1)[0].strip()
    return text[:40]


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    k = (len(ys) - 1) * p
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return ys[lo]
    return ys[lo] * (hi - k) + ys[hi] * (k - lo)


def analyze_agent(doc: dict) -> dict:
    summary = doc.get("summary") or {}
    results = [r for r in (doc.get("results") or []) if isinstance(r, dict)]
    n = len(results)
    passed = sum(1 for r in results if r.get("case_pass"))
    n_je = sum(1 for r in results if r.get("status") == "judge_error")
    scored = n - n_je
    run_pass = 0
    run_total = 0
    lats: list[float] = []
    tools: list[float] = []
    fw = 0
    kinds: Counter[str] = Counter()
    domain_tok: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "in": 0.0,
            "out": 0.0,
            "cr": 0.0,
            "cw": 0.0,
            "lat": 0.0,
            "tools": 0.0,
            "n": 0.0,
            "pass": 0.0,
        }
    )
    fail_rows: list[dict] = []
    for r in results:
        per = r.get("per_run") or []
        if isinstance(per, list):
            run_total += len(per)
            run_pass += sum(1 for x in per if x)
        lat = float(r.get("avg_latency") or 0)
        if lat:
            lats.append(lat)
        tools.append(float(r.get("avg_tools") or 0))
        fw += int(r.get("firewall_saved_runs") or 0)
        d = str(r.get("domain") or "?")
        slot = domain_tok[d]
        slot["in"] += int(r.get("input_tokens") or 0)
        slot["out"] += int(r.get("output_tokens") or 0)
        slot["cr"] += int(r.get("cache_read_tokens") or 0)
        slot["cw"] += int(r.get("cache_write_tokens") or 0)
        slot["lat"] += lat
        slot["tools"] += float(r.get("avg_tools") or 0)
        slot["n"] += 1
        if r.get("case_pass"):
            slot["pass"] += 1
        if not r.get("case_pass") and r.get("status") != "judge_error":
            kind = _fail_kind(r.get("fails"))
            kinds[kind] += 1
            fail_rows.append(
                {
                    "id": r.get("id"),
                    "domain": d,
                    "kind": kind,
                    "lat": lat,
                    "in": int(r.get("input_tokens") or 0),
                    "cache": float(r.get("cache_rate") or 0),
                    "tools": r.get("avg_tools"),
                    "per_run": per,
                    "sample": ((r.get("sample") or {}).get("delivered") or "")[:160],
                    "fail": str((r.get("fails") or [""])[0])[:180],
                }
            )
    tot_in = int(summary.get("input_tokens") or 0) or sum(int(r.get("input_tokens") or 0) for r in results)
    tot_out = int(summary.get("output_tokens") or 0) or sum(int(r.get("output_tokens") or 0) for r in results)
    tot_cr = int(summary.get("cache_read_tokens") or 0) or sum(int(r.get("cache_read_tokens") or 0) for r in results)
    tot_cw = int(summary.get("cache_write_tokens") or 0) or sum(int(r.get("cache_write_tokens") or 0) for r in results)
    return {
        "n": n,
        "passed": passed,
        "scored": scored,
        "judge_error": n_je,
        "pass_rate": (passed / scored) if scored else 0.0,
        "run_pass": run_pass,
        "run_total": run_total,
        "run_rate": (run_pass / run_total) if run_total else 0.0,
        "input": tot_in,
        "output": tot_out,
        "cache_read": tot_cr,
        "cache_write": tot_cw,
        "cache_rate": (tot_cr / tot_in) if tot_in else 0.0,
        "avg_tools": (sum(tools) / len(tools)) if tools else 0.0,
        "avg_lat": (sum(lats) / len(lats)) if lats else 0.0,
        "p50_lat": _percentile(lats, 0.5),
        "p95_lat": _percentile(lats, 0.95),
        "max_lat": max(lats) if lats else 0.0,
        "firewall_saved": fw,
        "fail_kinds": kinds,
        "domain_tok": domain_tok,
        "by_domain": summary.get("by_domain") or {},
        "fail_rows": fail_rows,
        "summary": summary,
    }


def analyze_longmem(judge_path: Path, answers_path: Path | None = None) -> dict | None:
    records = _load_json(judge_path)
    if not isinstance(records, list) or not records:
        return None
    by: dict[str, dict[str, int]] = defaultdict(lambda: {"passed": 0, "total": 0})
    passed = 0
    total = 0
    buckets: Counter[str] = Counter()
    for r in records:
        if not isinstance(r, dict):
            continue
        cat = str(r.get("question_type") or "unknown")
        raw_jd = r.get("judge")
        jd = raw_jd if isinstance(raw_jd, dict) else {}
        ok = bool(jd.get("passed"))
        by[cat]["total"] += 1
        by[cat]["passed"] += int(ok)
        total += 1
        passed += int(ok)
        reason = str(jd.get("reason") or "")
        if ok:
            buckets["pass"] += 1
        elif "search_cognition" in reason or "工具" in reason:
            buckets["tool_no_answer"] += 1
        elif any(x in reason for x in ("没有", "不记得", "不知道", "未提供", "未能", "未给出")):
            buckets["no_memory"] += 1
        else:
            buckets["other_wrong"] += 1
    n_ans = 0
    n_err = 0
    if answers_path and answers_path.exists():
        answers = _load_json(answers_path)
        if isinstance(answers, list):
            n_ans = len(answers)
            n_err = sum(1 for a in answers if isinstance(a, dict) and a.get("status_code") != 200)
    return {
        "passed": passed,
        "total": total,
        "by": dict(by),
        "answers": n_ans,
        "answer_errors": n_err,
        "judge_file": str(judge_path.relative_to(ROOT)).replace("\\", "/"),
        "answers_file": (str(answers_path.relative_to(ROOT)).replace("\\", "/") if answers_path else ""),
        "reason_buckets": dict(buckets),
    }


def _hist_line(label: str, path: Path) -> str:
    doc = _load_json(path)
    if not isinstance(doc, dict):
        return f"| {label} | (文件不存在) |"
    s = doc.get("summary") or {}
    p = int(s.get("passed_cases") or 0)
    t = int(s.get("total_cases") or s.get("scored_cases") or 0)
    lat = s.get("avg_latency_s")
    inn = int(s.get("input_tokens") or 0)
    cr = float(s.get("cache_rate") or 0)
    tools = s.get("avg_tools_per_case")
    return (
        f"| {label} | {p}/{t} ({_pct(p, t)}) | {lat}s | {_fmt_int(inn)} | {cr * 100:.1f}% | {tools} | `{path.name}` |"
    )


def render(meta: dict, agent_doc: dict | None, lm: dict | None) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %z")
    lines: list[str] = []
    a = analyze_agent(agent_doc) if isinstance(agent_doc, dict) else None
    lines += [
        "# GsCore 评测账本（BENCHMARK）",
        "",
        f"> 生成时间：**{now}**。源码仍是唯一事实源。本文件记录一次完整实测，",
        "> 不是发布门槛。k=3 合取会把「2/3 过」的开放题整例判死；08-28 之后",
        "> 90% 发布线看对齐后的 k=1，见 `plans/GROUP_CHAT_AGENT_CACHE_MEMORY_CAPABILITY_JOURNEY_20260828.md` §8.8 / §9。",
        ">",
        "> `cache_rate` = `cache_read / input`。本套评测每例独立 `user_id`，表内缓存率",
        "> 是「几乎全是会话首 run」的地板，**不能**拿去对照方案里「同 session ≥85%」。",
        "",
        "## 0. 这次怎么跑",
        "",
        "| 项 | 值 |",
        "|----|----|",
        f"| 日期 | {meta.get('date', '2026-08-28')}（agent loop 改后清空重跑） |",
        "| Core | 全插件，**无** `--dev`，端口 8765 |",
        "| 环境 | `GSUID_LOCAL_TEST_MODE=1`，`PYTHONUTF8=1`，`NO_PROXY=localhost,127.0.0.1` |",
        "| 高级模型 | `openai++MiniMAX`（`ai_config.high_level_provider_config_name`） |",
        "| 清库 | 开跑前清空对话记忆 SQL + Qdrant `memory_*` 集合 + 全部定时任务；保留 `knowledge` / 插件数据 / 配置 |",
        "| Agent 套件 | `eval/agent` 默认合并集（hard + expansion + prod_patterns + cognition_hub + speaker_slot）；dry-run **496 可跑 / 1 fixture 跳过**（比旅程 447 多了 `speaker_slot_recall` 等） |",
        "| Agent 协议 | `--k 3 --judge bot --concurrency 2 --timeout 360 --delivery-wait 90 --reset-state` |",
        "| 量尺 | 08-28 对齐后的 harness：委派算履约、完成态 latency slack、说话通道不算办事工具、`JUDGE_ERROR` 不进分母 |",
        "| LongMemEval | LongMemEval-S cleaned 500；`batch_observe` 摄入 + `inject_date` + `extract` + `clear_first`；System-1 作答 |",
        "",
        "分块跑 Agent 全量（每块写独立 JSON 再合并），避免单进程十小时墙把整份报告带走。",
        "",
        "## 1. 历史对照（不可与本次逐例对齐的部分）",
        "",
        "数据集略有增减、量尺在 08-28 改过。k=3 loop1/loop2 是**旧尺子**；k1 234 是新尺子 k=1。",
        "本次是**新尺子 + k=3 合取**。",
        "",
        "| 轮次 | 通过 | 延迟 | input | cache_rate | 工具/例 | 报告 |",
        "|------|------|------|-------|------------|---------|------|",
        "| k3_loop1（旧尺） | 325/447 (72.7%) | 32.7s | 13.24M | 57.0% | 0.39 | `_reallog_k3_loop1.json` |",
        _hist_line("k3_loop2（旧尺）", HIST_K3),
        _hist_line("k1 234（新尺 k=1）", HIST_K1),
        "",
        "LongMemEval 历史：`summary_improved_final.json` 记 **443/500 = 88.6%**（date + MiniMAX，受影响题用 improved、其余沿用 baseline）。",
        "",
    ]

    expected_k3 = int(meta.get("k3_expected") or 496)
    k3_incomplete = a is not None and int(a["n"]) < expected_k3
    if a is None:
        lines += [
            "## 2. Agent 硬核套件 · pass^k=3",
            "",
            "_尚未完成。编排进程仍在跑，完成后本节会被覆盖写入。_",
            "",
        ]
    else:
        gap = (a["run_rate"] - a["pass_rate"]) * 100
        lines += [
            "## 2. Agent 硬核套件 · pass^k=3",
            "",
            f"报告：`{meta.get('k3_out', 'eval/agent/results/_benchmark_k3_20260828.json')}`。",
            "",
            (
                f"**进行中**：已评 {a['n']}/{expected_k3} 例，下面分数只覆盖已完成子集，不是全量。"
                if k3_incomplete
                else f"全量 {a['n']} 例已完成。"
            ),
            "",
            "### 2.1 总表",
            "",
            "| 指标 | 值 |",
            "|------|----|",
            f"| **pass^k 通过** | **{a['passed']}/{a['scored']} = {a['pass_rate'] * 100:.1f}%** |",
            f"| 跑的例数 | {a['n']}（judge_error {a['judge_error']} 不进分母） |",
            f"| 单 run 通过 | {a['run_pass']}/{a['run_total']} = {a['run_rate'] * 100:.1f}% |",
            f"| 可靠性缺口（单run − pass^k） | {gap:+.1f} pt |",
            f"| 平均工具数/例 | {a['avg_tools']:.2f} |",
            f"| 平均延迟 / P50 / P95 / max | {a['avg_lat']:.1f}s / {a['p50_lat']:.1f}s / {a['p95_lat']:.1f}s / {a['max_lat']:.1f}s |",
            f"| input tokens | {a['input']:,} ({_fmt_int(a['input'])}) |",
            f"| output tokens | {a['output']:,} ({_fmt_int(a['output'])}) |",
            f"| cache_read | {a['cache_read']:,} ({_fmt_int(a['cache_read'])}) |",
            f"| cache_write | {a['cache_write']:,} |",
            f"| **cache_rate** | **{a['cache_rate'] * 100:.1f}%** |",
            f"| 出戏防火墙救场 run | {a['firewall_saved']} |",
            f"| 墙钟 | {meta.get('k3_elapsed_s', '—')} |",
            "",
            "### 2.2 分域（pass^k + token + 缓存 + 延迟）",
            "",
            "| 域 | pass^k | 单域 cache_rate | input | output | 均延迟 | 均工具 |",
            "|----|--------|-----------------|-------|--------|--------|--------|",
        ]
        by = a["by_domain"]
        for d in sorted(by.keys()):
            v = by[d]
            tok = a["domain_tok"].get(d) or {}
            inn = int(tok.get("in") or 0)
            out = int(tok.get("out") or 0)
            cr = float(tok.get("cr") or 0)
            n_d = int(tok.get("n") or v.get("total") or 0)
            cr_rate = (cr / inn) if inn else 0.0
            lat = (float(tok.get("lat") or 0) / n_d) if n_d else 0.0
            tools = (float(tok.get("tools") or 0) / n_d) if n_d else 0.0
            lines.append(
                f"| `{d}` | {v.get('pass')}/{v.get('total')} ({float(v.get('rate') or 0) * 100:.1f}%) | "
                f"{cr_rate * 100:.1f}% | {_fmt_int(inn)} | {_fmt_int(out)} | {lat:.1f}s | {tools:.2f} |"
            )
        lines += [
            "",
            "### 2.3 失败类型（合取第一条断言）",
            "",
            "| 类型 | 例数 | 占失败 |",
            "|------|------|--------|",
        ]
        n_fail = max(sum(a["fail_kinds"].values()), 1)
        for kind, n in a["fail_kinds"].most_common():
            lines.append(f"| `{kind}` | {n} | {n / n_fail * 100:.1f}% |")
        if not a["fail_kinds"]:
            lines.append("| （无失败） | 0 | — |")
        lines += [
            "",
            "### 2.4 失败例（id / 域 / 类型 / 延迟 / token）",
            "",
            "| id | 域 | 类型 | per_run | 延迟 | input | cache | 交付摘录 |",
            "|----|----|------|---------|------|-------|-------|----------|",
        ]
        for row in sorted(a["fail_rows"], key=lambda x: (str(x["domain"]), str(x["id"]))):
            sample = str(row["sample"]).replace("|", "\\|").replace("\n", " ")
            if len(sample) > 80:
                sample = sample[:80] + "…"
            lines.append(
                f"| `{row['id']}` | `{row['domain']}` | `{row['kind']}` | {row['per_run']} | "
                f"{row['lat']:.1f}s | {_fmt_int(int(row['in']))} | {float(row['cache']) * 100:.0f}% | {sample} |"
            )
        if not a["fail_rows"]:
            lines.append("| （无） | | | | | | | |")
        hist_k3 = _load_json(HIST_K3)
        hist_k1 = _load_json(HIST_K1)
        if isinstance(hist_k3, dict) or isinstance(hist_k1, dict):
            lines += [
                "",
                "### 2.5 与历史同域对照",
                "",
                "| 域 | 本次 k=3 | k3_loop2 旧尺 | k1 234 |",
                "|----|----------|---------------|--------|",
            ]
            domains = set(by.keys())
            if isinstance(hist_k3, dict):
                domains |= set((hist_k3.get("summary") or {}).get("by_domain") or {})
            if isinstance(hist_k1, dict):
                domains |= set((hist_k1.get("summary") or {}).get("by_domain") or {})

            def _cell(doc: object, d: str) -> str:
                if not isinstance(doc, dict):
                    return "—"
                v = ((doc.get("summary") or {}).get("by_domain") or {}).get(d)
                if not v:
                    return "—"
                return f"{v.get('pass')}/{v.get('total')}"

            for d in sorted(domains):
                cur = by.get(d)
                cur_s = f"{cur.get('pass')}/{cur.get('total')}" if cur else "—"
                lines.append(f"| `{d}` | {cur_s} | {_cell(hist_k3, d)} | {_cell(hist_k1, d)} |")
        lines.append("")

    if lm is None:
        lines += [
            "## 3. LongMemEval-S",
            "",
            "_尚未完成。Agent k=3 跑完后自动开跑。_",
            "",
        ]
    else:
        lines += [
            "## 3. LongMemEval-S",
            "",
            f"答卷：`{lm.get('answers_file')}`；判分：`{lm.get('judge_file')}`。",
            "",
            "### 3.1 总表",
            "",
            "| 指标 | 值 |",
            "|------|----|",
            f"| **准确率** | **{lm['passed']}/{lm['total']} = {_pct(int(lm['passed']), int(lm['total']))}** |",
            f"| 答卷数 / HTTP 非 200 | {lm.get('answers')} / {lm.get('answer_errors')} |",
            f"| 墙钟（最后一次编排续跑） | {meta.get('lm_elapsed_s', '—')}s |",
            "| 协议 | `eval/run_eval.py longmem probe --extract --inject-date --clear-first` + `judge` |",
            "| 历史对照 | improved final 443/500 = 88.6%（date + haystack 当对话史，无 `--extract`） |",
            "",
            "### 3.2 题型",
            "",
            "| 题型 | 通过 | 准确率 |",
            "|------|------|--------|",
        ]
        for cat, s in sorted(lm["by"].items()):
            lines.append(f"| `{cat}` | {s['passed']}/{s['total']} | {_pct(s['passed'], s['total'])} |")
        lines += [
            "",
            "摄入走 `/api/ai/memory/batch_observe`（带 haystack 日期时间戳）；作答走",
            "`/api/chat_with_history`（history 空、靠记忆检索）。`inject_date` 把",
            "`question_date` 写成「当前时间」，否则 temporal-reasoning 会系统性低估。",
            "`extract=True` 在摄入时跑实体/边抽取，System-1 可走图检索而不只是 episode-RAG。",
            "每题独立 `user_id=eval_{question_id}`，`--clear-first` 避免串题。",
            "",
            "### 3.3 读数",
            "",
            "- 与历史 443/500 **不可比**：历史把 haystack 塞进对话史；本次空 history，只靠记忆检索。",
            "- 答卷 HTTP **500/500 全 200**；5.6% 是检索/作答质量，不是传输失败。",
            "- judge 失败主因（reason 归类）："
            f" 无记忆/未给答案 {lm.get('reason_buckets', {}).get('no_memory', 0)}，"
            f"把 `search_cognition` 写进回复 {lm.get('reason_buckets', {}).get('tool_no_answer', 0)}，"
            f"其它错答 {lm.get('reason_buckets', {}).get('other_wrong', 0)}。",
            "- 全量 probe 跨多日；MiniMAX Token Plan 5h 额度用尽时切商汤，窗口到点再切回。",
            "- 上表墙钟只计最后一次编排续跑（20:01–21:08 的 probe 尾 + judge），不是从头到尾的墙钟。",
            "",
            "### 3.4 5.6% 根因与 SSP 复跑（2026-08-30）",
            "",
            "5.6% **不是** judge 口径或 HTTP 失败。抽样 `answers_20260828.json` 的 `memory` 字段几乎全是",
            "`[记忆目录]` 8 条 × 40 字（约 394 字符），且末行要求调 `search_cognition`。",
            "",
            "叠了三层：",
            "",
            "1. **H05 目录卡**：Chat 路径故意只灌标题、正文留给 `search_cognition`。",
            "2. **无工具时仍提示调工具**：模型把 `search_cognition` XML 写进回复（约 170 题）。",
            "3. **memory 块 800 字预算**：`_MEMORY_EVAL_GUIDE` 与检索正文同块，`join_named_blocks` 截到 800 后几乎看不到证据。",
            "",
            "另：`inject_date` 把 `当前时间：…` 拼进 query，向量检索被日期词带偏。",
            "",
            "现行协议（Chat 目录卡不变，仅 `memory_eval`）：`create_by=Chat` + `memory_eval` 灌完整证据会话、",
            "跳过 800 字帽、禁止工具指令、剥时间前缀、词面 SQL 补召。TEST 不再当评测门。judge 走 `as_judge=True`。",
            "",
            "`single-session-preference` 单独复跑（记忆库已在，`--skip-ingest`，30 题）：",
            "",
            "| 轮次 | 答卷 / 判分 | 通过 |",
            "|------|-------------|------|",
            "| 全量 20260828 | `answers_20260828.json` | **3/30 = 10%** |",
            "| 修注入 v2 | `answers_ssp_v2.json` / `judge_ssp_v2.json` | **16/30 = 53.3%** |",
            "| +词面召回 v3 | `answers_ssp_v3.json` / `judge_ssp_v3.json` | **17/30 = 56.7%** |",
            "| 片段优先 v4 | `answers_ssp_v4.json` / `judge_ssp_v4.json` | **16/30 = 53.3%** |",
            "",
            "**未到 95%。** SSP 的证据在约 50 个干扰会话里的**一条**偏好会话（例如西雅图景观酒店 → 问迈阿密酒店），",
            "问句往往不含金标专名（Luna / Garmin / TripIt / turbinado）。System-1 在 ~500 轮 / ~49 万字 haystack 上",
            "召不齐这条会话；judge 还要求点名多个具体细节。历史 88.6% 是把 haystack 整段塞进对话史，与本协议不可比。",
            "",
            "要 ≥95% 需要换协议（证据会话进 history，或可调用的 `search_cognition`），不是再把目录卡截断修回去。",
            "",
        ]

    lines += [
        "## 4. 读数注意",
        "",
        "1. **pass^k=3**：三次全过才算过。单 run 80% 的例 pass^3 只有 51%。开放题 bot-judge 抖动会被合取放大。",
        "2. **缓存率地板**：每例新 `user_id`，前缀缓存几乎只有「同一次 HTTP 多步」和 provider 跨请求前缀；",
        "   不是同人格同会话连跑。方案 ≥85% 要另做同 session 10 run。",
        "3. **cache_write=0**：当前 provider 用量里的 cache_write 字段经常不回；cache_read 仍可信。",
        "4. **延迟**：concurrency=2 仍会排队。完成态回复有 slack（2× cap 或 +30s），未完成仍按 cap 判。",
        "5. **清库范围**：对话记忆四表 + Qdrant `memory_*` + `aischeduledtask` 全删；",
        "   `knowledge` / `aimemeknowledge` / 插件库 / `userfavorability` 非 eval_ 行保留。",
        "6. 对照文件不要覆盖：本次写 `_benchmark_k3_20260828.json` 与 longmem `*_20260828.json`。",
        "",
        "## 5. 复现命令",
        "",
        "```powershell",
        '$env:GSUID_LOCAL_TEST_MODE="1"',
        '$env:GSUID_LOCAL_TEST_TOKEN="<token>"',
        '$env:PYTHONUTF8="1"',
        '$env:NO_PROXY="localhost,127.0.0.1"',
        "uv run python -m eval.agent.reset_state --all-dialogue",
        "uv run core --port 8765",
        "# 另开，等日志「AI 核心初始化全部完成」",
        "uv run python -m eval.agent.run --k 3 --judge bot --concurrency 2 --timeout 360 --delivery-wait 90 --reset-state",
        "uv run python eval/run_eval.py longmem probe --extract --inject-date --clear-first --concurrency 2",
        "uv run python eval/run_eval.py longmem judge",
        "uv run python -m eval.agent.write_benchmark",
        "```",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_docs(meta: dict | None = None) -> Path:
    meta = dict(meta or {})
    saved = _load_json(META_PATH)
    if isinstance(saved, dict):
        merged = dict(saved)
        merged.update(meta)
        meta = merged
    k3_path = Path(meta.get("k3_out") or (AGENT_RESULTS / "_benchmark_k3_20260828.json"))
    if not k3_path.is_absolute():
        k3_path = ROOT / k3_path
    agent_doc = _load_json(k3_path)
    lm_judge = Path(meta.get("lm_judge") or (LM_RESULTS / "judge_20260828.json"))
    lm_ans = Path(meta.get("lm_answers") or (LM_RESULTS / "answers_20260828.json"))
    if not lm_judge.is_absolute():
        lm_judge = ROOT / lm_judge
    if not lm_ans.is_absolute():
        lm_ans = ROOT / lm_ans
    lm = analyze_longmem(lm_judge, lm_ans)
    text = render(meta, agent_doc if isinstance(agent_doc, dict) else None, lm)
    DOCS.parent.mkdir(parents=True, exist_ok=True)
    DOCS.write_text(text, encoding="utf-8")
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {DOCS}")
    return DOCS


if __name__ == "__main__":
    write_docs({"date": "2026-08-28"})
