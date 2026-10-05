"""下载多个 taffy 版本的 .crate 源码，提取 subdivide_segment 对比断言是否还在。

用途：确定「升到哪个 taffy 版本能修掉 compute/float.rs 的退化 panic」。
不依赖 changelog 措辞，直接读源码。

用法::

    uv run python eval/manual/taffy_float_versions.py
"""

from __future__ import annotations

import io
import re
import sys
import tarfile
import urllib.request

VERSIONS = ("0.11.0", "0.12.0", "0.12.1", "0.12.2", "0.13.0", "0.14.0")
FLOAT_RS = "src/compute/float.rs"
ASSERT = "old_segment.y.contains(&divide_at_y)"


def fetch(version: str) -> str | None:
    url = f"https://static.crates.io/crates/taffy/taffy-{version}.crate"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
            blob = resp.read()
    except OSError as e:
        print(f"[{version}] 下载失败: {e}")
        return None
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        try:
            member = tf.getmember(f"taffy-{version}/{FLOAT_RS}")
        except KeyError:
            print(f"[{version}] 无 {FLOAT_RS}")
            return None
        handle = tf.extractfile(member)
        if handle is None:
            print(f"[{version}] 读不到 {FLOAT_RS}")
            return None
        return handle.read().decode("utf-8", errors="replace")


def report(version: str, src: str) -> None:
    has_fn = "subdivide_segment" in src
    has_assert = ASSERT in src
    # 抓 subdivide_segment 函数体
    m = re.search(r"fn subdivide_segment.*?\n    \}", src, re.DOTALL)
    body = m.group(0) if m else "(未找到 subdivide_segment)"
    # 是否还有 assert / 分支退化处理
    has_log = "debug_log" in body
    print(f"\n=== taffy {version} ===")
    print(f"  subdivide_segment 存在: {has_fn}")
    print(f"  该断言仍在        : {has_assert}")
    print(f"  内部走 debug 分支 : {has_log}")
    print("  ---- 函数体 ----")
    for line in body.splitlines():
        print("  " + line)


def main() -> int:
    for v in VERSIONS:
        src = fetch(v)
        if src is None:
            continue
        report(v, src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
