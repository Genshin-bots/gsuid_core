"""归属透传（``ai_core.configs.attribution``）单测。

覆盖：默认解析的三种模式、无归属早退、宿主解析器接管 / 弃权 / 抛错降级，
以及叠加到既有 ModelSettings 时不得冲掉模型自带字段。
"""

from typing import Dict, Tuple, Iterator, Optional

import pytest
from pydantic_ai.settings import ModelSettings, merge_model_settings
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.openai import OpenAIChatModelSettings

from gsuid_core.ai_core.configs import attribution
from gsuid_core.ai_core.agent_run.state import RunOnceState
from gsuid_core.ai_core.agent_run.tools import ToolsPhase
from gsuid_core.ai_core.configs.attribution import (
    FORWARD_OFF,
    FORWARD_RAW,
    FORWARD_MODES,
    FORWARD_HASHED,
    CallAttribution,
    AttributionRequest,
    hash_end_user_id,
    default_attribution,
    default_end_user_id,
    read_forward_config,
    split_provider_config_name,
    resolve_attribution_settings,
    register_attribution_resolver,
    unregister_attribution_resolver,
)
from gsuid_core.utils.plugins_config.models import GsStrConfig
from gsuid_core.ai_core.configs.openai_config import OPENAI_CONFIG_TEMPLATE

SCOPE: Tuple[str, str, str] = ("canvas-1#default", "10086", "CanvasBackend")


@pytest.fixture(autouse=True)
def _clean_resolver() -> Iterator[None]:
    """每个用例前后都清空解析器，避免注册态跨用例泄漏。"""
    unregister_attribution_resolver()
    yield
    unregister_attribution_resolver()


def _stub_forward_config(monkeypatch: pytest.MonkeyPatch, mode: str, salt: str = "") -> None:
    """绕开配置文件读取，直接固定 (mode, salt)。"""

    def _read(_config_name: str) -> Tuple[str, str]:
        return mode, salt

    monkeypatch.setattr(attribution, "read_forward_config", _read)


def _resolve(
    *,
    config_full_name: str = "openai++Gateway",
    task_level: str = "high",
    scope: Optional[Tuple[str, str, str]] = SCOPE,
    session_id: str = "sess-1",
    create_by: str = "Chat",
) -> Optional[OpenAIChatModelSettings]:
    return resolve_attribution_settings(
        config_full_name=config_full_name,
        task_level=task_level,
        scope=scope,
        session_id=session_id,
        create_by=create_by,
    )


def _flat(settings: Optional[OpenAIChatModelSettings]) -> Optional[Dict[str, object]]:
    """摊平成普通 dict——TypedDict 的非必填键无法直接下标断言。"""
    return None if settings is None else dict(settings)


# ── 纯函数 ────────────────────────────────────────────────────────────────────


def test_hash_is_deterministic_and_salt_sensitive() -> None:
    assert hash_end_user_id("10086", "pepper") == hash_end_user_id("10086", "pepper")
    assert hash_end_user_id("10086", "pepper") != hash_end_user_id("10086", "other")
    assert hash_end_user_id("10086", "pepper") != hash_end_user_id("10087", "pepper")
    assert len(hash_end_user_id("10086", "")) == 32
    assert "10086" not in hash_end_user_id("10086", "pepper")


def test_split_provider_config_name_tolerates_legacy_and_unknown() -> None:
    assert split_provider_config_name("openai++Gateway") == ("openai", "Gateway")
    assert split_provider_config_name("anthropic++Claude") == ("anthropic", "Claude")
    # 旧格式无分隔符按 openai 处理；未知 provider 原样返回而不抛错
    assert split_provider_config_name("MiniMAX") == ("openai", "MiniMAX")
    assert split_provider_config_name("weird++X") == ("weird", "X")


def test_default_attribution_modes() -> None:
    def req(mode: str, user_id: str = "10086") -> AttributionRequest:
        return AttributionRequest(
            provider="openai",
            config_name="Gateway",
            task_level="high",
            forward_mode=mode,
            group_id="g",
            user_id=user_id,
            bot_id="b",
            session_id="s",
            create_by="Chat",
        )

    raw = default_attribution(req(FORWARD_RAW), "pepper")
    assert raw is not None and raw.end_user_id == "10086"

    hashed = default_attribution(req(FORWARD_HASHED), "pepper")
    assert hashed is not None and hashed.end_user_id == hash_end_user_id("10086", "pepper")

    # 真正无主的后台调用不透传，让上游归入匿名桶
    assert default_attribution(req(FORWARD_HASHED, user_id=""), "pepper") is None


# ── resolve_attribution_settings ─────────────────────────────────────────────


def test_off_is_the_default_and_forwards_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_OFF)
    assert _resolve() is None


def test_raw_mode_sets_openai_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    assert _flat(_resolve()) == {"openai_user": "10086"}


