"""pydantic-ai 1.72.0 → 2.13.0 升级回归测试。

锁定升级到 2.13.0 的关键契约，确保后续维护不再回退到 1.x 已废弃的写法。

升级要点（详见 https://pydantic.dev/docs/ai/project/changelog/ 的 v2.0.0b1）：

1. ModelProfile 由 dataclass 改为 ``TypedDict``，``from_profile`` 和
   ``dataclasses.replace`` 都已移除；改用模块级 ``merge_profile`` 叠加字段。
2. ``Model(profile=...)`` 的可调用签名由 ``(name: str) -> Profile | None``
   改为 ``(default: ModelProfile) -> ModelProfile``（V2 把已解析默认喂过来）。
3. ``result.usage()`` / ``result.timestamp()`` 改为属性 ``result.usage`` /
   ``result.timestamp``；继续当方法调用会抛 ``TypeError``。
4. ``UsageLimits(request_tokens_limit=)`` → ``input_tokens_limit=``；
   ``Usage → RunUsage``。
5. MCP 入口改名 ``MCPServer*`` → ``MCPToolset``（GS Core 当前用 fastmcp，未触线）。
"""

from __future__ import annotations

import inspect

# ─────────────────────────────────────────────
# §1 关键符号 import 不再回退到 v1 的删除项
# ─────────────────────────────────────────────


def test_pydantic_ai_version_meets_minimum() -> None:
    """pydantic-ai-slim 必须至少是 2.13.0（升级目标线）。"""
    from importlib.metadata import version

    assert version("pydantic-ai-slim") >= "2.13.0"


def test_critical_imports_resolve_to_v2_paths() -> None:
    """GS Core 持有的 pydantic_ai 顶层符号在 2.13.0 下仍可解析。

    完整覆盖：gs_agent / register / image_reader / configs.models 等模块的 import 行
    （见 grep "from pydantic_ai" gsuid_core）；升级回归必须不能从这些路径消失。
    """
    from pydantic_ai import Agent, RunContext, ToolReturn
    from pydantic_ai.tools import Tool
    from pydantic_ai.usage import RunUsage, UsageLimits, RequestUsage
    from pydantic_ai.messages import ImageUrl, ToolReturnPart
    from pydantic_ai.profiles import ModelProfile, merge_profile
    from pydantic_ai.settings import ModelSettings, ThinkingLevel
    from pydantic_ai.models.openai import (
        OpenAIChatModel,
        OpenAIResponsesModel,
        OpenAIStreamedResponse,
        OpenAIChatModelSettings,
    )
    from pydantic_ai.profiles.openai import OpenAIModelProfile
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from pydantic_ai.toolsets.abstract import ToolsetTool, AbstractToolset
    from pydantic_ai.toolsets.function import FunctionToolsetTool
    from pydantic_ai.providers.anthropic import AnthropicProvider

    for sym in (
        Agent,
        RunContext,
        ToolReturn,
        Tool,
        ImageUrl,
        ToolReturnPart,
        ModelSettings,
        ThinkingLevel,
        RequestUsage,
        RunUsage,
        UsageLimits,
        ModelProfile,
        merge_profile,
        OpenAIModelProfile,
        OpenAIChatModel,
        OpenAIChatModelSettings,
        OpenAIResponsesModel,
        OpenAIStreamedResponse,
        AnthropicModel,
        OpenAIProvider,
        AnthropicProvider,
        AbstractToolset,
        ToolsetTool,
        FunctionToolsetTool,
    ):
        assert sym is not None, f"import failed: {sym}"


# ─────────────────────────────────────────────
# §2 ModelProfile 是 TypedDict，原 from_profile 已移除
# ─────────────────────────────────────────────


def test_openai_model_profile_is_typed_dict() -> None:
    """v2.0+: OpenAIModelProfile 是 TypedDict(dict 子类)，不再是 dataclass。

    升级到 2.13 的标志：dataclasses.replace 不再能用在它上面；维护者绕道
    的常见征兆是 try 把 profile 当对象调 ``profile.field``，这里直接锁住。
    """
    from pydantic_ai.profiles.openai import OpenAIModelProfile

    assert isinstance(OpenAIModelProfile, type)
    assert issubclass(OpenAIModelProfile, dict)
    # from_profile 在 v2.0 升级路径里被移除；保留它是 reserved word。
    assert not hasattr(OpenAIModelProfile, "from_profile")


def test_openai_model_profile_construction_returns_dict() -> None:
    """构造 OpenAIModelProfile(k=v) 直接返回 TypedDict，叠加字段需用 merge_profile。"""
    from pydantic_ai.profiles.openai import OpenAIModelProfile

    profile = OpenAIModelProfile(openai_chat_send_back_thinking_parts=False)
    assert isinstance(profile, dict)
    assert "openai_chat_send_back_thinking_parts" in profile
    assert profile["openai_chat_send_back_thinking_parts"] is False


