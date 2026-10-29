"""表情包绑定：归包规则、空包不检索、路由登记顺序。"""

import asyncio
from unittest.mock import patch

from gsuid_core.ai_core.meme.library import MemeLibrary
from gsuid_core.ai_core.meme.pack_model import (
    DEFAULT_PACK_ID,
    TagMembershipInput,
    pack_id_for_hint,
    pack_name_for_hint,
    next_assign_default,
    decide_tag_membership,
)


def test_hint_pack_id_is_stable() -> None:
    assert pack_id_for_hint("摆烂") == pack_id_for_hint("摆烂")
    assert pack_id_for_hint("摆烂") != pack_id_for_hint("搞怪")


def test_pack_name_for_long_hint_and_reserved_name() -> None:
    assert pack_name_for_hint("通用表情包", "abc", {}) == "通用表情包·标签"
    long_hint = "名" * 80
    name = pack_name_for_hint(long_hint, "0123456789abcdef", {})
    assert len(name) <= 64
    taken = {name: "other"}
    fallback = pack_name_for_hint(long_hint, "0123456789abcdef", taken)
    assert fallback.endswith("01234567")


def test_unspecified_first_success_joins_default_pack() -> None:
    action = decide_tag_membership(
        TagMembershipInput(
            pending_pack_id="",
            pending_pack_exists=False,
            has_members=False,
            assign_default_pack=True,
        )
    )
    assert action.join_pack_id == DEFAULT_PACK_ID
    assert action.assign_default_pack is False


def test_removed_from_all_packs_stays_memberless() -> None:
    action = decide_tag_membership(
        TagMembershipInput(
            pending_pack_id="",
            pending_pack_exists=False,
            has_members=False,
            assign_default_pack=False,
        )
    )
    assert action.join_pack_id == ""


def test_specified_pack_does_not_also_join_default() -> None:
    action = decide_tag_membership(
        TagMembershipInput(
            pending_pack_id="pack-1",
            pending_pack_exists=True,
            has_members=False,
            assign_default_pack=False,
        )
    )
    assert action.join_pack_id == "pack-1"
    assert action.clear_pending is True


def test_deleted_pending_pack_does_not_join_default_when_flag_is_false() -> None:
    action = decide_tag_membership(
        TagMembershipInput(
            pending_pack_id="gone",
            pending_pack_exists=False,
            has_members=False,
            assign_default_pack=False,
        )
    )
    assert action.join_pack_id == ""
    assert action.clear_pending is True


def test_retag_keeps_flag_for_never_assigned_manual_row() -> None:
    assert next_assign_default("manual", False, True, "") is True
    assert next_assign_default("tagged", True, True, "") is False
    assert next_assign_default("pending", False, False, "") is False


def test_meme_protocol_uses_the_same_word_list() -> None:
    from gsuid_core.ai_core.prompt_group.catalog import default_body

    body = default_body("meme_protocol")
    assert "`<meme:情绪>`" in body
    assert "{{emotion_words}}" in body
    assert "{{scene_words}}" in body
    assert "开心" not in body
    assert "卖萌" not in body


def test_send_and_tag_share_one_word_list() -> None:
    from gsuid_core.ai_core.meme.vocab import SCENE_WORDS, EMOTION_WORDS, join_words
    from gsuid_core.ai_core.meme.tagger import TAG_PROMPT
    from gsuid_core.ai_core.prompt_group.render import render_text
    from gsuid_core.ai_core.prompt_group.catalog import default_body

    rendered = render_text(
        default_body("meme_protocol"),
        {
            "emotion_words": join_words(EMOTION_WORDS),
            "scene_words": join_words(SCENE_WORDS),
            "meme_frequency": "频率：闲聊时大约每五到八次回复用一次。",
        },
    )
    for word in EMOTION_WORDS + SCENE_WORDS:
        assert word in TAG_PROMPT
        assert word in rendered


def test_empty_pack_ids_do_not_embed() -> None:
    async def _boom(_text: str) -> None:
        raise AssertionError("should not embed")

    async def _run() -> None:
        with patch("gsuid_core.ai_core.meme.library._embed_text", _boom):
            rows = await MemeLibrary.search_by_text("开心", pack_ids=[])
            assert rows == []

    asyncio.run(_run())


def test_folder_and_pack_ids_together_return_empty() -> None:
    async def _run() -> None:
        rows = await MemeLibrary.search_by_text("开心", folder="common", pack_ids=["p"])
        assert rows == []

    asyncio.run(_run())


def test_pack_routes_register_before_meme_detail() -> None:
    import gsuid_core.webconsole.meme_api  # noqa: F401
    import gsuid_core.webconsole.persona_api  # noqa: F401
    from gsuid_core.webconsole.app_app import app

    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths.index("/api/meme/packs") < paths.index("/api/meme/{meme_id}")
    assert "/api/persona/{persona_name}/meme_packs" in paths
    assert "/api/persona/bundle/import" in paths
