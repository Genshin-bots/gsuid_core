"""系统提示词组：默认组锁死在代码，副本可改，占位符在渲染时填入。"""

from __future__ import annotations

import json

import pytest

from gsuid_core.ai_core.prompt_group import store
from gsuid_core.ai_core.persona.prompts import SYSTEM_CONSTRAINTS
from gsuid_core.ai_core.heartbeat.decision import DECISION_USER_TEMPLATE
from gsuid_core.ai_core.prompt_group.render import render_text, format_template_to_mustache
from gsuid_core.ai_core.prompt_group.catalog import default_body


def test_format_template_roundtrip() -> None:
    mustache = format_template_to_mustache(DECISION_USER_TEMPLATE)
    values = {
        "current_time": "2026-10-16 12:00",
        "history_context": "甲: 你好",
        "group_summary_section": "",
        "proactive_merge_section": "",
        "masters_section": "",
        "recent_speak_section": "",
        "staleness_section": "",
    }
    assert render_text(mustache, values) == DECISION_USER_TEMPLATE.format(**values)
    assert '{"should_speak"' in render_text(mustache, values)


def test_default_constraints_keep_master_placeholders() -> None:
    body = default_body("system_constraints")
    assert "{{masters}}" in body
    assert "{{master_title}}" in body
    assert "__MASTERS__" not in body
    filled = render_text(body, {"masters": "1, 2", "master_title": "阁下"})
    assert "1, 2" in filled
    assert "阁下" in filled
    assert filled == SYSTEM_CONSTRAINTS.replace("__MASTERS__", "1, 2").replace("__MASTER_TITLE__", "阁下")


