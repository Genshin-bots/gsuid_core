"""评测用户 id 的下划线在 SQLite LIKE 里必须是字面量。"""

import sqlite3

from eval.agent.reset_state import _EVAL_USER_LIKE, _EVAL_LIKE_ESCAPE


def test_eval_user_like_matches_only_the_literal_prefix() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("create table t (user_id text)")
    conn.executemany(
        "insert into t values (?)",
        [("eval_1",), ("evaluation",), ("eval-x",), ("other",)],
    )
    rows = conn.execute(
        "select user_id from t where user_id like ? escape ? order by user_id",
        (_EVAL_USER_LIKE, _EVAL_LIKE_ESCAPE),
    ).fetchall()
    assert [row[0] for row in rows] == ["eval_1"]
