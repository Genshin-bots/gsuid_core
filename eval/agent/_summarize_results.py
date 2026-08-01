import json
from pathlib import Path

import yaml

d = json.loads(Path("eval/agent/results/_optimized_20260801_full.json").read_text(encoding="utf-8"))
s = d["summary"]
print("TOTAL", s["passed_cases"], "/", s["total_cases"], f"{s['pass_rate'] * 100:.1f}%")
print("avg_tools", s.get("avg_tools_per_case"), "avg_lat", s.get("avg_latency_s"))
print()
print("=== by domain (asc rate) ===")
for dom, v in sorted(s["by_domain"].items(), key=lambda x: x[1]["rate"]):
    print(f"{dom:28s} {v['pass']:3d}/{v['total']:3d}  {v['rate'] * 100:5.1f}%")

fails = [r for r in d["results"] if not r.get("case_pass")]
print()
print("FAIL count", len(fails))

exp = yaml.safe_load(Path("eval/agent/cases/group_chat_expansion.yaml").read_text(encoding="utf-8"))
exp_ids = {c["id"] for c in exp["cases"]}
exp_res = [r for r in d["results"] if r["id"] in exp_ids]
exp_pass = sum(1 for r in exp_res if r.get("case_pass"))
print(f"group_chat_expansion: {exp_pass}/{len(exp_res)} = {exp_pass / max(1, len(exp_res)) * 100:.1f}%")
main_res = [r for r in d["results"] if r["id"] not in exp_ids]
main_pass = sum(1 for r in main_res if r.get("case_pass"))
print(f"hard_suite only: {main_pass}/{len(main_res)} = {main_pass / max(1, len(main_res)) * 100:.1f}%")

print()
print("=== expansion fails ===")
for r in exp_res:
    if not r.get("case_pass"):
        print(f"  {r['id']:40s} {r.get('domain')} tools={r.get('avg_tools')} {str(r.get('fails'))[:140]}")

print()
print("=== weak domains fails sample ===")
weak = {"cross_turn_recall", "format_constraint", "implicit_addressing", "multi_speaker", "colloquial_recall"}
for r in fails:
    if r.get("domain") in weak:
        print(f"  {r['id']:40s} {r.get('domain')} {str(r.get('fails'))[:140]}")
