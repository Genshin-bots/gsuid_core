"""把撞点的 Episode.valid_at 按 rowid 摊成 +1s。不清库、不重灌。

SQL valid_at 是窗口召回真源。向量 valid_at_ts 仍按日历日分桶，同日 +1s 不换日；
若要向量日内序与 SQL 一致，请重灌。

  uv run python eval/BEAM_10M/migrate_episode_times.py
  uv run python eval/BEAM_10M/migrate_episode_times.py --dry-run
  uv run python eval/BEAM_10M/migrate_episode_times.py --db path/to/GsData.db
"""

from __future__ import annotations

import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.memory.ingest_time import spread_datetimes


def _parse(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    s = str(raw).replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def migrate_scope(con: sqlite3.Connection, scope_key: str, *, dry_run: bool) -> int:
    cur = con.cursor()
    cur.execute(
        "SELECT rowid, id, valid_at FROM aimemepisode WHERE scope_key=? ORDER BY rowid",
        (scope_key,),
    )
    rows = cur.fetchall()
    old = [_parse(r[2]) for r in rows]
    new = spread_datetimes(old)
    n = 0
    for (rowid, _eid, raw), ts in zip(rows, new):
        if ts is None:
            continue
        prev = _parse(raw)
        if prev == ts:
            continue
        n += 1
        if dry_run:
            continue
        cur.execute(
            "UPDATE aimemepisode SET valid_at=? WHERE rowid=?",
            (ts.strftime("%Y-%m-%d %H:%M:%S.%f"), rowid),
        )
    if not dry_run:
        con.commit()
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Spread colliding Episode.valid_at by rowid.")
    parser.add_argument("--db", type=Path, default=None, help="GsData.db path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scopes", nargs="*", default=None, help="scope_key list")
    args = parser.parse_args()
    db = args.db if args.db is not None else get_res_path() / "GsData.db"
    if not db.is_file():
        raise SystemExit(f"db not found: {db}")
    scopes = args.scopes if args.scopes else [f"user_global:beam_eval_{i}" for i in range(10)]
    con = sqlite3.connect(str(db), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    total = 0
    for scope in scopes:
        n = migrate_scope(con, scope, dry_run=args.dry_run)
        print(f"{scope} updated={n} dry_run={args.dry_run}")
        total += n
    print(f"total {total}")
    con.close()


if __name__ == "__main__":
    main()
