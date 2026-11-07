"""存量别名污染与段子实体清洗脚本（Module A / §2.3）。

功能：
1. 扫描 group_profile term_mappings 中来自插件别名且与群语境不匹配的条目；
2. 扫描 AIMemEntity.summary 中拼接的（别名'...'：...）片段并剥离；
3. 识别段子孤儿实体（非 speaker、仅 1 条边、fact 含"叙述中"、last_accessed is null）；
4. 通过 consolidation_worker._purge_entities 物理清理实体（同步 Qdrant 与分层图）。

默认 --dry-run，加 --apply 真正执行。
"""

import re
import csv
import asyncio
import argparse
from typing import List, Tuple

from sqlmodel import col, select

from gsuid_core.logger import logger
from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.state_store import state_mutate, state_list_keys
from gsuid_core.ai_core.memory.group_profile import get_group_profile
from gsuid_core.ai_core.memory.database.models import AIMemEdge, AIMemEntity
from gsuid_core.ai_core.memory.lifecycle.consolidation_worker import _purge_entities

_ALIAS_FRAGMENT_RE = re.compile(r"\n?（别名'[^']*'：.*?）", re.DOTALL)


async def scan_and_clean_term_mappings(apply: bool) -> List[dict[str, str]]:
    """扫描并清理跨群套用的插件别名映射。"""
    from gsuid_core.ai_core.register import get_aliases_for_scope

    records: List[dict[str, str]] = []
    registered_aliases = get_aliases_for_scope()
    known_formals = {f for formals in registered_aliases.values() for f in formals}

    scope_keys = await state_list_keys("__gscore_group_profile__")
    for sk in scope_keys:
        profile = await get_group_profile(sk)
        term_map = profile.get("term_mappings", {})
        dirty_keys: list[str] = []
        for a, f in term_map.items():
            if f in known_formals or a in registered_aliases:
                dirty_keys.append(a)
                records.append(
                    {
                        "type": "term_mapping",
                        "scope_key": sk,
                        "key": a,
                        "value": f,
                        "action": "delete" if apply else "dry_run_delete",
                    }
                )
        if apply and dirty_keys:

            def _clean_mutate(current: dict) -> dict:
                m = dict(current.get("term_mappings", {}))
                for k in dirty_keys:
                    m.pop(k, None)
                current["term_mappings"] = m
                return current

            await state_mutate("__gscore_group_profile__", sk, _clean_mutate)

    return records


async def scan_and_clean_entity_summaries(apply: bool) -> List[dict[str, str]]:
    """扫描并剥离 entity summary 中污染的（别名'...'：...）片段。"""
    records: List[dict[str, str]] = []
    async with base_models.async_maker() as session:
        stmt = select(AIMemEntity).where(col(AIMemEntity.summary).contains("（别名'"))
        res = await session.execute(stmt)
        entities = list(res.scalars().all())

        for ent in entities:
            old_summary = ent.summary
            new_summary = _ALIAS_FRAGMENT_RE.sub("", old_summary).strip()
            records.append(
                {
                    "type": "entity_summary",
                    "scope_key": ent.scope_key,
                    "key": ent.name,
                    "value": old_summary[:60],
                    "action": f"strip -> {new_summary[:40]}" if apply else "dry_run_strip",
                }
            )
            if apply and new_summary != old_summary:
                ent.summary = new_summary
                session.add(ent)
        if apply and entities:
            await session.commit()

    return records


async def scan_and_purge_joke_entities(apply: bool) -> List[dict[str, str]]:
    """按规则识别段子实体（非 speaker、仅1边、含'叙述中'、从未被检索）。"""
    records: List[dict[str, str]] = []
    victims_to_purge: List[Tuple[str, str, str]] = []

    async with base_models.async_maker() as session:
        # 查询非 speaker 且包含'叙述中'事实的边
        stmt = (
            select(AIMemEntity, AIMemEdge)
            .join(AIMemEdge, col(AIMemEntity.id) == col(AIMemEdge.source_entity_id))
            .where(
                col(AIMemEntity.is_speaker).is_(False),
                col(AIMemEdge.fact).contains("叙述中"),
                col(AIMemEdge.last_accessed).is_(None),
            )
        )
        res = await session.execute(stmt)
        candidates = res.all()

        for ent, edge in candidates:
            # 确认该实体只有 1 条边
            edge_count_stmt = select(col(AIMemEdge.id)).where(
                (col(AIMemEdge.source_entity_id) == ent.id) | (col(AIMemEdge.target_entity_id) == ent.id)
            )
            count_res = await session.execute(edge_count_stmt)
            if len(count_res.all()) <= 1:
                victims_to_purge.append((ent.id, ent.scope_key, ent.qdrant_id))
                records.append(
                    {
                        "type": "joke_entity",
                        "scope_key": ent.scope_key,
                        "key": ent.name,
                        "value": edge.fact[:60],
                        "action": "purge_entity" if apply else "dry_run_purge",
                    }
                )

    if apply and victims_to_purge:
        await _purge_entities(victims_to_purge)

    return records


def _write_csv(csv_path: str, records: List[dict[str, str]]) -> None:
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["type", "scope_key", "key", "value", "action"])
        writer.writeheader()
        writer.writerows(records)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup alias pollution and joke entities")
    parser.add_argument("--apply", action="store_true", help="Apply cleanup actions (default is dry-run)")
    parser.add_argument("--csv", default="eval/manual/alias_cleanup_preview.csv", help="Output preview CSV path")
    args = parser.parse_args()

    apply = bool(args.apply)
    await base_models.init_database()
    logger.info(f"Starting alias pollution cleanup (apply={apply})...")

    all_records: List[dict[str, str]] = []
    all_records.extend(await scan_and_clean_term_mappings(apply))
    all_records.extend(await scan_and_clean_entity_summaries(apply))
    all_records.extend(await scan_and_purge_joke_entities(apply))

    logger.info(f"Scan finished. Total affected records: {len(all_records)}")

    if args.csv and all_records:
        await asyncio.to_thread(_write_csv, args.csv, all_records)
        logger.info(f"Preview saved to {args.csv}")


if __name__ == "__main__":
    asyncio.run(main())
