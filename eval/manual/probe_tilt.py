"""探针：测 pytakumi 下中文「倾斜」到底认哪条声明。

中文没有 italic 字面，font-style 很可能不生效；transform 是否支持也要实测。
四种写法各渲一张，肉眼比对是否真的斜了。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gsuid_core.utils.html_render import render_html_to_bytes

OUT = Path(__file__).resolve().parent / "probe_tilt"

TEXT = "HTML 渲染换后端"
ORANGE = "#f08a3c"

CASES: list[tuple[str, str]] = [
    ("A 无倾斜（对照）", ""),
    ("B font-style:italic", "font-style:italic;"),
    ("C transform:skewX(-8deg)", "transform:skewX(-8deg);"),
    ("D skew + inline-block", "display:inline-block;transform:skewX(-8deg);"),
    ("E rotate(-2deg)", "display:inline-block;transform:rotate(-2deg);"),
    ("F skewY(6deg)", "display:inline-block;transform:skewY(6deg);"),
]

STYLE = """
body{margin:0;background:#0b0b12;font-family:"MiSans","Microsoft YaHei",sans-serif;}
.row{padding:14px 20px;border-bottom:1px solid #222;}
.lab{font-size:9px;color:#777;margin-bottom:6px;}
"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    body = []
    for label, extra in CASES:
        body.append(
            f'<div class="row"><div class="lab">{label}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{ORANGE};{extra}">'
            f"[ {TEXT} ]</div></div>"
        )
    html = f"<!DOCTYPE html><html><head><style>{STYLE}</style></head><body>{''.join(body)}</body></html>"
    data = await render_html_to_bytes(html, max_width=480.0, dpi=192.0, default_font_size=0.0, root_max_width=480.0)
    (OUT / "tilt.png").write_bytes(data)
    print(f"wrote {OUT / 'tilt.png'} ({len(data)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
