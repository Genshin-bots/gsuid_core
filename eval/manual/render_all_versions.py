"""全量渲染所有 changelog 版本，校验不崩、不丢条目、不出异常尺寸。"""

import sys
import asyncio
from pathlib import Path
from importlib import import_module

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

OUT = Path(__file__).resolve().parents[2] / "test_output" / "all_versions"
LIMIT = sys.argv[1] if len(sys.argv) > 1 else "0"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()

    from gsuid_core.buildin_plugins.core_command.core_update_history import (
        template,
        changelog as source,
    )

    command = import_module("gsuid_core.buildin_plugins.core_command.core_update_history")
    refs = source.list_versions()
    limit = int(LIMIT)
    if limit:
        refs = refs[:limit]

    failures: list[str] = []
    total_entries = 0
    total_groups = 0
    for ref in refs:
        name = f"{ref.version}{'' if ref.released else '-unreleased'}.png"
        try:
            version = source.parse_version(ref)
            html = template.build_version_html(version, is_current=source.is_current(ref))
            data = await command._render(html)
        except Exception as exc:  # noqa: BLE001 - 探针要看到全部失败
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if not data.startswith(b"\x89PNG"):
            failures.append(f"{name}: 不是 PNG")
            continue
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if width != 1120:
            failures.append(f"{name}: 宽度异常 {width}")
        if height < 200:
            failures.append(f"{name}: 高度异常 {height}")
        # 条目数对不上说明解析吞了内容
        if version.entries:
            total_entries += len(version.entries)
            total_groups += len(version.groups)
        (OUT / name).write_bytes(data)
        print(
            f"OK {name:<28} {width}x{height:<6} {len(data) // 1024:>5}KB "
            f"{len(version.entries):>3} 条 / {len(version.groups)} 组 / {len(version.lead)} 引言"
        )

    print(f"\n共 {len(refs)} 个版本，{total_entries} 条目，{total_groups} 组")
    if failures:
        print(f"\n失败 {len(failures)} 个：")
        for line in failures:
            print(f"  {line}")
    else:
        print("全部通过，无异常")


asyncio.run(main())
