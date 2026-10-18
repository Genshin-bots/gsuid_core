import json
from pathlib import Path

p = Path(r"E:\杂项\QQCache\Download\session_yunzai_onebot_161431712_group_277045897_1792338122709.json")
print("size", p.stat().st_size)
raw = p.read_text(encoding="utf-8")
d = json.loads(raw)
print("type", type(d).__name__)
if isinstance(d, dict):
    print("keys", list(d.keys())[:80])
elif isinstance(d, list):
    print("len", len(d))
    if d:
        print("item0 type", type(d[0]).__name__)
        if isinstance(d[0], dict):
            print("item0 keys", list(d[0].keys())[:40])
