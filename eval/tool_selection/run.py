"""工具选择评测：量化「正确插件的工具有没有进本轮工具池」（Pool Recall）。

用例由 `ai_core/entity_index.py` 的实体身份索引**自动生成**（零人工标注）：
插件注册的每个实体都带确定的插件归属，× 句式模板即得 ground truth。

**必须打真实运行中的 core**（`POST /api/ai/tools/assemble_preview`）：工具注册表只有
在完整启动序之后才是全的——单独 `load_plugins()` 拿到的是残缺注册表（XW 的 AI-RAG
走的是启动钩子，不是 import 期），在那上面测出来的数字全是假的。

前置：core 以 `GSUID_LOCAL_TEST_MODE=1` 启动，且 Qdrant 在跑。
详见同目录 README.md。
"""

from __future__ import annotations

import os
import sys
import json
import time
import asyncio
import argparse
from typing import Dict, List, Optional
from pathlib import Path
from collections import Counter
from dataclasses import field, asdict, dataclass

import httpx

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_RESULTS = Path(__file__).parent / "results"
_ENDPOINT = "/api/ai/tools/assemble_preview"

# 句式模板：覆盖「查面板 / 问练度 / 要攻略 / 泛问」四类真实提问。
# {X} = 插件注册的实体名；期望 = 该实体所属插件的工具进池。
TEMPLATES: Dict[str, str] = {
    "panel": "帮我看看{X}的面板",
    "panel_bare": "{X}面板",
    "level": "{X}练度怎么样",
    "guide": "{X}怎么养比较好",
    "lookup": "查一下{X}的资料",
}


@dataclass
class Case:
    query: str
    surface: str
    expected_plugin: str
    template_id: str


@dataclass
class Outcome:
    case: Case
    pool_recall: bool
    top_seed_plugin: str
    pool_plugins: List[str] = field(default_factory=list)
    pool_size: int = 0


async def build_cases(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    per_plugin: int,
    only_plugin: Optional[str],
) -> List[Case]:
    """从**运行中 core** 的实体身份索引交叉生成用例；每插件至多 `per_plugin` 个（0=全部）。

    ground truth 必须取自 core：插件的实体注册有的在 import 期（`ai_alias`）、有的在
    启动钩子（XW 的 AI-RAG）。评测进程自己 `load_plugins()` 只能拿到前者，
    生成出来的用例会**整整漏掉一半插件**（问过一次这个坑）。
    """
    headers = {"X-Local-Test-Token": token} if token else {}
    resp = await client.get(f"{base}/api/ai/entity_index", headers=headers)
    if resp.status_code == 404:
        raise RuntimeError("entity_index 端点 404 —— core 未开 local-test 模式或 token 不对")
    resp.raise_for_status()

    by_plugin: Dict[str, List[str]] = {}
    for entry in resp.json()["data"]["entries"]:
        # 归属歧义 → ground truth 不成立，直接排除（框架本来也不会据此路由）
        if entry["ambiguous"]:
            continue
        plugin = entry["plugins"][0]
        if only_plugin and plugin != only_plugin:
            continue
        by_plugin.setdefault(plugin, []).append(entry["surface"])

    cases: List[Case] = []
    for plugin, surfaces in by_plugin.items():
        # 长 surface 更具辨识度，优先取，避免评测被 3 字母缩写主导
        picked = sorted(surfaces, key=len, reverse=True)
        if per_plugin > 0:
            picked = picked[:per_plugin]
        for surface in picked:
            for tid, tpl in TEMPLATES.items():
                cases.append(Case(tpl.format(X=surface), surface, plugin, tid))
    return cases


async def _preview(client: httpx.AsyncClient, base: str, token: str, query: str) -> Dict:
    headers = {"X-Local-Test-Token": token} if token else {}
    resp = await client.post(f"{base}{_ENDPOINT}", json={"query": query}, headers=headers)
    if resp.status_code == 404:
        raise RuntimeError(
            f"{_ENDPOINT} 返回 404 —— core 没开 local-test 模式。请用 GSUID_LOCAL_TEST_MODE=1 重启 core。"
        )
    resp.raise_for_status()
    return resp.json()["data"]


