import json
from pathlib import Path

import yaml

old_p = Path("eval/agent/results/_optimized_20260801_full.json")
new_p = Path("eval/agent/results/_optimized_v2_full.json")
old = json.loads(old_p.read_text(encoding="utf-8")) if old_p.exists() else None
new = json.loads(new_p.read_text(encoding="utf-8"))
ns = new["summary"]
print("=== V2 TOTAL ===")
print(f"{ns['passed_cases']}/{ns['total_cases']} = {ns['pass_rate'] * 100:.1f}%")
print(f"avg_tools={ns.get('avg_tools_per_case')} avg_lat={ns.get('avg_latency_s')}")
if old:
    os_ = old["summary"]
    print(
        f"vs V1: {os_['passed_cases']}/{os_['total_cases']} = {os_['pass_rate'] * 100:.1f}%  "
        f"delta={ns['pass_rate'] - os_['pass_rate']:+.1%}"
    )

exp = yaml.safe_load(Path("eval/agent/cases/group_chat_expansion.yaml").read_text(encoding="utf-8"))
exp_ids = {c["id"] for c in exp["cases"]}
exp_res = [r for r in new["results"] if r["id"] in exp_ids]
exp_pass = sum(1 for r in exp_res if r.get("case_pass"))
print(f"group_chat_expansion: {exp_pass}/{len(exp_res)} = {exp_pass / max(1, len(exp_res)) * 100:.1f}%")
if old:
    oexp = [r for r in old["results"] if r["id"] in exp_ids]
    op = sum(1 for r in oexp if r.get("case_pass"))
    print(f"  v1 was: {op}/{len(oexp)} = {op / max(1, len(oexp)) * 100:.1f}%")

main = [r for r in new["results"] if r["id"] not in exp_ids]
mp = sum(1 for r in main if r.get("case_pass"))
print(f"hard_suite: {mp}/{len(main)} = {mp / max(1, len(main)) * 100:.1f}%")

print("\n=== domain (asc) ===")
for d, v in sorted(ns["by_domain"].items(), key=lambda x: x[1]["rate"]):
    line = f"{d:28s} {v['pass']:3d}/{v['total']:3d}  {v['rate'] * 100:5.1f}%"
    if old and d in old["summary"]["by_domain"]:
        ov = old["summary"]["by_domain"][d]
        line += f"  (v1 {ov['rate'] * 100:.0f}%)"
    print(line)

print("\n=== expansion fails ===")
for r in exp_res:
    if not r.get("case_pass"):
        print(f"  {r['id']:40s} {r.get('domain')} {str(r.get('fails'))[:100]}")
