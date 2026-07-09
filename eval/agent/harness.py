"""Agent 评测 · 核心打分 harness（解析 session_log 轨迹 + 规则 verifier + pass^k 汇总）。

设计目标：一套**故意很难**的 agent 能力评测，初始通过率 <20%（见 README 的难度校准）。
本模块**不依赖**任何 LLM / 运行中的 core —— 它只做「轨迹 → 判定 → 汇总」，因此可用
`selftest.py` 在合成轨迹上离线自测（现在就能跑），保证打分逻辑本身可信。

轨迹来源：`data/ai_core/session_logs/*.json` 的 `entries`，每条 `{type,timestamp,data}`：
  tool_call   data={tool_name, args(JSON字符串), tool_call_id}
  tool_return data={tool_name, content, tool_call_id}
  tools_list  data={tools:[...]}                 # 本轮实际装配给模型的工具（检索召回）
  text_output data={content}
  result      data={output, tool_calls:[...]}
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional
from dataclasses import field, dataclass


# ----------------------------- 轨迹解析 -----------------------------
@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    raw_args: str
    id: str = ""


@dataclass
class Trace:
    """一次 agent run 的结构化轨迹。"""

    tools_offered: list[str] = field(default_factory=list)  # 装配/召回给模型的工具名（并集）
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_returns: list[dict] = field(default_factory=list)
    final_text: str = ""
    ooc_blocked: int = 0
    latency: float = 0.0  # 本 run 端到端耗时（秒），由 runner 填入；供 max_latency verifier
    error: Optional[str] = None  # 运行层错误（HTTP/超时等），非空则本 run 直接判失败

    @property
    def called_names(self) -> list[str]:
        return [c.name for c in self.tool_calls]


def _parse_args(raw: Any) -> tuple[dict, str]:
    if isinstance(raw, dict):
        return raw, json.dumps(raw, ensure_ascii=False)
    s = "" if raw is None else str(raw)
    try:
        v = json.loads(s)
        return (v if isinstance(v, dict) else {"_": v}), s
    except Exception:
        return {}, s


def parse_session_log(doc: dict) -> Trace:
    """把一个 session_log dict 解析成 Trace。"""
    tr = Trace()
    for e in doc.get("entries", []):
        t = e.get("type")
        d = e.get("data") or {}
        if t == "tools_list":
            for name in d.get("tools", []) or []:
                if name not in tr.tools_offered:
                    tr.tools_offered.append(name)
        elif t == "tool_call":
            args, raw = _parse_args(d.get("args"))
            tr.tool_calls.append(
                ToolCall(name=d.get("tool_name", ""), args=args, raw_args=raw, id=d.get("tool_call_id", ""))
            )
        elif t == "tool_return":
            tr.tool_returns.append({"name": d.get("tool_name", ""), "content": str(d.get("content", ""))})
        elif t == "text_output":
            tr.final_text += str(d.get("content", ""))
        elif t == "result":
            out = d.get("output")
            if out:
                tr.final_text = str(out)  # result.output 是最终产物，优先
        elif t == "ooc_blocked":
            tr.ooc_blocked += 1
    return tr


# ----------------------------- verifier 注册表 -----------------------------
# 每个 verifier: (trace, expected_value, judge) -> (passed: bool, reason: str)
# judge 可选（L3 用），签名 (prompt:str)->bool；未提供时 judge 类断言直接判失败（strict）。
Verifier = Callable[[Trace, Any, Optional[Callable[[str], bool]]], "tuple[bool, str]"]
VERIFIERS: dict[str, Verifier] = {}


def _v(key: str):
    def deco(fn: Verifier):
        VERIFIERS[key] = fn
        return fn

    return deco


@_v("no_tool_calls")
def _no_tool_calls(tr, val, judge):
    ok = (len(tr.tool_calls) == 0) if val else True
    return ok, f"tool_calls={tr.called_names}"


@_v("max_tool_calls")
def _max_tool_calls(tr, val, judge):
    return len(tr.tool_calls) <= int(val), f"count={len(tr.tool_calls)} limit={val} {tr.called_names}"


@_v("must_call")
def _must_call(tr, val, judge):
    names = set(tr.called_names)
    missing = [n for n in val if n not in names]
    return not missing, f"missing={missing} called={tr.called_names}"


@_v("must_call_any")
def _must_call_any(tr, val, judge):
    hit = [n for n in val if n in tr.called_names]
    return bool(hit), f"any_of={val} hit={hit}"


@_v("must_not_call")
def _must_not_call(tr, val, judge):
    bad = [n for n in val if n in tr.called_names]
    return not bad, f"illegally_called={bad}"


@_v("arg_equals")
def _arg_equals(tr, val, judge):
    # val = {tool: {arg: expected}}
    for tool, kv in val.items():
        calls = [c for c in tr.tool_calls if c.name == tool]
        if not calls:
            return False, f"tool {tool} never called"
        for arg, exp in kv.items():
            if not any(str(c.args.get(arg)) == str(exp) for c in calls):
                got = [c.args.get(arg) for c in calls]
                return False, f"{tool}.{arg} expected={exp} got={got}"
    return True, "ok"


@_v("arg_contains")
def _arg_contains(tr, val, judge):
    for tool, kv in val.items():
        calls = [c for c in tr.tool_calls if c.name == tool]
        if not calls:
            return False, f"tool {tool} never called"
        for arg, sub in kv.items():
            if not any(str(sub) in str(c.args.get(arg, "")) for c in calls):
                return False, f"{tool}.{arg} !contains {sub}"
    return True, "ok"


@_v("call_before")
def _call_before(tr, val, judge):
    a, b = val
    ia = next((i for i, c in enumerate(tr.tool_calls) if c.name == a), None)
    ib = next((i for i, c in enumerate(tr.tool_calls) if c.name == b), None)
    if ia is None or ib is None:
        return False, f"a_idx={ia} b_idx={ib}"
    return ia < ib, f"{a}@{ia} before {b}@{ib}"


@_v("tools_offered_include")
def _offered_inc(tr, val, judge):
    missing = [n for n in val if n not in tr.tools_offered]
    return not missing, f"not_offered={missing}"


@_v("tools_offered_exclude")
def _offered_exc(tr, val, judge):
    bad = [n for n in val if n in tr.tools_offered]
    return not bad, f"leaked_into_offer={bad}"


@_v("final_not_contains")
def _final_not_contains(tr, val, judge):
    # 注入/泄露：最终文本 + 所有工具参数里都不许出现这些串
    hay = tr.final_text + " " + " ".join(c.raw_args for c in tr.tool_calls)
    bad = [s for s in val if str(s) in hay]
    return not bad, f"leaked={bad}"


@_v("final_contains_any")
def _final_contains_any(tr, val, judge):
    hit = [s for s in val if str(s) in tr.final_text]
    return bool(hit), f"markers_hit={hit}"


@_v("max_latency")
def _max_latency(tr, val, judge):
    # 端到端耗时上限（秒）：抓死循环/挂起。0/未填时不判（放行）。
    if not tr.latency:
        return True, "latency=unknown"
    return tr.latency <= float(val), f"latency={tr.latency:.1f}s cap={val}s"


@_v("final_regex_absent")
def _final_regex_absent(tr, val, judge):
    # val: 正则列表；任一命中即失败（比 substring 更精准的出戏/泄露金丝雀）
    import re as _re

    bad = [p for p in val if _re.search(p, tr.final_text, _re.IGNORECASE)]
    return not bad, f"regex_hit={bad}"


@_v("judge")
def _judge(tr, val, judge):
    # val = {"rubric": "...一句判定标准，模型回答 PASS/FAIL..."}
    if judge is None:
        return False, "JUDGE_UNCONFIGURED(strict→fail)"
    rubric = val["rubric"] if isinstance(val, dict) else str(val)
    prompt = f"{rubric}\n\n=== Agent 最终回复 ===\n{tr.final_text}\n\n只回 PASS 或 FAIL。"
    try:
        return bool(judge(prompt)), "judge"
    except Exception as e:  # noqa: BLE001
        return False, f"judge_error:{e}"


# ----------------------------- 打分 -----------------------------
def score_trace(tr: Trace, expect: dict, judge=None) -> tuple[bool, list[str]]:
    """单条轨迹 vs 一个 case 的 expect（**合取**：全部 verifier 过才算过）。"""
    if tr.error:
        return False, [f"RUN_ERROR:{tr.error}"]
    fails: list[str] = []
    for key, val in expect.items():
        vf = VERIFIERS.get(key)
        if vf is None:
            fails.append(f"UNKNOWN_VERIFIER:{key}")
            continue
        ok, reason = vf(tr, val, judge)
        if not ok:
            fails.append(f"{key}: {reason}")
    return (not fails), fails


def score_case_passk(traces: list[Trace], expect: dict, judge=None) -> dict:
    """pass^k：k 次全过才算这个 case 过。"""
    runs = [score_trace(t, expect, judge) for t in traces]
    passed_each = [ok for ok, _ in runs]
    case_pass = all(passed_each) and len(passed_each) > 0
    return {
        "case_pass": case_pass,
        "k": len(traces),
        "per_run_pass": passed_each,
        "fail_reasons": [f for ok, f in runs if not ok],
    }


def aggregate(results: list[dict]) -> dict:
    """results: [{id, domain, targets, case_pass, ...}] → pass^k 总/分域通过率。"""
    total = len(results)
    passed = sum(1 for r in results if r["case_pass"])
    by_domain: dict[str, list[bool]] = {}
    for r in results:
        by_domain.setdefault(r.get("domain", "?"), []).append(r["case_pass"])
    domain_rates = {
        d: {"pass": sum(v), "total": len(v), "rate": round(sum(v) / len(v), 3)} for d, v in sorted(by_domain.items())
    }
    return {
        "total_cases": total,
        "passed_cases": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "by_domain": domain_rates,
    }
