"""CLI shim：``python eval/BEAM_official/run_beam_eval.py``。

实现在 ``eval.common.beam_runner``；官方 ladder 用 ``run_official.py``。
"""

from __future__ import annotations

from eval.common.beam_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