def test_merge_profile_overlay_preserves_baseline() -> None:
    """merge_profile 后续参数覆盖同名键，但保留基底所有字段。"""
    from pydantic_ai.profiles import ModelProfile, merge_profile
    from pydantic_ai.profiles.openai import OpenAIModelProfile

    base = ModelProfile(supports_tools=True, supports_json_schema_output=True)
    overlay = OpenAIModelProfile(openai_chat_send_back_thinking_parts=False)
    merged = merge_profile(base, overlay)

    assert "supports_tools" in merged and merged["supports_tools"] is True
    assert "supports_json_schema_output" in merged and merged["supports_json_schema_output"] is True
    assert "openai_chat_send_back_thinking_parts" in merged
    assert merged["openai_chat_send_back_thinking_parts"] is False


# ─────────────────────────────────────────────
# §3 Model(profile=) 的 callable 签名是 (default) -> Profile
# ─────────────────────────────────────────────


def test_model_profile_callable_signature_v2() -> None:
    """pydantic_ai 用 ``ModelProfileSpec`` 标明 v2 callable shape。

    v1: ``Callable[[str], ModelProfile | None]`` 接收 model_name；v2:
    ``Callable[[ModelProfile], ModelProfile]`` 接收已解析默认；GS Core
    自己构造 profile_spec 时必须按 v2 签名实现，否则运行时调用会抛
    "takes 1 positional argument but 2 were given"。
    """
    import collections.abc as _abc

    from pydantic_ai.profiles import ModelProfile, ModelProfileSpec

    args = getattr(ModelProfileSpec, "__args__", ())
    callable_branch = next(
        (a for a in args if getattr(a, "__origin__", None) is _abc.Callable),
        None,
    )
    assert callable_branch is not None, f"ModelProfileSpec lacks callable branch: {args}"

    # typing.Callable[[X], Y] 的 __args__ == (X, Y); v2 用前向引用, 实际存的是字符串
    params = callable_branch.__args__
    assert len(params) == 2, f"v2 spec wants (arg, ret) pair, got {params}"

    arg_spec = params[0]
    if isinstance(arg_spec, type):
        assert arg_spec is ModelProfile, f"v2 spec callable 入参必须是 ModelProfile, got {arg_spec}"
    else:
        # 前向引用走字符串, 做等价检查 + 名称检查
        assert arg_spec == "ModelProfile", f"v2 spec callable 入参名必须是 ModelProfile, got {arg_spec}"

    ret_spec = params[1]
    if isinstance(ret_spec, type):
        assert ret_spec is ModelProfile, f"v2 spec callable 返回值必须是 ModelProfile, got {ret_spec}"
    else:
        assert ret_spec == "ModelProfile", f"v2 spec callable 返回值名必须是 ModelProfile, got {ret_spec}"


def test_get_openai_model_by_name_profile_spec_uses_v2_shape() -> None:
    """静态锁：configs/models.py 中的 profile_spec 实现走 v2 callable shape。

    send_back_thinking=off 分支必须：
    1. 用 ``merge_profile(default, OpenAIModelProfile(...))``，禁止
       ``from_profile`` 与 ``dataclasses.replace``；
    2. 接受 ``default: ModelProfile`` 而不是 ``name: str``。
    """
    import gsuid_core.ai_core.configs.models as models_mod

    src = inspect.getsource(models_mod.get_openai_model_by_name)
    # v1 API 必须已撤
    assert "OpenAIModelProfile.from_profile" not in src, "迁移完仍有 v1 from_profile"
    assert "dataclasses.replace" not in src, "迁移完仍用 dataclasses.replace 改 profile"
    assert "_dc_replace" not in src, "迁移完仍用 _dc_replace 别名"
    # v2 API 必须到位
    assert "merge_profile(" in src, "send_back_thinking 覆写未使用 v2 merge_profile"
    # v2.0: Callable[[ModelProfile], ModelProfile] 形状; 命名前缀同样允许 (
    # 提取出的内部辅助函数 ``_overlay_send_back_off`` 也算 v2 形态的实现)。
    assert "_overlay_send_back_off(default: ModelProfile)" in src or "profile_spec(default: ModelProfile)" in src, (
        "send_back_thinking 覆写仍按 v1 签名 (name, _provider); 期望 (default: ModelProfile)"
    )


# ─────────────────────────────────────────────
# §4 Result API: usage / timestamp 由方法改为属性
# ─────────────────────────────────────────────


def test_usage_limits_supports_request_limit_kw() -> None:
    """v2 中 UsageLimits 仍支持 ``request_limit=``（兼容 v1 用法）。

    GS Core 里 ``UsageLimits(request_limit=1)`` 仍在 gs_agent 中使用；该参数必须
    仍按"最大轮数"语义工作，不能被改名/移除（changelog 没动 request_limit 名称）。
    """
    from pydantic_ai.usage import UsageLimits

    sig = inspect.signature(UsageLimits.__init__)
    assert "request_limit" in sig.parameters


