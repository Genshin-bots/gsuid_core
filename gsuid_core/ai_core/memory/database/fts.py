"""SQLite FTS5 词面索引（``aimemepisode_fts``）。

只用于 Episode 快速词面召回；非 SQLite 方言退化为参数化 LIKE 查询。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.base_models import _db_type

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

_FTS_TABLE = "aimemepisode_fts"
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LEX_TOKEN_RE = re.compile(r"[A-Za-z0-9_+#.-]{2,}")
_FTS_READY: bool | None = None


def fts_supported() -> bool:
    return _db_type == "sqlite"


def _index_text(content: str) -> str:
    """正文 + CJK 单字/二元语法，让 unicode61 分词器也能命中中文。"""
    body = content or ""
    grams: list[str] = []
    for match in _CJK_RUN_RE.finditer(body):
        run = match.group(0)
        grams.extend(run)
        grams.extend(run[i : i + 2] for i in range(len(run) - 1))
    if not grams:
        return body
    return body + "\n" + " ".join(grams)


async def ensure_fts_table(session: AsyncSession) -> None:
    global _FTS_READY
    if not fts_supported():
        _FTS_READY = False
        return
    try:
        await session.execute(
            sql_text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} "
                "USING fts5(episode_id UNINDEXED, scope_key UNINDEXED, content, tokenize='unicode61')"
            )
        )
        _FTS_READY = True
    except Exception:
        # 编译期缺少 FTS5 时退回 LIKE，不影响 Episode 落库。
        _FTS_READY = False


async def index_episode(session: AsyncSession, episode: "AIMemEpisode") -> None:
    if not fts_supported():
        return
    if _FTS_READY is None:
        await ensure_fts_table(session)
    if not _FTS_READY:
        return
    await session.execute(
        sql_text(f"DELETE FROM {_FTS_TABLE} WHERE episode_id = :eid"),
        {"eid": episode.id},
    )
    await session.execute(
        sql_text(f"INSERT INTO {_FTS_TABLE}(episode_id, scope_key, content) VALUES (:eid, :sk, :content)"),
        {"eid": episode.id, "sk": episode.scope_key, "content": _index_text(episode.content)},
    )


async def index_episodes(session: AsyncSession, episodes: "list[AIMemEpisode]") -> None:
    """批量 FTS 写入：executemany 分块，避免 2000 条 Episode 触发写闸超时。"""
    if not fts_supported() or not episodes:
        return
    if _FTS_READY is None:
        await ensure_fts_table(session)
    if not _FTS_READY:
        return
    stmt = sql_text(f"INSERT INTO {_FTS_TABLE}(episode_id, scope_key, content) VALUES (:eid, :sk, :content)")
    params = [
        {"eid": episode.id, "sk": episode.scope_key, "content": _index_text(episode.content or "")}
        for episode in episodes
    ]
    for start in range(0, len(params), 100):
        await session.execute(stmt, params[start : start + 100])


async def delete_episode_ids(session: AsyncSession, episode_ids: list[str]) -> None:
    if not fts_supported() or not episode_ids:
        return
    if _FTS_READY is not True:
        return
    for start in range(0, len(episode_ids), 400):
        chunk = episode_ids[start : start + 400]
        names = ", ".join(f":id{i}" for i in range(len(chunk)))
        params = {f"id{i}": eid for i, eid in enumerate(chunk)}
        await session.execute(sql_text(f"DELETE FROM {_FTS_TABLE} WHERE episode_id IN ({names})"), params)


def _fts_query(tokens: list[str]) -> str:
    # 词面加前缀通配支持单复数与屈折变化，短语保持完全匹配
    safe: list[str] = []
    for tok in tokens:
        t = tok.replace(chr(34), "").strip()
        if not t:
            continue
        if " " in t:
            safe.append(f'"{t}"')
        elif t.isascii() and t.isalpha() and len(t) >= 3:
            safe.append(f'"{t}"')
            low = t.lower()
            if low.endswith("ies") and len(low) > 4:
                safe.append(f'"{t[:-3]}y"*')
            elif low.endswith("es") and len(low) > 4:
                safe.append(f'"{t[:-2]}"*')
            elif low.endswith("s") and not low.endswith("ss") and len(low) > 3:
                safe.append(f'"{t[:-1]}"*')
            elif low.endswith("ing") and len(low) > 5:
                safe.append(f'"{t[:-3]}"*')
            elif low.endswith("ed") and len(low) > 4:
                safe.append(f'"{t[:-2]}"*')
            else:
                safe.append(f'"{t}"*')
        else:
            safe.append(f'"{t}"')
    seen: set[str] = set()
    uniq: list[str] = []
    for s in safe:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return " OR ".join(uniq)


async def match_episode_ids(scope_keys: list[str], tokens: list[str], limit: int) -> list[str]:
    """FTS5 MATCH（SQLite）/ LIKE 回退（其它方言）。"""
    from gsuid_core.utils.database.base_models import async_maker

    if not scope_keys or not tokens or limit <= 0:
        return []
    if not fts_supported() or _FTS_READY is False:
        return await _like_episode_ids(scope_keys, tokens, limit)
    if _FTS_READY is None:
        async with async_maker() as session:
            await ensure_fts_table(session)
            await session.commit()
        if not _FTS_READY:
            return await _like_episode_ids(scope_keys, tokens, limit)

    q = _fts_query(tokens)
    if not q:
        return []
    for start in range(0, len(scope_keys), 400):
        chunk = scope_keys[start : start + 400]
        scopes = ", ".join(f":sk{i}" for i in range(len(chunk)))
        params: dict[str, str | int] = {f"sk{i}": sk for i, sk in enumerate(chunk)}
        params["q"] = q
        params["lim"] = limit
        stmt = sql_text(
            f"SELECT episode_id FROM {_FTS_TABLE} WHERE scope_key IN ({scopes}) "
            f"AND {_FTS_TABLE} MATCH :q ORDER BY bm25({_FTS_TABLE}) LIMIT :lim"
        )
        try:
            async with async_maker() as session:
                rows = (await session.execute(stmt, params)).all()
            found = [str(row[0]) for row in rows if row[0]]
            if found:
                return found
        except Exception:
            # FTS 虚表缺失/损坏时退回 LIKE，绝不打断检索主链路。
            break
    return await _like_episode_ids(scope_keys, tokens, limit)


async def _like_episode_ids(scope_keys: list[str], tokens: list[str], limit: int) -> list[str]:
    from sqlmodel import col, select

    from gsuid_core.utils.database.base_models import async_maker
    from gsuid_core.ai_core.memory.database.models import AIMemEpisode

    # PostgreSQL / MySQL 与 FTS 缺表时共用参数化 LIKE 回退。
    stmt = select(col(AIMemEpisode.id)).where(col(AIMemEpisode.scope_key).in_(scope_keys))
    for token in tokens[:6]:
        stmt = stmt.where(col(AIMemEpisode.content).contains(token))
    stmt = stmt.limit(limit)
    async with async_maker() as session:
        result = await session.execute(stmt)
        return [str(row[0]) for row in result.all() if row[0]]
