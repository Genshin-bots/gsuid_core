"""把命中的 float 用例最小化，定位 taffy panic 的必要条件。

目的不是好看，而是回答「该禁什么」：
- 若去掉 ``clear`` 就不崩 → 只需在预处理里禁 float 上的 clear
- 若去掉小数高度就不崩 → 与 f32 分段边界精度有关
- 若两者都去掉仍崩 → float 本身就得禁

贪心删减：逐个尝试删除/简化，仍能复现就保留该简化。
每步都在独立子进程里渲染（panic 会 abort 进程）。

用法::

    uv run python eval/manual/taffy_float_panic_minimize.py
"""

from __future__ import annotations

import sys
import subprocess

DOC = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"><style>\n'
    "*{{margin:0;padding:0;box-sizing:border-box}}\n"
    "body{{background:#fff;width:800px}}\n"
    '</style></head><body>\n<div style="width:600px">\n{body}\n</div>\n</body></html>'
)

FLOATS = [
    '<div style="float:left;width:120px;height:66.67px;margin-top:75px;clear:right;background:#345"></div>',
    '<div style="float:right;width:100px;height:100px;margin-top:20px;clear:right;background:#345"></div>',
    '<div style="float:left;width:100px;height:133.33px;margin-top:150px;clear:right;background:#345"></div>',
    '<div style="float:left;width:80px;height:100px;margin-top:22.5px;clear:both;background:#345"></div>',
    '<div style="float:right;width:150px;height:80px;margin-top:40px;clear:none;background:#345"></div>',
]
PARAS = [
    '<p style="font-size:15px;line-height:1.5">第0段文字环绕 content ' + "填充" * 40 + "</p>",
    '<p style="font-size:15px;line-height:1.5">第1段文字环绕 content ' + "填充" * 30 + "</p>",
]

# 简化变换：label -> (needle, replacement)
SIMPLIFY: list[tuple[str, str, str]] = [
    (
        "去掉第1个float的clear",
        "float:left;width:120px;height:66.67px;margin-top:75px;clear:right",
        "float:left;width:120px;height:66.67px;margin-top:75px",
    ),
    ("clear:right→none(全部)", "clear:right", "clear:none"),
    ("去掉全部clear", ";clear:both", ""),
    ("去掉clear:both", ";clear:both", ""),
    ("高度取整(全部)", ".67px", "px"),
    ("高度取整(全部)", ".5px", "px"),
    ("去掉第4个float的margin-top", "width:80px;height:100px;margin-top:22.5px", "width:80px;height:100px"),
]


def render_crash(body: str) -> tuple[bool, str]:
    """在子进程渲染；返回 (是否 panic 崩溃, stderr 摘要)。"""
    code = (
        "import pytakumi,sys\n"
        f"html={DOC.format(body=body)!r}\n"
        "pytakumi.html_to_pic(html,width=800,height=None,format='png',"
        "renderer=pytakumi.Renderer())\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    first_line = proc.stderr.strip().splitlines()
    return "panicked" in proc.stderr, first_line[0] if first_line else ""


def crashes(elements: list[str]) -> bool:
    hit, _ = render_crash("\n".join(elements))
    return hit


def main() -> int:
    elements = FLOATS + PARAS
    hit, first = render_crash("\n".join(elements))
    print(f"起点：{len(elements)} 个元素，崩溃={hit}  {first}")
    if not hit:
        print("起点就不崩，无法最小化")
        return 1

    # 贪心删元素
    changed = True
    while changed:
        changed = False
        for i in range(len(elements)):
            trial = elements[:i] + elements[i + 1 :]
            if trial and crashes(trial):
                print(f"  - 删除元素[{i}] 后仍崩 → 保留删除")
                elements = trial
                changed = True
                break

    # 贪心简化样式
    for label, needle, repl in SIMPLIFY:
        body = "\n".join(elements)
        if needle not in body:
            continue
        trial = [e.replace(needle, repl) for e in elements]
        if crashes(trial):
            print(f"  ✓ {label} 后仍崩 → 保留简化")
            elements = trial
        else:
            print(f"  ✗ {label} 后不崩 → 该条件是必要的")

    print("\n" + "=" * 70)
    print("最小化结果（%d 个元素）:" % len(elements))
    print("=" * 70)
    print(DOC.format(body="\n".join(elements)))
    print("=" * 70)
    final = crashes(elements)
    print(f"复核：最小用例仍然崩溃 = {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
