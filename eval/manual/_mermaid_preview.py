"""生成两张预览图：正常 Mermaid 出图，以及 mermaid.ink 不可用时的黑框。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from gsuid_core.utils.html_render import mermaid_ink, render_html_to_bytes

OUT = Path(__file__).resolve().parent / "_mermaid_preview"
OUT.mkdir(exist_ok=True)

PAGE = """
<div style="background:#f4f7fb;padding:20px;color:#1a2332;font-family:MiSans,sans-serif;">
  <p style="font-size:28px;font-weight:630;margin:0 0 8px;">流程和时序</p>
  <p style="font-size:16px;margin:0 0 12px;">这一段在图的前面，必须还在。</p>
  <div class="mermaid">graph TD
    开始-->处理
    处理-->|通过| 结束
    处理-->|退回| 开始</div>
  <pre><code class="language-mermaid">sequenceDiagram
    Alice->>Bob: 你好
    Bob-->>Alice: 收到</code></pre>
  <p style="font-size:16px;">饼图在下面。</p>
  ```mermaid
  pie title 占比
    "甲" : 40
    "乙" : 60
  ```
  <p style="font-size:18px;color:#0b6b3a;">页脚还在，颜色是绿的。</p>
</div>
"""

OUTAGE = """
<div style="background:#ffffff;padding:16px;font-family:MiSans,sans-serif;">
  <div style="background:#2255ee;color:#fff;font-size:20px;padding:10px;">页头蓝色</div>
  <div class="mermaid">graph TD; A-->B</div>
  <div style="background:#1f9d55;color:#fff;font-size:20px;padding:10px;">页脚绿色</div>
</div>
"""


def _down(code: str, *, client: httpx.Client | None = None) -> str:
    del code, client
    raise httpx.ConnectError("connection refused")


async def main() -> None:
    png = await render_html_to_bytes(PAGE, max_width=840, dpi=128, default_font_size=16)
    (OUT / "ok.png").write_bytes(png)
    original = mermaid_ink.fetch_mermaid_svg
    mermaid_ink.fetch_mermaid_svg = _down
    try:
        bad = await render_html_to_bytes(OUTAGE, max_width=640, dpi=128, default_font_size=16)
    finally:
        mermaid_ink.fetch_mermaid_svg = original
    (OUT / "down.png").write_bytes(bad)
    print(OUT)


if __name__ == "__main__":
    asyncio.run(main())
