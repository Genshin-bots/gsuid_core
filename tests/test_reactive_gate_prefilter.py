"""软触发沉默门规则预筛：零 LLM 路径。"""

from gsuid_core.ai_core.heartbeat.decision import _reactive_gate_rule_prefilter


def test_empty_and_pure_particles_silence() -> None:
    assert _reactive_gate_rule_prefilter("") is False
    assert _reactive_gate_rule_prefilter("哈哈哈") is False
    assert _reactive_gate_rule_prefilter("嗯…") is False
    assert _reactive_gate_rule_prefilter("！！！") is False


def test_short_followup_passes() -> None:
    assert _reactive_gate_rule_prefilter("你说的那个怎么样了") is True
    assert _reactive_gate_rule_prefilter("改成明天八点") is True
    assert _reactive_gate_rule_prefilter("早柚还在吗") is True


def test_ambiguous_goes_to_llm() -> None:
    assert _reactive_gate_rule_prefilter("今天群里好热闹啊感觉大家都挺有精神的") is None