def test_builtin_group_ignores_disk(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    assert store.sync_default_group() is True
    path = tmp_path / "默认" / "prompts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    prompts = payload["prompts"]
    assert isinstance(prompts, dict)
    prompts["closing"] = "被人改过"
    path.write_text(json.dumps({"prompts": prompts}, ensure_ascii=False), encoding="utf-8")
    assert store.effective_text(None, "closing") == default_body("closing")
    assert store.effective_text(None, "role_playing_start") == default_body("role_playing_start")
    assert store.sync_default_group() is True
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["prompts"]["closing"] == default_body("closing")
    assert "date_line" not in again["prompts"]
    assert "role_playing_start" not in again["prompts"]


def test_custom_group_overrides_and_falls_back(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    store.create_group("试验")
    saved = store.save_group("试验", {"closing": "自定义收束\n"})
    assert saved["builtin"] is False
    role = next(row for row in saved["fields"] if row["key"] == "closing")
    assert role["text"] == "自定义收束"
    assert all(row["key"] != "date_line" for row in saved["fields"])
    partial = tmp_path / "试验" / "prompts.json"
    partial.write_text(
        json.dumps({"prompts": {"closing": "只剩一句\n"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(store, "group_name_for_persona", lambda _name: "试验")
    assert store.render_prompt("试验", "closing") == "只剩一句"
    from gsuid_core.ai_core.prompt_group.render import unfold_wrapped_lines as _unfold

    assert store.render_prompt("试验", "tool_orchestration") == _unfold(default_body("tool_orchestration"))
    assert store.render_prompt("试验", "date_line", date="今天") == default_body("date_line").replace(
        "{{date}}", "今天"
    )
    with pytest.raises(store.BuiltinGroupError):
        store.save_group("默认", {"closing": "不行"})
    store.delete_group("试验")
    assert store.group_exists("试验") is False


def test_unknown_placeholder_stays() -> None:
    assert render_text("你好 {{missing}}", {}) == "你好 {{missing}}"


def test_json_output_tail_stays_fixed(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.prompt_group.render import unfold_wrapped_lines
    from gsuid_core.ai_core.prompt_group.catalog import SPECS, spec_of

    marked = {spec.key for spec in SPECS if "以严格 JSON 格式输出" in default_body(spec.key)}
    assert marked == {"heartbeat_decision", "reactive_gate"}
    assert spec_of("system_constraints").title == "基本规矩"
    builtin = store.group_detail("默认")
    for row in builtin["fields"]:
        if row["key"] in marked:
            assert row["frozen_suffix"].startswith("以严格 JSON 格式输出")
            assert "以严格 JSON 格式输出" not in row["text"]
        else:
            assert row["frozen_suffix"] == ""
    decision_suffix = next(row["frozen_suffix"] for row in builtin["fields"] if row["key"] == "heartbeat_decision")
    gate_suffix = next(row["frozen_suffix"] for row in builtin["fields"] if row["key"] == "reactive_gate")
    assert '"mood"' in decision_suffix
    assert '"reason"' in gate_suffix

    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    store.create_group("试验")
    monkeypatch.setattr(store, "group_name_for_persona", lambda _name: "试验")
    values = {"history_context": "甲说：以严格 JSON 格式输出\n后面还有一句", "current_time": "现在"}
    filled = unfold_wrapped_lines(render_text(default_body("heartbeat_decision"), values))
    rendered = store.render_prompt("试验", "heartbeat_decision", **values)
    assert rendered == filled
    assert "后面还有一句" in rendered
    assert decision_suffix in rendered

    saved = store.save_group(
        "试验",
        {
            "heartbeat_decision": '群里安静时先看上一句。\n\n以严格 JSON 格式输出\n{"should_speak": false}',
            "reactive_gate": '先看是不是在找你。\n以严格 JSON 格式输出\n{"nope": true}',
            "system_constraints": "保持安静。以严格 JSON 格式输出，这是角色规矩。",
        },
    )
    decision = next(item for item in saved["fields"] if item["key"] == "heartbeat_decision")
    gate = next(item for item in saved["fields"] if item["key"] == "reactive_gate")
    rules = next(item for item in saved["fields"] if item["key"] == "system_constraints")
    assert decision["text"] == "群里安静时先看上一句。"
    assert gate["text"] == "先看是不是在找你。"
    assert decision["frozen_suffix"] == decision_suffix
    assert gate["frozen_suffix"] == gate_suffix
    assert '{"should_speak": false}' not in decision["text"]
    assert '{"nope": true}' not in gate["text"]
    assert rules["text"] == "保持安静。以严格 JSON 格式输出，这是角色规矩。"
    assert rules["frozen_suffix"] == ""
    sent = store.render_prompt("试验", "heartbeat_decision")
    assert sent.startswith("群里安静时先看上一句。")
    assert decision_suffix in sent
    assert '{"should_speak": false}' not in sent


def test_default_meme_protocol_has_frequency_placeholder() -> None:
    from gsuid_core.ai_core.prompt_group.catalog import spec_of

    body = default_body("meme_protocol")
    assert "{{meme_frequency}}" in body
    assert "{{emotion_words}}" in body
    assert "{{scene_words}}" in body
    assert "meme_frequency" in spec_of("meme_protocol").placeholders


def test_build_persona_prompt_meme_protocol_follows_allow_flag(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from gsuid_core.ai_core.persona import processor
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.persona.chat_style import ChatStyle
    from gsuid_core.ai_core.persona.meme_style import meme_frequency_line_for

    async def fake_load(_name: str) -> str:
        return "测"

    async def fake_date(format: str = "") -> str:
        return "2026年10月23日"

    monkeypatch.setattr(processor, "load_persona", fake_load)
    monkeypatch.setattr(processor, "get_current_date", fake_date)
    monkeypatch.setattr("gsuid_core.ai_core.persona.appearance.load_appearance_line", lambda _name: "")
    monkeypatch.setattr("gsuid_core.ai_core.persona.settings.get_master_title", lambda _name: "")
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.chat_style.resolve_chat_style",
        lambda _name: ChatStyle(bubbles=2, soft=60, hard=150, segment_hint="一段"),
    )
    monkeypatch.setattr("gsuid_core.ai_core.agent_node.registry.format_capability_roster", lambda _name: "")
    monkeypatch.setattr(
        "gsuid_core.ai_core.agent_node.registry.persona_allows_capability_agent",
        lambda _name, _node: False,
    )
    old_base = persona_config_manager._base_path
    persona_config_manager._base_path = tmp_path
    persona_config_manager._cache.clear()
    rare = "频率：少用。只在对方先发表情，或一个表情就能回完时才发。"
    mid = "频率：闲聊时大约每五到八次回复用一次。"
    often = "频率：闲聊时大约每两到三次回复用一次。"

    def _freq_norm(text: str) -> str:
        return text.replace(rare, "FREQ").replace(mid, "FREQ").replace(often, "FREQ")

    try:
        (tmp_path / "测").mkdir()
        assert persona_config_manager.set_allow_meme_reply("测", True)[0] is True
        assert persona_config_manager.set_meme_frequency("测", 0)[0] is True
        p0 = asyncio.run(processor.build_persona_prompt("测"))
        assert persona_config_manager.set_meme_frequency("测", 50)[0] is True
        p50 = asyncio.run(processor.build_persona_prompt("测"))
        assert persona_config_manager.set_meme_frequency("测", 100)[0] is True
        p100 = asyncio.run(processor.build_persona_prompt("测"))
        assert "<meme:情绪>" in p50
        assert "{{" not in p0
        assert "send_meme" not in p50
        assert "适合发" in p50
        assert "早柚" not in p50
        assert "唔…" not in p50 and "本貉" not in p50
        assert rare in p0 and mid not in p0 and often not in p0
        assert mid in p50 and rare not in p50 and often not in p50
        assert often in p100 and rare not in p100 and mid not in p100
        assert _freq_norm(p0) == _freq_norm(p50) == _freq_norm(p100)

        skipped = asyncio.run(processor.build_persona_prompt("测", include_meme_protocol=False))
        assert "<meme:情绪>" not in skipped
        assert rare not in skipped and mid not in skipped and often not in skipped

        assert persona_config_manager.set_allow_meme_reply("测", False)[0] is True
        denied = asyncio.run(processor.build_persona_prompt("测"))
        assert "<meme:情绪>" not in denied
        assert meme_frequency_line_for(50) not in denied
        assert "{{meme_frequency}}" not in denied
    finally:
        persona_config_manager._cache.clear()
        persona_config_manager._base_path = old_base


def test_speech_block_keeps_answer_contract_in_its_own_field(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.prompt_group.catalog import spec_of

    body = default_body("speech_block")
    assert "{{answer_contract}}" not in body
    assert "{{hard}}" not in body
    assert "硬上限" not in body
    assert "answer_contract" not in spec_of("speech_block").placeholders
    assert "hard" not in spec_of("speech_block").placeholders
    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    store.create_group("试验")
    saved = store.save_group(
        "试验",
        {"speech_block": body + "\n{{answer_contract}}"},
    )
    row = next(item for item in saved["fields"] if item["key"] == "speech_block")
    assert "{{answer_contract}}" not in row["text"]
    assert row["text"] == body
    monkeypatch.setattr(store, "group_name_for_persona", lambda _name: "试验")
    rendered = store.render_prompt(
        "试验",
        "speech_block",
        soft="60",
        segment_hint="一段",
        answer_contract="不应嵌进来",
    )
    assert "不应嵌进来" not in rendered
    assert "{{answer_contract}}" not in rendered


def test_closing_rule_stays_out_of_the_editable_body(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert default_body("closing").split("\n")[0].strip() != "---"
    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    store.create_group("试验")
    path = tmp_path / "试验" / "prompts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    prompts = payload["prompts"]
    assert isinstance(prompts, dict)
    prompts["closing"] = "---\n自定义收束"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    detail = store.group_detail("试验")
    row = next(item for item in detail["fields"] if item["key"] == "closing")
    assert row["text"] == "自定义收束"
    monkeypatch.setattr(store, "group_name_for_persona", lambda _name: "试验")
    assert store.render_prompt("试验", "closing") == "自定义收束"
    saved = store.save_group("试验", {"closing": "---\n仍是正文\n--- 不是分隔"})
    saved_row = next(item for item in saved["fields"] if item["key"] == "closing")
    assert saved_row["text"] == "仍是正文\n--- 不是分隔"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored_prompts = stored["prompts"]
    assert isinstance(stored_prompts, dict)
    assert stored_prompts["closing"] == "仍是正文\n--- 不是分隔"
    from gsuid_core.ai_core.prompt_group.render import prefix_closing_rule

    assert prefix_closing_rule("自定义收束") == "自定义收束"
    assert prefix_closing_rule("") == ""
    assert prefix_closing_rule("仍是正文\n--- 不是分隔") == "仍是正文\n--- 不是分隔"


def test_unfold_joins_indented_continuation_and_parts() -> None:
    from gsuid_core.ai_core.prompt_group.render import join_prompt_parts, unfold_wrapped_lines

    raw = "插不上话→优先<SILENCE>，\n  要拒绝或纠正。\n\n## 合规\n- 禁恐。"
    assert unfold_wrapped_lines(raw) == "插不上话→优先<SILENCE>， 要拒绝或纠正。\n\n## 合规\n- 禁恐。"
    assert join_prompt_parts("第一段\n", "\n第二段") == "第一段\n\n第二段"


def test_filled_values_keep_indented_lines(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    store.create_group("试验")
    store.save_group("试验", {"closing": "上文：\n{{history}}"})
    monkeypatch.setattr(store, "group_name_for_persona", lambda _name: "试验")
    rendered = store.render_prompt("试验", "closing", history="第一行\n    缩进还在")
    assert "    缩进还在" in rendered


def test_old_hard_cap_clause_is_removed_on_save(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    store.create_group("试验")
    saved = store.save_group("试验", {"speech_block": "建议不超过 {{soft}} 字，硬上限 {{hard}} 字。"})
    row = next(item for item in saved["fields"] if item["key"] == "speech_block")
    assert row["text"] == "建议不超过 {{soft}} 字。"
    assert "硬上限" not in row["text"]


def test_broken_group_file_uses_code_default(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path)
    store.create_group("试验")
    (tmp_path / "试验" / "prompts.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(store, "group_name_for_persona", lambda _name: "试验")
    assert store.render_prompt("试验", "closing") == store.render_prompt(None, "closing")


def test_delete_group_rebinds_persona(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.persona.config import persona_config_manager

    monkeypatch.setattr(store, "PROMPT_GROUP_PATH", tmp_path / "groups")
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.resource.list_available_personas",
        lambda: ["测"],
    )
    old_base = persona_config_manager._base_path
    persona_config_manager._base_path = tmp_path / "persona"
    persona_config_manager._cache.clear()
    try:
        (tmp_path / "persona" / "测").mkdir(parents=True)
        store.create_group("试验")
        assert persona_config_manager.set_prompt_group("测", "试验")[0] is True
        store.delete_group("试验")
        raw = persona_config_manager.get_config("测").get_config("prompt_group").data
        assert raw == "默认"
        assert store.group_exists("试验") is False
    finally:
        persona_config_manager._cache.clear()
        persona_config_manager._base_path = old_base


def test_persona_card_indent_stays_in_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from gsuid_core.ai_core.persona import processor
    from gsuid_core.ai_core.persona.chat_style import ChatStyle

    async def fake_load(_name: str) -> str:
        return "### 卡\n    Name: 测"

    async def fake_date(format: str = "") -> str:
        return "2026年10月18日"

    monkeypatch.setattr(processor, "load_persona", fake_load)
    monkeypatch.setattr(processor, "get_current_date", fake_date)
    monkeypatch.setattr("gsuid_core.ai_core.persona.appearance.load_appearance_line", lambda _name: "")
    monkeypatch.setattr("gsuid_core.ai_core.persona.settings.get_master_title", lambda _name: "")
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.chat_style.resolve_chat_style",
        lambda _name: ChatStyle(bubbles=2, soft=60, hard=150, segment_hint="一段"),
    )
    monkeypatch.setattr("gsuid_core.ai_core.agent_node.registry.format_capability_roster", lambda _name: "")
    monkeypatch.setattr(
        "gsuid_core.ai_core.agent_node.registry.persona_allows_capability_agent",
        lambda _name, _node: False,
    )

    def fake_render(_persona: str | None, key: str, **values: str) -> str:
        if key == "speech_block":
            return f"建议不超过 {values['soft']} 字"
        if key == "date_line":
            return f"当前日期：{values['date']}"
        return key

    monkeypatch.setattr("gsuid_core.ai_core.prompt_group.store.render_prompt", fake_render)
    prompt = asyncio.run(processor.build_persona_prompt("测"))
    assert "    Name: 测" in prompt
    assert "建议不超过 60 字" in prompt


def test_each_session_reloads_after_persona_file_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.ai_router import _check_persona_changed

    monkeypatch.setattr("gsuid_core.ai_core.ai_router._get_persona_mtime", lambda _name: 2.0)
    first = GsCoreAIAgent(persona_name="测")
    second = GsCoreAIAgent(persona_name="测")
    first.persona_source_mtime = 1.0
    second.persona_source_mtime = 1.0
    assert _check_persona_changed(first, "测") is True
    assert _check_persona_changed(second, "测") is True
    first.persona_source_mtime = 2.0
    assert _check_persona_changed(first, "测") is False