def test_hashed_mode_does_not_leak_raw_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_HASHED, salt="pepper")
    assert _flat(_resolve()) == {"openai_user": hash_end_user_id("10086", "pepper")}


def test_non_openai_provider_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``user`` 是 OpenAI 协议字段，其余 provider 不该走到读配置这一步。"""

    def _boom(_config_name: str) -> Tuple[str, str]:
        raise AssertionError("非 openai provider 不应读取 openai 配置")

    monkeypatch.setattr(attribution, "read_forward_config", _boom)
    assert _resolve(config_full_name="anthropic++Claude") is None
    assert _resolve(config_full_name="gemini++Gemini") is None


def test_missing_active_config_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    assert _resolve(config_full_name="") is None


def test_no_scope_forwards_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    assert _resolve(scope=None) is None


def test_unknown_forward_mode_falls_back_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, "totally-bogus")
    assert _resolve() is None


# ── 宿主解析器 ────────────────────────────────────────────────────────────────


def test_resolver_takes_over_id_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    seen: list[AttributionRequest] = []

    def resolver(req: AttributionRequest) -> Optional[CallAttribution]:
        seen.append(req)
        return CallAttribution(end_user_id=f"subject:{req.user_id}", extra_headers={"X-Demo-Tier": "pro"})

    register_attribution_resolver(resolver)

    assert _flat(_resolve()) == {
        "openai_user": "subject:10086",
        "extra_headers": {"X-Demo-Tier": "pro"},
    }
    assert len(seen) == 1
    assert seen[0].forward_mode == FORWARD_RAW
    assert (seen[0].group_id, seen[0].user_id, seen[0].bot_id) == SCOPE
    assert seen[0].session_id == "sess-1" and seen[0].create_by == "Chat"


def test_resolver_still_gated_by_config_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """开关是主闸：注册了解析器也不该对 off 的上游偷偷发东西。"""
    _stub_forward_config(monkeypatch, FORWARD_OFF)
    register_attribution_resolver(lambda _req: CallAttribution(end_user_id="should-not-appear"))
    assert _resolve() is None


def test_resolver_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    register_attribution_resolver(lambda _req: None)
    assert _resolve() is None


def test_resolver_returning_empty_attribution_forwards_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    register_attribution_resolver(lambda _req: CallAttribution())
    assert _resolve() is None


def test_resolver_exception_degrades_instead_of_breaking_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)

    def resolver(_req: AttributionRequest) -> Optional[CallAttribution]:
        raise RuntimeError("上游身份服务挂了")

    register_attribution_resolver(resolver)
    assert _resolve() is None


def test_resolver_can_forward_headers_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    register_attribution_resolver(lambda _req: CallAttribution(extra_headers={"X-Demo": "1"}))
    assert _flat(_resolve()) == {"extra_headers": {"X-Demo": "1"}}


def test_unregister_restores_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    register_attribution_resolver(lambda _req: CallAttribution(end_user_id="custom"))
    unregister_attribution_resolver()
    assert _flat(_resolve()) == {"openai_user": "10086"}


# ── 叠加语义 ──────────────────────────────────────────────────────────────────


def test_overlay_preserves_model_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """归属只是逐 key 增量：不得冲掉模型对象自带的 thinking / usage 等设置。"""
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    base = OpenAIChatModelSettings(thinking="high", openai_continuous_usage_stats=True, temperature=0.4)
    merged = merge_model_settings(base, _resolve())

    assert merged is not None
    assert dict(merged) == {
        "thinking": "high",
        "openai_continuous_usage_stats": True,
        "temperature": 0.4,
        "openai_user": "10086",
    }
    # base 本身不被就地改动（模型对象共享 settings，污染会跨 run 泄漏）
    assert "openai_user" not in dict(base)


def test_overlay_with_none_is_a_noop() -> None:
    base = ModelSettings(temperature=0.4)
    assert merge_model_settings(base, None) == base


# ── 配置读取契约 ──────────────────────────────────────────────────────────────


class _StubEntry:
    def __init__(self, data: object) -> None:
        self.data = data


class _StubConfig:
    """最小 StringConfig 替身：只需支持 ``get_config(key).data``。"""

    def __init__(self, mode: object, salt: object) -> None:
        self._values = {"forward_end_user_id": mode, "end_user_id_salt": salt}

    def get_config(self, key: str) -> _StubEntry:
        return _StubEntry(self._values[key])


def test_template_default_is_off() -> None:
    """新建配置必须默认 off——启用本特性前，框架行为与不存在本模块完全一致。"""
    mode_cfg = OPENAI_CONFIG_TEMPLATE["forward_end_user_id"]
    salt_cfg = OPENAI_CONFIG_TEMPLATE["end_user_id_salt"]
    assert isinstance(mode_cfg, GsStrConfig)
    assert isinstance(salt_cfg, GsStrConfig)
    assert mode_cfg.data == FORWARD_OFF
    assert mode_cfg.options == list(FORWARD_MODES)
    # salt 是密钥性质，控制台须打码
    assert salt_cfg.data == "" and salt_cfg.secret is True


class _Host(ToolsPhase):
    """最小 run-once 宿主：只装 ``_run_once_build_agent_meta`` 用得到的字段。"""

    def __init__(self, model: TestModel, active_config_name: str) -> None:
        self.model = model
        self._active_config_name = active_config_name
        self.task_level = "high"
        self.session_id = "sess-1"
        self.create_by = "Chat"
        self.system_prompt = "你是一个智能助手。"
        self.extract_history_calls = 0

    def extract_history(self) -> None:
        self.extract_history_calls += 1

    def _inject_deepseek_rp_marker(self, st: object) -> None:
        return None


def _build_state(scope: Optional[Tuple[str, str, str]]) -> RunOnceState:
    st = RunOnceState(
        user_message="hi",
        bot=None,
        ev=None,
        rag_context=None,
        tools=[],
        return_mode="return",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
    )
    st.budget_scope = scope
    st.addr_gated = False
    st.expose_dynamic = False
    return st


def test_build_agent_injects_attribution_without_polluting_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """集成点：归属只影响本次 run 的 Agent，模型对象自身的 settings 必须原样不动。"""
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    model = TestModel(settings=OpenAIChatModelSettings(temperature=0.7))
    host = _Host(model, "openai++Gateway")

    agent = host._run_once_build_agent_meta(_build_state(SCOPE))

    assert dict(agent.model_settings or {}) == {"temperature": 0.7, "openai_user": "10086"}
    # 模型对象在会话内被多个 run 共享，就地写会把上一个用户的标识带给下一个
    assert dict(model.settings or {}) == {"temperature": 0.7}
    assert host.extract_history_calls == 1


def test_build_agent_is_untouched_when_forwarding_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_OFF)
    model = TestModel(settings=OpenAIChatModelSettings(temperature=0.7))
    host = _Host(model, "openai++Gateway")

    agent = host._run_once_build_agent_meta(_build_state(SCOPE))

    assert dict(agent.model_settings or {}) == {"temperature": 0.7}


def test_build_agent_survives_model_without_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_RAW)
    host = _Host(TestModel(), "openai++Gateway")

    agent = host._run_once_build_agent_meta(_build_state(SCOPE))

    assert dict(agent.model_settings or {}) == {"openai_user": "10086"}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("off", FORWARD_OFF),
        ("raw", FORWARD_RAW),
        ("RAW", FORWARD_RAW),
        ("  hashed ", FORWARD_HASHED),
        ("bogus", FORWARD_OFF),
        ("", FORWARD_OFF),
    ],
)
def test_read_forward_config_normalizes(monkeypatch: pytest.MonkeyPatch, stored: str, expected: str) -> None:
    def _get_config(_config_name: str) -> _StubConfig:
        return _StubConfig(stored, "pepper")

    monkeypatch.setattr(attribution, "get_openai_config", _get_config)
    mode, salt = read_forward_config("Gateway")
    assert mode == expected
    assert salt == "pepper"


# ── default_end_user_id：解析器复用默认归属语义（拿不到 salt） ──────────────────


def _req(mode: str, user_id: str = "10086") -> AttributionRequest:
    return AttributionRequest(
        provider="openai",
        config_name="Gateway",
        task_level="high",
        forward_mode=mode,
        group_id="canvas-1#default",
        user_id=user_id,
        bot_id="Bot",
        session_id="sess-1",
        create_by="Chat",
    )


def test_default_end_user_id_follows_configured_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_HASHED, salt="pepper")

    assert default_end_user_id(_req(FORWARD_RAW)) == "10086"
    # salt 由本函数从配置里取，解析器无从得知
    assert default_end_user_id(_req(FORWARD_HASHED)) == hash_end_user_id("10086", "pepper")
    assert default_end_user_id(_req(FORWARD_OFF)) == ""
    assert default_end_user_id(_req(FORWARD_RAW, user_id="")) == ""


def test_resolver_can_add_headers_and_keep_default_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_forward_config(monkeypatch, FORWARD_HASHED, salt="pepper")

    def resolver(req: AttributionRequest) -> Optional[CallAttribution]:
        return CallAttribution(
            end_user_id=default_end_user_id(req),
            extra_headers={"Authorization": "Bearer per-call-token"},
        )

    register_attribution_resolver(resolver)

    assert _flat(_resolve()) == {
        "openai_user": hash_end_user_id("10086", "pepper"),
        "extra_headers": {"Authorization": "Bearer per-call-token"},
    }
