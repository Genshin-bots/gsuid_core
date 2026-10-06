"""离线渲染 `core更新记录` 的卡片，供人工看版式。

用法（仓库根目录）：
    uv run python eval/manual/preview_update_history.py

只渲代表样本，不做全版本回归：最新已发布版、带未发布段的最短版、
条目最少的旧版（` **🎨** ` 无标签写法）、以及版本索引。
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path
from importlib import import_module

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "test_output"
PREFIX = "update_history_"

# (输出名, 版本号, 是否按「当前版本」渲染)；版本号留空 = 默认那张合集图
CASES: list[tuple[str, str, bool]] = [
    ("01_recent_default.png", "", False),
    ("02_latest_released.png", "0.11.0", False),
    ("03_unreleased.png", "0.11.0-unreleased", False),
    ("04_minimal_old.png", "0.7.4", False),
    ("05_index.png", "列表", False),
]


def _clear_outputs() -> None:
    for old in OUTPUT_DIR.glob(f"{PREFIX}*.png"):
        old.unlink()


def _png_size(data: bytes) -> str:
    """从 PNG 头部读宽高，避免为了打印尺寸再引一个图像库。"""
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return f"{width}x{height}"


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _clear_outputs()

    from gsuid_core.version import __version__
    from gsuid_core.buildin_plugins.core_command.core_update_history import (
        template,
        changelog as source,
    )

    command = import_module("gsuid_core.buildin_plugins.core_command.core_update_history")

    refs = source.list_versions()
    recent = source.pick_recent(refs)
    print(f"core __version__={__version__} changelogs 版本数={len(refs)}")
    labels = " + ".join(f"{ref.version}{'' if ref.released else '（未发布）'}" for ref in recent)
    print(f"默认合集: {labels or '无'}")
    print(f"最新一条: {refs[0].version if refs else '无'} released={refs[0].released if refs else '-'}\n")

    for name, query, force_current in CASES:
        if query == "列表":
            html = template.build_index_html(refs, total=len(refs))
            detail = f"{len(refs)} 个版本"
        elif not query:
            versions = [source.parse_version(ref) for ref in recent]
            html = template.build_recent_html(versions)
            detail = "合集 " + " + ".join(v.version for v in versions)
        else:
            ref = source.resolve_query(query, refs)
            if ref is None:
                print(f"SKIP {name}: 找不到 {query}")
                continue
            version = source.parse_version(ref)
            html = template.build_version_html(version, is_current=force_current or source.is_current(ref))
            detail = f"{len(version.entries)} 条 · {version.date or '无日期'} · {version.subtitle[:18]}"

        data = await command._render(html)
        path = OUTPUT_DIR / f"{PREFIX}{name}"
        path.write_bytes(data)
        print(f"OK {path.name:<28} {_png_size(data):>11}  {len(data) // 1024:>5}KB  {detail}")

    print(f"\n输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
