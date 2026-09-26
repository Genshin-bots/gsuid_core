"""插件热度：关键字归并只算一次，uuid 伪命令不进排名。"""

from gsuid_core.webconsole.plugin_usage import (
    rank_plugins,
    merge_plugin_counts,
    fold_keywords_to_plugins,
)


def test_keyword_counts_fold_onto_one_plugin():
    mapping = {"签到": "GenshinUID", "帮助": "core_command"}
    totals = fold_keywords_to_plugins({"签到": 10, "帮助": 3, "未知": 100}, mapping)
    assert totals == {"GenshinUID": 10, "core_command": 3}


def test_uuid_keywords_are_ignored():
    mapping = {"11111111-1111-4111-8111-111111111111": "core_command", "帮助": "core_command"}
    totals = fold_keywords_to_plugins(
        {"11111111-1111-4111-8111-111111111111": 99999, "帮助": 2},
        mapping,
    )
    assert totals == {"core_command": 2}


def test_same_keyword_is_not_double_counted_when_map_has_one_owner():
    mapping = {"签到": "GenshinUID"}
    history = fold_keywords_to_plugins({"签到": 4}, mapping)
    today = fold_keywords_to_plugins({"签到": 6}, mapping)
    assert merge_plugin_counts(history, today)["GenshinUID"] == 10


def test_rank_orders_by_triggers_then_name():
    ranked = rank_plugins({"b": 5, "a": 5, "c": 1, "d": 0})
    assert [item["name"] for item in ranked] == ["a", "b", "c"]
    assert ranked[0]["triggers"] == 5