def test_gs_agent_uses_result_usage_as_property() -> None:
    """锁：gs_agent 的 token 记账必须读 ``result.usage`` 属性，不准调用 ``()``。

    v1 的 ``result.usage()`` 在 v2 直接抛 ``TypeError``；我们的兼容层 try/except
    TypeError 只兜底"日志不报错"，真正想要的是直接走 v2 写法。
    """
    import gsuid_core.ai_core.gs_agent as agent_mod

    src = inspect.getsource(agent_mod)
    # 真正读取 token 用的那一行必须是属性访问
    assert "result.usage\n" in src or "result.usage," in src or "result.usage " in src, (
        "gs_agent 应该用 result.usage 属性访问而非 result.usage()"
    )
    # v1 旧写法必须已撤（除注释/字符串外）
    bad_lines = [
        line
        for line in src.splitlines()
        if line.lstrip().startswith("usage_obj") and "result.usage" in line and line.rstrip().endswith("()")
    ]
    assert not bad_lines, f"残留 result.usage() 方法调用: {bad_lines}"


def test_run_usage_attributes_in_v2() -> None:
    """RunUsage 字段名在 v2 仍叫 input_tokens/output_tokens（v1 已重命名）。

    升级到 2.13 时 token 名也跟 v1 同名（changelog 显示 v1.100 起仅做 input/output
    重命名），gs_agent 是 ``usage_obj.input_tokens`` 等字段，回归必须保住。
    """
    from pydantic_ai.usage import RunUsage

    u = RunUsage()
    for attr in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        assert hasattr(u, attr), f"RunUsage 缺失 {attr}"


# ─────────────────────────────────────────────
# §5 工厂函数可达性（import-time 不再触雷 v2 移除的符号）
# ─────────────────────────────────────────────


def test_module_factory_imports_succeed() -> None:
    """configs.models 模块必须能干净 import（不再触及 v2 已移除符号）。

    该模块导入了 v1 用 OpenAIModelProfile.from_profile / dataclasses.replace
    的写法，迁移完应该无残留；静态读源码 + import 双重锁。
    """
    import gsuid_core.ai_core.configs.models as models_mod  # noqa: F401

    # 模块级不能残留 v1 物
    mod_src = inspect.getsource(models_mod)
    assert "from dataclasses import replace" not in mod_src, "configs/models.py 顶层不应再 import dataclasses.replace"


def test_openai_factory_returns_models_without_v1_overrides() -> None:
    """get_openai_model_by_name 主体走 v2 路径；构造侧关键标志位需 v2 兼容。

    不实测出网（不真发请求），仅校验：
    1. AutoUsageOpenAIChatModel 派生链依然是 OpenAIChatModel；
    2. _AutoUsageStreamedResponse 继承 OpenAIStreamedResponse，签名仍是
       ``async def _validate_response``（不依赖 dataclass 元数据）。
    3. get_openai_model_by_name 工厂内构造 OpenAIChatModel 时走
       ``OpenAIChatModelSettings(...)``（v2 TypedDict 形态）而非 v1 dataclass。
    """
    import inspect

    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIStreamedResponse

    from gsuid_core.ai_core.configs.models import (
        AutoUsageOpenAIChatModel,
        get_openai_model_by_name,
        _AutoUsageStreamedResponse,
    )

    assert issubclass(AutoUsageOpenAIChatModel, OpenAIChatModel), "AutoUsageOpenAIChatModel 仍应继承 OpenAIChatModel"
    assert issubclass(_AutoUsageStreamedResponse, OpenAIStreamedResponse), (
        "_AutoUsageStreamedResponse 仍应继承 OpenAIStreamedResponse"
    )

    # v2: factory 里 settings 用 OpenAIChatModelSettings(...) 即 TypedDict 路径
    src = inspect.getsource(get_openai_model_by_name)
    assert "OpenAIChatModelSettings(" in src, "get_openai_model_by_name 未走 v2 OpenAIChatModelSettings 路径"


# ─────────────────────────────────────────────
# §6 gs_agent 顶层 import 在 v2 下不破
# ─────────────────────────────────────────────


def test_gs_agent_module_imports() -> None:
    """gs_agent 顶层 import 链路在 v2.13.0 下不出 ImportError。

    检查 gs_agent 通过 ``from pydantic_ai.X import Y`` 公开用到的 v2.13 符号
    都还在，避免某次升级再次撞到删除项。
    """
    import importlib

    import gsuid_core.ai_core.gs_agent as agent_mod

    importlib.reload(agent_mod)
    # 关键 runtime 符号（按 gs_agent.py 实际 import 的列表）：
    # Agent / CallToolsNode / ModelRequestNode / RunUsage / UsageLimits / ModelProfile
    # 注意: ModelSettings / ThinkingLevel 仅在 gs_agent 中用于类型注解,
    # 不在运行时绑定, dir() 看不到(预期)。
    expected = (
        "Agent",
        "CallToolsNode",
        "ModelRequestNode",
        "RunUsage",
        "UsageLimits",
        "ModelProfile",
    )
    for name in expected:
        assert name in dir(agent_mod), f"gs_agent 缺失 v2.13 符号: {name}"