async def run(per_plugin: int, only_plugin: Optional[str], base: str, concurrency: int) -> None:
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    token = os.environ["GSUID_LOCAL_TEST_TOKEN"] if "GSUID_LOCAL_TEST_TOKEN" in os.environ else ""

    outcomes: List[Outcome] = []
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=90.0) as client:
        cases = await build_cases(client, base, token, per_plugin, only_plugin)
        if not cases:
            print("❌ 没有生成用例——core 的实体索引为空（插件是否注册了 ai_alias？）")
            return
        plugins = sorted({c.expected_plugin for c in cases})
        print(f"用例 {len(cases)} 条 | 覆盖插件: {plugins}")
        print(f"目标 core: {base}\n")
        t0 = time.time()

        async def one(case: Case) -> None:
            async with sem:
                data = await _preview(client, base, token, case.query)
            pool_plugins = [t["plugin"] for t in data["pool"]]
            seeds = data["seeds"]
            outcomes.append(
                Outcome(
                    case=case,
                    pool_recall=case.expected_plugin in pool_plugins,
                    top_seed_plugin=seeds[0]["plugin"] if seeds else "none",
                    pool_plugins=sorted(set(pool_plugins)),
                    pool_size=len(data["pool"]),
                )
            )

        await asyncio.gather(*(one(c) for c in cases))

    _report(outcomes, time.time() - t0)


def _report(outcomes: List[Outcome], elapsed: float) -> None:
    total = len(outcomes)
    recall_hits = sum(1 for o in outcomes if o.pool_recall)
    top_hits = sum(1 for o in outcomes if o.top_seed_plugin == o.case.expected_plugin)
    empty_pools = sum(1 for o in outcomes if o.pool_size == 0)

    print("=" * 68)
    print(f"总体   Pool Recall : {recall_hits}/{total} = {recall_hits / total:.1%}")
    print(f"       Top-Seed 正确: {top_hits}/{total} = {top_hits / total:.1%}")
    print(f"       空工具池     : {empty_pools}/{total}")
    print(f"       耗时 {elapsed:.1f}s ({elapsed / total * 1000:.0f} ms/例)")

    print("\n--- 按插件 ---")
    for plugin in sorted({o.case.expected_plugin for o in outcomes}):
        sub = [o for o in outcomes if o.case.expected_plugin == plugin]
        hit = sum(1 for o in sub if o.pool_recall)
        top = sum(1 for o in sub if o.top_seed_plugin == plugin)
        print(f"  {plugin:24s} Recall {hit:4d}/{len(sub):<4d} = {hit / len(sub):6.1%}   Top-Seed {top / len(sub):6.1%}")

    print("\n--- 按句式 ---")
    for tid, tpl in TEMPLATES.items():
        sub = [o for o in outcomes if o.case.template_id == tid]
        if not sub:
            continue
        hit = sum(1 for o in sub if o.pool_recall)
        print(f"  {tid:12s} {tpl:16s} Recall {hit / len(sub):6.1%}")

    fails = [o for o in outcomes if not o.pool_recall]
    if fails:
        print(f"\n--- 失败用例里，工具池被谁占了（共 {len(fails)} 条）---")
        thief: Counter = Counter()
        for o in fails:
            for p in o.pool_plugins:
                if p != o.case.expected_plugin:
                    thief[p] += 1
        for p, n in thief.most_common(8):
            print(f"  {p:24s} 出现在 {n} 条失败用例的池里")
        print("\n  失败样例:")
        for o in fails[:6]:
            print(f"    {o.case.query!r}")
            print(f"        期望 {o.case.expected_plugin} | 实际池: {o.pool_plugins}")

    _RESULTS.mkdir(parents=True, exist_ok=True)
    out = _RESULTS / f"tool_selection_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {
                "total": total,
                "pool_recall": recall_hits / total,
                "top_seed_acc": top_hits / total,
                "outcomes": [asdict(o) for o in outcomes],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已写入 {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="工具选择评测（Pool Recall，零 LLM）")
    parser.add_argument("--per-plugin", type=int, default=40, help="每插件取多少实体（0=全部）")
    parser.add_argument("--plugin", type=str, default=None, help="只测某个插件")
    parser.add_argument("--base", type=str, default="http://127.0.0.1:8765", help="core 地址")
    parser.add_argument("--concurrency", type=int, default=4, help="并发数（嵌入是单线程，别开太大）")
    args = parser.parse_args()
    asyncio.run(run(args.per_plugin, args.plugin, args.base, args.concurrency))


if __name__ == "__main__":
    main()
