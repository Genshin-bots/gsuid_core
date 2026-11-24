"""装配统一（§5.3）的防漂移锁：生产入口与评测端点必须消费同一装配函数。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _load_kits_once() -> None:
    from gsuid_core.ai_core.kits import occupants_of, load_enabled_kits

    if not occupants_of("mood"):
        asyncio.run(load_enabled_kits(run_init_steps=False))


def test_both_entries_consume_shared_assembly() -> None:
    handle_ai = _src("gsuid_core/ai_core/handle_ai.py")
    endpoint = _src("gsuid_core/webconsole/chat_with_history_api.py")
    router = _src("gsuid_core/ai_core/ai_router.py")
    assembly = _src("gsuid_core/ai_core/context_assembly.py")
    loop = _src("gsuid_core/ai_core/agent_run/loop.py")

    assert "async def run_interactive_turn(" in handle_ai
    assert "run_interactive_turn(" in endpoint
    assert 'else "Chat"' in endpoint
    assert "memory_eval" not in endpoint and "evidence_mode" not in endpoint
    assert "_MEMORY_EVAL_GUIDE" not in endpoint
    assert "parse_injected_clock" not in assembly and "parse_injected_clock" not in endpoint
    assert "parse_clock_at" in endpoint and "req.clock_at" in endpoint
    assert 'create_by in _MAIN_PERSONA_CREATE_BY and st.return_mode != "return"' in loop
    assert "dynamic_tools=http_dynamic_tools(" in endpoint
    assert "dual_route_retrieve(" not in endpoint and "classifier_service" not in endpoint
    assert "settle_turn(" not in endpoint and "assemble_dynamic_context(" in handle_ai
    for name, src in [("ai_router", router), ("chat_with_history_api", endpoint)]:
        assert "build_session_system_prompt(" in src, f"{name} 不再消费共享 system prompt 装配"
    for name, src in [("handle_ai", handle_ai), ("chat_with_history_api", endpoint)]:
        assert 'f"【长期记忆】' not in src and "【长期记忆】\\n" not in src
        assert "（口吻锚点：" not in src


def test_dynamic_context_ordering_and_suffix_policy() -> None:
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.hooks.points import AgentHookPoint
    from gsuid_core.ai_core.context_assembly import (
        _SUFFIX_PRODUCT_CAP,
        suffix_allowed_blocks,
        assemble_dynamic_context,
        _apply_suffix_block_policy,
    )
    from gsuid_core.ai_core.interaction_scaffold import TurnGraph

    _load_kits_once()
    full, has_actionable = asyncio.run(
        assemble_dynamic_context(
            query="那深圳呢",
            user_id="test_u",
            bot_id="TEST",
            persona_name=None,
            mood_key="test_u",
            rel=None,
            history_context="[历史对话] 旧→新\n小明: 你好",
            memory_context_text="用户喜欢喝美式",
            soft_triggered=True,
        )
    )
    assert has_actionable in (False, True)
    i_hist, i_mem = (
        full.find("[历史对话]"),
        full.find("[长期记忆"),
    )
    assert 0 <= i_hist < i_mem

    tg = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="p",
        is_tome=True,
        primary_speaker="u1",
        call_to_self=True,
    )
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=tg)
    allowed = suffix_allowed_blocks(ctx)
    assert allowed is not None and "voice_anchor" in allowed
    voice = "（口吻：" + "轻软简短" * 45 + "）"
    ctx.blocks = {
        "voice_anchor": voice,
        "inbox_pending": "（待你回应）" + "甲" * 160,
        "task": "事项#1｜进行中" + "X" * 90,
        "relationship": "R" * 90,
        "memory": "M" * 80,
        "history": "H" * 500,
    }
    _apply_suffix_block_policy(ctx)
    assert ctx.blocks["voice_anchor"] == voice and ctx.blocks["history"] == "H" * 500
    assert ctx.blocks["task"].startswith("事项#1｜进行中") and not ctx.blocks["task"].endswith("…")
    assert ctx.blocks["relationship"] == "R" * 90
    assert (
        sum(
            len(v)
            for k, v in ctx.blocks.items()
            if k not in ("voice_anchor", "history", "memory", "inbox_pending", "evidence")
        )
        <= _SUFFIX_PRODUCT_CAP
    )

    idle = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="p",
        is_tome=False,
        primary_speaker="u1",
        call_to_self=False,
    )
    idle_ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=idle)
    # 未寻址群轮：产品块全剥，但收件箱结构块（待回应/证据）永远放行。
    assert suffix_allowed_blocks(idle_ctx) == frozenset({"voice_anchor", "history", "inbox_pending", "evidence"})
    idle_ctx.blocks = {"voice_anchor": voice, "task": "任务块", "memory": "M" * 80, "history": "H" * 500}
    _apply_suffix_block_policy(idle_ctx)
    assert "task" not in idle_ctx.blocks and "memory" not in idle_ctx.blocks

    soft_ctx = AgentHookContext(
        point=AgentHookPoint.COMPOSE_CONTEXT,
        turn_graph=idle,
        soft_triggered=True,
    )
    assert suffix_allowed_blocks(soft_ctx) == frozenset({"voice_anchor", "history", "inbox_pending", "evidence"})


def test_alias_and_session_anchor_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.messages import ModelRequest, SystemPromptPart
    from pydantic_ai.models.test import TestModel

    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.entity_index import clear_entity_index, register_entity_surface
    from gsuid_core.ai_core.control.lines import journal_delta_line
    from gsuid_core.ai_core.history_compact import _peel_ephemeral_system_lines
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context, history_line_is_assistant
    from gsuid_core.ai_core.agent_run.support import _ensure_session_anchor_on_first_user
    from gsuid_core.webconsole.chat_with_history_api import ChatHistoryTurn, _load_request_history

    assert history_line_is_assistant("#4 [14:32:18] 你: 晴天〔已发〕")
    assert history_line_is_assistant("#5 [14:32:20] 你→#3(小明): 晴天〔已发〕")
    assert history_line_is_assistant("#6 [14:32:30] 你[回执]→小明: 已记录")
    assert not history_line_is_assistant("#3 [14:32:05] 小明(用户ID:456): 今天天气怎么样")

    _load_kits_once()
    clear_entity_index()
    register_entity_surface("北辰星", "北辰星", "SomePlugin")
    register_entity_surface("长离", "长离", "OtherPlugin")
    try:

        def _assemble(query: str, history: str) -> str:
            return asyncio.run(
                assemble_dynamic_context(
                    query=query,
                    user_id="test_u",
                    bot_id="TEST",
                    persona_name="北辰星",
                    mood_key="test_u",
                    history_context=history,
                )
            )[0]

        assert "别名表命中" not in _assemble("北辰星早上好", "")
        assert "别名表命中" not in _assemble(
            "傻孩子我是怕你被压扁", "[历史对话] 旧→新\n[15:40:01] 路人(用户ID:other_u): 长离今天怎么样"
        )
        mixed = _assemble("北辰星，长离用什么武器", "")
        assert "长离→OtherPlugin" in mixed and "北辰星→" not in mixed
    finally:
        clear_entity_index()

    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_for_task", lambda _level, _kind=None: TestModel())
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_config_name_for_task", lambda _level, _kind=None: "test:m")
    monkeypatch.setattr("gsuid_core.ai_core.gs_agent.get_model_fingerprint_for_task", lambda _level, _kind=None: "fp")

    agent = create_agent(system_prompt="你是测试角色。", persona_name="早柚", create_by="Chat")
    _load_request_history(
        agent,
        [ChatHistoryTurn(role="user", content="你好"), ChatHistoryTurn(role="assistant", content="你好呀")],
        guard_on=False,
    )
    assert isinstance(agent.history[0], ModelRequest)
    assert any(isinstance(p, SystemPromptPart) and p.content == "你是测试角色。" for p in agent.history[0].parts)

    proactive = create_agent(system_prompt="你是巡检角色。", persona_name="早柚", create_by="Chat")
    proactive.append_proactive_assistant_turn(content="群里好安静呀", source="heartbeat", trigger_reason="test")
    assert any(isinstance(p, SystemPromptPart) and p.content == "你是巡检角色。" for p in proactive.history[0].parts)

    cur, _, where, _ = _ensure_session_anchor_on_first_user(
        [], "你好", "你好", persona_name="", marker="【角色沉浸要求】测试"
    )
    assert where == "current" and "【角色沉浸要求】测试" in cur
    cur2, lean2, where2, _ = _ensure_session_anchor_on_first_user(
        agent.history, "第二轮发言", "第二轮发言", persona_name="", marker="【角色沉浸要求】测试"
    )
    assert where2 == "already" and cur2 == "第二轮发言" and lean2 == "第二轮发言"

    delta = journal_delta_line("[15:05 | 模拟盘记账] 记完收盘账")
    assert _peel_ephemeral_system_lines(f"[用户发言]\n今天怎么样\n{delta}") == "[用户发言]\n今天怎么样"

    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.messages import ImageUrl, UserPromptPart
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.profiles.openai import openai_model_profile

    from gsuid_core.ai_core.control.lines import TOOLS_EXECUTION_GATED_DENY
    from gsuid_core.ai_core.configs.models import openai_user_thinking_profile
    from gsuid_core.ai_core.history_compact import _relean_user_turn
    from gsuid_core.ai_core.configs.openai_wire import (
        AutoUsageOpenAIChatModel,
        RecoveringOpenAIChatModel,
        RecoveringOpenAIResponsesModel,
        revise_openai_attempt,
        clear_openai_wire_facts,
        openai_profile_for_model,
    )
    from gsuid_core.ai_core.memory.retrieval.types import Episode
    from gsuid_core.ai_core.buildin_tools.visibility import tool_return_is_gate_reject
    from gsuid_core.ai_core.memory.retrieval.dual_route import _filter_point_lookup_episodes

    full_req = ModelRequest(parts=[UserPromptPart(content=f"[历史对话]\nA: 1\n\n[用户发言]\n你好\n{delta}")])
    _relean_user_turn([full_req], "[用户发言]\n你好")
    assert isinstance(full_req.parts[0], UserPromptPart)
    assert full_req.parts[0].content == "[用户发言]\n你好"

    mm_req = ModelRequest(
        parts=[
            UserPromptPart(
                content=[f"[历史对话]\nA: 1\n\n[用户发言]\n看图\n{delta}", ImageUrl(url="https://x.invalid/a.png")]
            )
        ]
    )
    _relean_user_turn([mm_req], "[用户发言]\n看图")
    assert isinstance(mm_req.parts[0], UserPromptPart)
    assert isinstance(mm_req.parts[0].content, list)
    assert mm_req.parts[0].content[0] == "[用户发言]\n看图"
    assert isinstance(mm_req.parts[0].content[1], ImageUrl)

    assert tool_return_is_gate_reject(TOOLS_EXECUTION_GATED_DENY) is True

    eps: list[Episode] = [
        {
            "id": f"e{i}",
            "content": f"无关日常片段 {i}",
            "valid_at": "",
            "scope_key": "group:group_1",
            "embedding": [],
            "score": 0.0208 - i * 0.0003,
        }
        for i in range(6)
    ]
    assert len(_filter_point_lookup_episodes(eps, "今天天气如何", dense_anchor_cap=2)) == 2

    stopword_pending: Episode = {
        "id": "p_stop",
        "content": "测试用户A: 今天午饭吃面",
        "valid_at": "",
        "scope_key": "group:group_1",
        "embedding": [],
    }
    kept_after_stop = _filter_point_lookup_episodes([*eps, stopword_pending], "今天天气如何", dense_anchor_cap=2)
    assert [e["id"] for e in kept_after_stop] == ["e0", "e1"]

    substantive_pending: Episode = {
        "id": "p_hit",
        "content": "测试用户A: 猫粮牌子选了测试款",
        "valid_at": "",
        "scope_key": "group:group_1",
        "embedding": [],
    }
    kept_with_hit = _filter_point_lookup_episodes([*eps, substantive_pending], "今天猫粮牌子是什么", dense_anchor_cap=2)
    assert [e["id"] for e in kept_with_hit] == ["p_hit", "e0", "e1"]

    clear_openai_wire_facts()
    try:
        import json as _json

        import httpx2
        from openai import AsyncOpenAI
        from pydantic_ai.tools import ToolDefinition
        from pydantic_ai.messages import TextPart, ModelMessage
        from pydantic_ai.providers.openai import OpenAIProvider

        base_prof = openai_user_thinking_profile("max")(openai_model_profile("MiniMax-M3.1-Flash"))
        assert "supports_thinking" in base_prof and base_prof["supports_thinking"] is True
        err_none = ModelHTTPError(
            status_code=400,
            model_name="MiniMax-M3.1-Flash",
            body={"error": {"message": "invalid reasoning_effort: none; expected low, medium, high, max"}},
        )
        rev1 = revise_openai_attempt(
            "https://api.minimaxi.com/v1",
            "MiniMax-M3.1-Flash",
            err_none,
            None,
            ModelRequestParameters(thinking=False),
        )
        assert rev1 is not None
        learned_prof = openai_profile_for_model("https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash")(base_prof)
        assert "openai_reasoning_efforts" in learned_prof
        assert learned_prof["openai_reasoning_efforts"] == frozenset({"low", "medium", "high", "max"})

        # "strictly positive" 不得误判为 strict 工具错误
        err_strictly = ModelHTTPError(
            status_code=400,
            model_name="test/model-flash",
            body={"error": {"message": "temperature must be strictly positive"}},
        )
        assert (
            revise_openai_attempt(
                "https://gateway.invalid/v1",
                "test/model-flash",
                err_strictly,
                None,
                ModelRequestParameters(),
            )
            is None
        )

        # 端到端校验：混传 strict/non-strict 工具触发 400 后就地去 strict 重试，且跨实例失效旧 profile 缓存
        sent_tools_strict: list[tuple[str, dict[str, bool]]] = []

        def _strict_handler(request: httpx2.Request) -> httpx2.Response:
            payload = _json.loads(request.content.decode("utf-8"))
            assert isinstance(payload, dict) and "tools" in payload and isinstance(payload["tools"], list)
            path = request.url.path
            flags: dict[str, bool] = {}
            for item in payload["tools"]:
                assert isinstance(item, dict)
                if path.endswith("/responses"):
                    assert isinstance(item["name"], str)
                    flags[item["name"]] = "strict" in item and bool(item["strict"])
                else:
                    assert "function" in item and isinstance(item["function"], dict)
                    fn = item["function"]
                    assert isinstance(fn["name"], str)
                    flags[fn["name"]] = "strict" in fn and bool(fn["strict"])
            sent_tools_strict.append((path, flags))
            if any(flags.values()) and not all(flags.values()):
                nested = _json.dumps(
                    {
                        "error": {
                            "message": (
                                'Prefill server error (400 Bad Request): {"object":"error",'
                                '"message":"Cannot use strict tool with non-strict tool in the same request",'
                                '"type":"BadRequest","param":null,"code":400}'
                            ),
                            "type": "invalid_request_error",
                        }
                    }
                )
                return httpx2.Response(
                    400,
                    json={"message": nested, "type": "invalid_request_error"},
                )
            if path.endswith("/responses"):
                return httpx2.Response(
                    200,
                    json={
                        "id": "resp-test",
                        "object": "response",
                        "created_at": 1700000000,
                        "status": "completed",
                        "model": "test/model-flash",
                        "output": [
                            {
                                "id": "msg-test",
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "ok-resp", "annotations": []}],
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                    },
                )
            if "stream" in payload and payload["stream"] is True:
                chunk = {
                    "id": "chatcmpl-stream",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": "test/model-flash",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "ok-stream"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }
                sse = f"data: {_json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")
                return httpx2.Response(200, content=sse, headers={"content-type": "text/event-stream"})
            return httpx2.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": "test/model-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                },
            )

        gw_url = "https://gateway.invalid/v1"
        gw_model = "test/model-flash"
        wire_prof = openai_profile_for_model(gw_url, gw_model)

        async def _run_strict_recovery() -> None:
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(_strict_handler)) as hc:
                client = AsyncOpenAI(api_key="k", base_url=gw_url, max_retries=0, http_client=hc)
                provider = OpenAIProvider(openai_client=client)
                stream_m = AutoUsageOpenAIChatModel(gw_model, provider=provider, profile=wire_prof)
                # 另一个在 400 之前就缓存过 profile 的同网关模型实例（Chat 与 Responses）
                sibling_m = RecoveringOpenAIChatModel(gw_model, provider=provider, profile=wire_prof)
                _ = sibling_m.profile
                resp_m = RecoveringOpenAIResponsesModel(gw_model, provider=provider, profile=wire_prof)
                _ = resp_m.profile

                mixed_tools = [
                    ToolDefinition(
                        name="all_required_tool",
                        description="all params required",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                            "required": ["q"],
                        },
                    ),
                    ToolDefinition(
                        name="optional_param_tool",
                        description="has optional param",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
                            "required": ["q"],
                        },
                    ),
                ]
                req_params = ModelRequestParameters(function_tools=mixed_tools)
                msg: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hi")])]

                # 1) 生产主路径 request_stream：首包混传 400 → 就地去 strict 重试成功
                prepped_settings, prepped_params = stream_m.prepare_request(None, req_params)
                async with stream_m.request_stream(msg, prepped_settings, prepped_params) as stream:
                    async for _ in stream:
                        pass
                    streamed = stream.get()
                assert streamed.parts and isinstance(streamed.parts[0], TextPart)
                assert streamed.parts[0].content == "ok-stream"

                # 2) 跨实例验证：sibling_m 与 resp_m 的旧 profile 缓存自动失效，首包即零 400
                sib_settings, sib_params = sibling_m.prepare_request(None, req_params)
                sib_res = await sibling_m.request(msg, sib_settings, sib_params)
                assert sib_res.parts and isinstance(sib_res.parts[0], TextPart) and sib_res.parts[0].content == "ok"

                resp_settings, resp_params = resp_m.prepare_request(None, req_params)
                resp_res = await resp_m.request(msg, resp_settings, resp_params)
                assert resp_res.parts and isinstance(resp_res.parts[0], TextPart)
                assert resp_res.parts[0].content == "ok-resp"

        asyncio.run(_run_strict_recovery())
        assert sent_tools_strict == [
            ("/v1/chat/completions", {"all_required_tool": True, "optional_param_tool": False}),
            ("/v1/chat/completions", {"all_required_tool": False, "optional_param_tool": False}),
            ("/v1/chat/completions", {"all_required_tool": False, "optional_param_tool": False}),
            ("/v1/responses", {"all_required_tool": False, "optional_param_tool": False}),
        ]

        # 已学到 disable_strict_tools 后再次收到同错不得无限重试
        err_strict_again = ModelHTTPError(
            status_code=400,
            model_name=gw_model,
            body={"error": {"message": "Cannot use strict tool with non-strict tool in the same request"}},
        )
        assert revise_openai_attempt(gw_url, gw_model, err_strict_again, None, ModelRequestParameters()) is None
    finally:
        clear_openai_wire_facts()

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.planning.handle_resolver import ResolvedHandle
    from gsuid_core.ai_core.planning.tool_output_tools import _tool_output_access_allowed
    from gsuid_core.ai_core.planning.tool_output_helper import should_fold_for_model

    assert should_fold_for_model("x" * 4500, tool_name="web_search_tool", is_group=True, is_capability=True) is True
    assert should_fold_for_model("x" * 3500, tool_name="web_search_tool", is_group=True, is_capability=True) is False

    grp_handle = ResolvedHandle(
        id="to_test000001",
        source="tool_output",
        mime="text/plain",
        summary="测试摘要",
        owner_user_id="",
        scope_key="group_1",
        payload_inline="正文",
        payload_path="",
        size_bytes=6,
    )
    test_model = TestModel()
    ctx_same_grp = RunContext(
        deps=ToolContext(ev=Event(bot_id="test", user_id="", group_id="group_1")),
        model=test_model,
        usage=RunUsage(),
    )
    ctx_other_grp = RunContext(
        deps=ToolContext(ev=Event(bot_id="test", user_id="", group_id="group_2")),
        model=test_model,
        usage=RunUsage(),
    )
    assert _tool_output_access_allowed(grp_handle, ctx_same_grp) is True
    assert _tool_output_access_allowed(grp_handle, ctx_other_grp) is False


def test_render_delta_quote_edges_and_merge() -> None:
    """线程视图：引用边显式标出；同人连发合并但带引用的单独成行。"""
    from gsuid_core.message_history import MessageRecord
    from gsuid_core.ai_core.thread_view import render_delta
    from gsuid_core.message_history.link import EventLink

    base = 1_780_000_000.0

    def user_rec(
        seq: int,
        uid: str,
        name: str,
        text: str,
        reply_to_seq: int | None = None,
        reply_to_author: str | None = None,
        at_self: bool = False,
    ) -> MessageRecord:
        return MessageRecord(
            role="user",
            content=text,
            user_id=uid,
            user_name=name,
            timestamp=base + seq * 8,
            metadata={"user_type": "group", "msg_id": f"m{seq}"},
            link=EventLink(
                seq=seq,
                msg_id=f"m{seq}",
                reply_to_seq=reply_to_seq,
                reply_to_author=reply_to_author,
                at_self=at_self,
            ),
        )

    records = [
        user_rec(1, "100000003", "测试用户C", "pil活"),
        user_rec(2, "100000003", "测试用户C", "让你画图"),
        user_rec(3, "100000002", "测试用户B", "ぴー活是什么意思", at_self=True),
        user_rec(4, "100000002", "测试用户B", "神了", reply_to_seq=2, reply_to_author="100000003"),
        MessageRecord(
            role="assistant",
            content="唔…P活一般是パパ活的缩写，简单说就是…",
            user_id="100000002",
            user_name="测试人格",
            timestamp=base + 5 * 8,
            metadata={
                "msg_id": "b5",
                "speech_channel": "persona",
                "persona_name": "测试人格",
                "reply_to_user_id": "100000002",
                "reply_to_user_name": "测试用户B",
            },
            link=EventLink(seq=5, msg_id="b5", in_reply_to_seqs=(3,), seen_seq=4),
        ),
        user_rec(6, "100000004", "测试用户D", "谁叫汉堡", reply_to_seq=5, reply_to_author="self"),
        user_rec(7, "100000004", "测试用户D", "x2"),
        user_rec(8, "100000004", "测试用户D", "x3"),
    ]

    out = render_delta(
        records,
        from_seq=0,
        head_seq=8,
        backfill=False,
        pending_seqs=frozenset({4, 6}),
        persona_name="测试人格",
        private=False,
    )
    lines = out.splitlines()

    quote_idx = next(i for i, ln in enumerate(lines) if "神了" in ln)
    sent_idx = next(i for i, ln in enumerate(lines) if "〔已发〕" in ln)
    assert quote_idx < sent_idx
    assert "↩#2(测试用户C" in lines[quote_idx]

    quoted = next(ln for ln in lines if "谁叫汉堡" in ln)
    assert quoted.startswith("#6 ") and "↩#5(" in quoted
    # 旁观引用渲染：含原收件人与时间差，且无指令性措辞（§5.2）
    assert "↩#5(你→测试用户B" in quoted and "前 · 闲聊回复" in quoted
    for w in ("要回", "应该", "请回", "必须", "不要"):
        assert w not in quoted
    merged = next(ln for ln in lines if ln.startswith("#7–#8"))
    assert "x2 / x3" in merged

    # record.metadata 不含 session_id 时，render_delta 与 render_router_view 均能透传渲染〔依据〕标签
    import time as _time

    from gsuid_core.models import Event
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry
    from gsuid_core.ai_core.thread_view import render_router_view
    from gsuid_core.ai_core.evidence.store import Observation, get_evidence_store

    sess_id = "OneBot:onebot:900000001:group:70001"
    store = get_evidence_store(sess_id)
    store.observations.clear()
    obs_now = _time.time()
    obs = Observation(
        obs_id="ob_basis01",
        session_id=sess_id,
        tool_name="daily_checkin",
        service="TestPlugin",
        requested_by="100000002",
        requested_by_seq=3,
        credential_owner="100000002",
        outcome="data",
        observed_at=obs_now,
        summary="已完成签到",
        handle="",
    )
    store.append(obs)
    try:
        ev_records = [
            *records[:4],
            MessageRecord(
                role="assistant",
                content="已帮你完成签到啦",
                user_id="900000001",
                user_name="测试人格",
                timestamp=base + 42,
                metadata={
                    "msg_id": "b_ev",
                    "bot_id": "OneBot",
                    "speech_channel": "persona",
                    "persona_name": "测试人格",
                    "reply_to_user_id": "100000002",
                    "reply_to_user_name": "测试用户B",
                },
                link=EventLink(seq=5, msg_id="b_ev", in_reply_to_seqs=(3,), seen_seq=4, evidence=("ob_basis01",)),
            ),
            user_rec(6, "100000004", "测试用户D", "测试人格，帮我签到"),
        ]
        delta_with_ev = render_delta(
            ev_records,
            from_seq=0,
            head_seq=6,
            backfill=False,
            pending_seqs=(6,),
            persona_name="测试人格",
            private=False,
            session_id=sess_id,
        )
        turn_ev = InboxTurn(
            session_id=sess_id,
            persona_name="测试人格",
            entries=(
                PendingEntry(
                    WakeItem(6, "100000004", "name", "hard", base + 48),
                    Event(
                        "OneBot", "900000001", "m6", "group", "70001", "100000004", {}, 6, raw_text="测试人格，帮我签到"
                    ),
                    persona_name="测试人格",
                ),
            ),
            from_seq=5,
            head_seq=6,
        )
        from gsuid_core.ai_core.thread_view import render_window as _render_window_ev

        router_with_ev = render_router_view(turn_ev, ev_records)
        win_with_ev = _render_window_ev(ev_records, persona_name="测试人格", session_id=sess_id)
        assert "〔依据: 替测试用户B查" in delta_with_ev
        assert "〔依据: 替测试用户B查" in router_with_ev
        assert "〔依据: 替测试用户B查" in win_with_ev
    finally:
        store.observations.clear()

    # 静默轮记入 A 轨的 dlg_ 委派句柄标记为 tool_delivery，绝不渲染成「你: dlg_…〔已发〕」角色台词
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.thread_view import render_window
    from gsuid_core.ai_core.history_format import is_persona_speech, remember_silence_handles

    hm = get_history_manager()
    ev_dlg = Event("OneBot", "900000001", "dlg_m1", "group", "group_dlg_1", "100000001", {}, 6)
    hm.clear_history(ev_dlg)
    try:
        hm.add_message(ev_dlg, role="user", content="帮我画一张测试指标图", user_name="测试用户A")
        remember_silence_handles(ev_dlg, ["dlg_2f7fe0a6-db1c-4aff-8195-b80c721ae644"], manager=hm)
        stored = hm.get_history(ev_dlg)
        assert len(stored) == 2 and not is_persona_speech(stored[-1])
        win = render_window(stored, persona_name="测试人格")
        assert "dlg_2f7fe0a6" not in win
    finally:
        hm.clear_history(ev_dlg)

    # 群聊统一切片：30 分钟前无关命令回执裁掉、连续 4 条同人重复折叠为 (×4)、
    # TurnRouter 视图正确标注 receipt 与 quote_self 归属及 [场景] 行
    from gsuid_core.ai_core.inbox.state import ReceiptContext

    t_now = base + 7200.0
    slice_records = [
        MessageRecord(
            role="user",
            content="旧指令触发",
            user_id="100000099",
            user_name="测试用户Z",
            timestamp=t_now - 3600.0,
            metadata={"user_type": "group", "msg_id": "old1", "command_hit": True},
            link=EventLink(seq=10, msg_id="old1"),
        ),
        MessageRecord(
            role="assistant",
            content="[图片]",
            user_id="900000001",
            user_name="测试人格",
            timestamp=t_now - 3595.0,
            metadata={
                "msg_id": "old2",
                "speech_channel": "command_aside",
                "persona_name": "测试人格",
                "reply_to_user_id": "100000099",
                "reply_to_user_name": "测试用户Z",
            },
            link=EventLink(seq=11, msg_id="old2"),
        ),
        MessageRecord(
            role="assistant",
            content="这是之前回复给测试用户A 的内容",
            user_id="900000001",
            user_name="测试人格",
            timestamp=t_now - 90.0,
            metadata={
                "msg_id": "bot12",
                "speech_channel": "persona",
                "persona_name": "测试人格",
                "reply_to_user_id": "100000001",
                "reply_to_user_name": "测试用户A",
            },
            link=EventLink(seq=12, msg_id="bot12", in_reply_to_seqs=(10,)),
        ),
        *[
            MessageRecord(
                role="user",
                content="哈哈",
                user_id="100000002",
                user_name="测试用户B",
                timestamp=t_now - 60.0 + idx * 5.0,
                metadata={"user_type": "group", "msg_id": f"rep_{idx}"},
                link=EventLink(seq=13 + idx, msg_id=f"rep_{idx}", reply_to_seq=12, reply_to_author="self"),
            )
            for idx in range(4)
        ],
        MessageRecord(
            role="user",
            content="测试一下这个",
            user_id="100000001",
            user_name="测试用户A",
            timestamp=t_now,
            metadata={"user_type": "group", "msg_id": "q17"},
            link=EventLink(seq=17, msg_id="q17", reply_to_seq=12, reply_to_author="self"),
        ),
    ]
    sliced_delta = render_delta(
        slice_records,
        from_seq=9,
        head_seq=17,
        backfill=True,
        pending_seqs=(17,),
        persona_name="测试人格",
        private=False,
    )
    assert "#10 " not in sliced_delta and "#11 " not in sliced_delta
    assert "(×4)" in sliced_delta and "#17 " in sliced_delta

    # 私聊冷启动 backfill 不受 30 分钟群聊断层限制；无引用同人连发 ≥3 相同正文合行亦折叠为 (×N)
    dm_old_records = [
        MessageRecord(
            role="user",
            content="两小时前的私聊问题",
            user_id="100000001",
            user_name="测试用户A",
            timestamp=t_now - 7200.0,
            metadata={"user_type": "direct", "msg_id": "dm1"},
            link=EventLink(seq=1, msg_id="dm1"),
        ),
        MessageRecord(
            role="user",
            content="继续聊",
            user_id="100000001",
            user_name="测试用户A",
            timestamp=t_now,
            metadata={"user_type": "direct", "msg_id": "dm2"},
            link=EventLink(seq=2, msg_id="dm2"),
        ),
    ]
    dm_delta = render_delta(
        dm_old_records, from_seq=1, head_seq=2, backfill=True, pending_seqs=(2,), persona_name="测试人格", private=True
    )
    assert "两小时前的私聊问题" in dm_delta

    unquoted_spam = [
        MessageRecord(
            role="user",
            content="刷屏词",
            user_id="100000002",
            user_name="测试用户B",
            timestamp=t_now - 30.0 + idx * 2.0,
            metadata={"user_type": "group", "msg_id": f"sp_{idx}"},
            link=EventLink(seq=20 + idx, msg_id=f"sp_{idx}"),
        )
        for idx in range(4)
    ] + [
        MessageRecord(
            role="user",
            content="正经问题",
            user_id="100000001",
            user_name="测试用户A",
            timestamp=t_now,
            metadata={"user_type": "group", "msg_id": "sp_q"},
            link=EventLink(seq=25, msg_id="sp_q"),
        )
    ]
    spam_delta = render_delta(
        unquoted_spam,
        from_seq=19,
        head_seq=25,
        backfill=False,
        pending_seqs=(25,),
        persona_name="测试人格",
        private=False,
    )
    assert "刷屏词 (×4)" in spam_delta

    turn_qself = InboxTurn(
        session_id="OneBot:onebot:900000001:group:70002",
        persona_name="测试人格",
        entries=(
            PendingEntry(
                WakeItem(17, "100000001", "quote_self", "soft", t_now),
                Event("OneBot", "900000001", "q17", "group", "70002", "100000001", {}, 0, raw_text="测试一下这个"),
                persona_name="测试人格",
            ),
        ),
        from_seq=16,
        head_seq=17,
    )
    rview_q = render_router_view(turn_qself, slice_records, relation_label="主人", group_rate_1m=5)
    assert "引用对象=你→本人" in rview_q
    assert "[场景] 关系=主人 · 近1分钟群消息=5条 · 触发=quote_self" in rview_q
    assert "(用户ID:" not in rview_q

    turn_rcpt = InboxTurn(
        session_id="OneBot:onebot:900000001:group:70002",
        persona_name="测试人格",
        entries=(
            PendingEntry(
                WakeItem(11, "100000099", "receipt", "soft", t_now),
                Event("OneBot", "900000001", "old1", "group", "70002", "100000099", {}, 6, raw_text="旧指令触发"),
                receipt=ReceiptContext(None, "测试插件", "entry.py", "错误码 -400016"),
                persona_name="测试人格",
            ),
        ),
        from_seq=10,
        head_seq=11,
    )
    rview_rcpt = render_router_view(turn_rcpt, slice_records)
    assert "★#11 receipt · 插件=测试插件" in rview_rcpt and "你上次回他是" not in rview_rcpt

    # 主 Agent 待回应块对命令回执不得退化为「AI(用户ID:...)」
    import gsuid_core.ai_core.outbound as _outbound_mod
    from gsuid_core.ai_core.outbound import QuoteResolve
    from gsuid_core.ai_core.inbox.blocks import quote_fact_lines, render_pending_block

    pblock_rcpt = render_pending_block(turn_rcpt, slice_records)
    assert "★#11 [命令回执] 插件=测试插件" in pblock_rcpt and "AI(" not in pblock_rcpt

    # 窗口外引用（reply_to_seq=None）仍必须触发 resolve_quote 补回出站事实行
    out_of_win_ev = Event(
        "OneBot",
        "900000001",
        "q_oow",
        "group",
        "70002",
        "100000001",
        {},
        0,
        raw_text="这",
        reply_id="missing_mid",
        reply="测试播报内容",
    )
    turn_oow = InboxTurn(
        session_id="OneBot:onebot:900000001:group:70002",
        persona_name="测试人格",
        entries=(
            PendingEntry(
                WakeItem(18, "100000001", "quote_self", "soft", t_now), out_of_win_ev, persona_name="测试人格"
            ),
        ),
        from_seq=17,
        head_seq=18,
    )
    oow_rec = MessageRecord(
        role="user",
        content="这",
        user_id="100000001",
        user_name="测试用户A",
        timestamp=t_now,
        metadata={"msg_id": "q_oow", "reply_id": "missing_mid", "reply_preview": "测试播报内容"},
        link=EventLink(seq=18, msg_id="q_oow", reply_to_seq=None, reply_to_author="self"),
    )
    orig_resolve_quote = _outbound_mod.resolve_quote

    async def _fake_resolve_quote(_ev: Event) -> QuoteResolve:
        return QuoteResolve(
            line="（被引用的是你于 14:40 发给群内的「测试播报」）", topic="测试播报", handle="", target="群内"
        )

    _outbound_mod.resolve_quote = _fake_resolve_quote
    try:
        qfacts = asyncio.run(quote_fact_lines(turn_oow, [*slice_records, oow_rec]))
    finally:
        _outbound_mod.resolve_quote = orig_resolve_quote
    assert qfacts == ["★#18 （被引用的是你于 14:40 发给群内的「测试播报」）"]

    # 日志经历去重 / 偏好 2-gram 去重 / 空摘要观测过滤
    from pydantic_ai.messages import TextPart, ModelResponse

    from gsuid_core.ai_core.self_cognition import collect_speaker_preference_rules
    from gsuid_core.ai_core.agent_run.prepare import _journal_delta_in_recent_history

    hist_j = [ModelResponse(parts=[TextPart(content="刚刚帮测试用户A查了今日天气，结果是晴天")])]
    assert _journal_delta_in_recent_history("刚刚帮测试用户A查了今日天气，结果是晴天", hist_j) is True
    assert _journal_delta_in_recent_history("完全不同的另一件新日程安排记录", hist_j) is False
    two_seg = (
        "（系统提示：刚刚经历：[12:00 | 闲聊] 刚刚帮测试用户A查了今日天气，结果是晴天；"
        "[12:01 | 闲聊] 全新的另一条未复述事项记录）"
    )
    assert _journal_delta_in_recent_history(two_seg, hist_j) is False

    dedup_prefs = collect_speaker_preference_rules(
        [],
        [
            ("用户(user_id:100000001)对测试人格使用昵称小柚子来称呼", "naming", False),
            ("用户(user_id:100000001)平时对测试人格使用昵称小柚子来称呼", "general", False),
            ("用户(user_id:100000001)喜欢喝热美式咖啡", "general", False),
        ],
        "100000001",
        limit=6,
    )
    assert len(dedup_prefs) == 2

    # 源头拒写空壳 read_image 观测，避免污染证据存储与渲染
    from gsuid_core.ai_core.inbox.turn import bind_turn, reset_turn
    from gsuid_core.ai_core.evidence.capture import record_tool_result

    empty_sess = "OneBot:onebot:900000001:group:70098_ev"
    empty_store = get_evidence_store(empty_sess)
    empty_store.observations.clear()
    tok_ev = bind_turn(
        InboxTurn(
            session_id=empty_sess,
            persona_name="测试人格",
            entries=(
                PendingEntry(
                    WakeItem(1, "100000001", "mention", "hard", obs_now),
                    Event("OneBot", "900000001", "ev1", "group", "70098", "100000001", {}, 6),
                    persona_name="测试人格",
                ),
            ),
            from_seq=0,
            head_seq=1,
        )
    )
    try:
        record_tool_result("read_image", b"raw-bytes", None)
        assert not empty_store.observations
    finally:
        reset_turn(tok_ev)
        empty_store.observations.clear()


def test_phase2a_history_roll_invisible_name_and_later_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR 2A：过期前缀阶梯滚出 + 主会话 max_history 护栏 + 隐形昵称清洗 + 滞后追问已交付标注。"""
    from datetime import datetime, timezone, timedelta

    from pydantic_ai.messages import (
        TextPart,
        ModelRequest,
        ModelResponse,
        UserPromptPart,
        SystemPromptPart,
    )
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.models import Event
    from gsuid_core.ai_core.utils import sanitize_display_name, _build_relationship_description
    from gsuid_core.message_history import MessageRecord
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry
    from gsuid_core.ai_core.thread_view import render_delta
    from gsuid_core.ai_core.inbox.blocks import render_pending_block
    from gsuid_core.message_history.link import EventLink
    from gsuid_core.ai_core.history_compact import compact_session_history
    from gsuid_core.ai_core.configs.ai_config import ai_config

    # 1. P2-1：活跃窗内严格保头；3 小时过期前缀阶梯滚出但原样保留 SystemPromptPart
    now_dt = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
    old_dt = now_dt - timedelta(hours=3)
    fresh_dt = now_dt - timedelta(minutes=2)

    def _make_hist(head_dt: datetime) -> list[ModelRequest | ModelResponse]:
        msgs: list[ModelRequest | ModelResponse] = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="固定系统提示词", timestamp=head_dt),
                    UserPromptPart(content="三小时前大盘走势", timestamp=head_dt),
                ]
            ),
            ModelResponse(parts=[TextPart(content="三小时前大盘回复")], timestamp=head_dt),
        ]
        for i in range(1, 10):
            ts = fresh_dt + timedelta(seconds=i * 5)
            msgs.append(ModelRequest(parts=[UserPromptPart(content=f"近期提问{i}", timestamp=ts)]))
            msgs.append(ModelResponse(parts=[TextPart(content=f"近期回复{i}")], timestamp=ts))
        return msgs

    # 活跃期内：保头裁中段（首轮仍在）
    fresh_compacted, did_fresh = compact_session_history(
        _make_hist(fresh_dt), max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert did_fresh is True
    assert isinstance(fresh_compacted[0], ModelRequest)
    assert any(isinstance(p, UserPromptPart) and p.content == "三小时前大盘走势" for p in fresh_compacted[0].parts)

    # 超 30 分钟过期前缀：阶梯滚出三小时前旧题，但保留字节一致的 SystemPromptPart
    rolled, did_roll = compact_session_history(
        _make_hist(old_dt), max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert did_roll is True
    assert isinstance(rolled[0], ModelRequest)
    assert any(isinstance(p, SystemPromptPart) and p.content == "固定系统提示词" for p in rolled[0].parts)
    assert not any(
        isinstance(m, ModelRequest)
        and any(isinstance(p, UserPromptPart) and p.content == "三小时前大盘走势" for p in m.parts)
        for m in rolled
    )
    assert any(
        isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) and p.content == "近期提问9" for p in m.parts)
        for m in rolled
    )

    # 无时区 naive 时间戳按 UTC 归一化，不因本地时区偏移误触发或漏触发滚出
    naive_fresh, _ = compact_session_history(
        _make_hist(datetime(2026, 11, 24, 11, 58, 0)),
        max_history=12,
        trim_ratio=0.6,
        max_prefix_age_s=1800.0,
        now_ts=now_dt.timestamp(),
    )
    assert isinstance(naive_fresh[0], ModelRequest)
    assert any(isinstance(p, UserPromptPart) and p.content == "三小时前大盘走势" for p in naive_fresh[0].parts)
    naive_old, _ = compact_session_history(
        _make_hist(datetime(2026, 11, 24, 9, 0, 0)),
        max_history=12,
        trim_ratio=0.6,
        max_prefix_age_s=1800.0,
        now_ts=now_dt.timestamp(),
    )
    assert not any(
        isinstance(m, ModelRequest)
        and any(isinstance(p, UserPromptPart) and p.content == "三小时前大盘走势" for p in m.parts)
        for m in naive_old
    )

    # 极端单轮长工具链（无第二条干净 UserPrompt）：不产出 SystemPrompt -> ModelResponse(ToolCall)
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    single_turn_tools: list[ModelRequest | ModelResponse] = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="固定系统提示词", timestamp=old_dt),
                UserPromptPart(content="超长单轮工具提问", timestamp=old_dt),
            ]
        )
    ]
    for i in range(8):
        ts = fresh_dt + timedelta(seconds=i * 2)
        single_turn_tools.append(
            ModelResponse(
                parts=[ToolCallPart(tool_name="demo_tool", args={"i": i}, tool_call_id=f"c{i}")], timestamp=ts
            )
        )
        single_turn_tools.append(
            ModelRequest(
                parts=[ToolReturnPart(tool_name="demo_tool", content=f"ok{i}", tool_call_id=f"c{i}", timestamp=ts)]
            )
        )
    safe_tools_hist, _ = compact_session_history(
        single_turn_tools, max_history=10, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert isinstance(safe_tools_hist[0], ModelRequest)
    assert any(isinstance(p, UserPromptPart) for p in safe_tools_hist[0].parts)

    # 滚出过期首轮后若靠前切点仍超 max_history，必须继续保头裁中段压回上限以内
    oversized_after_roll: list[ModelRequest | ModelResponse] = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="固定系统提示词", timestamp=old_dt),
                UserPromptPart(content="过期旧提问", timestamp=old_dt),
            ]
        ),
        ModelResponse(parts=[TextPart(content="过期旧回复")], timestamp=old_dt),
        ModelRequest(parts=[UserPromptPart(content="后续唯一干净提问", timestamp=fresh_dt)]),
    ]
    for i in range(10):
        ts = fresh_dt + timedelta(seconds=i * 2)
        oversized_after_roll.append(
            ModelResponse(
                parts=[ToolCallPart(tool_name="demo_tool", args={"i": i}, tool_call_id=f"r{i}")],
                timestamp=ts,
            )
        )
        oversized_after_roll.append(
            ModelRequest(
                parts=[ToolReturnPart(tool_name="demo_tool", content=f"ok{i}", tool_call_id=f"r{i}", timestamp=ts)]
            )
        )
    capped_rolled, did_cap_roll = compact_session_history(
        oversized_after_roll, max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert did_cap_roll is True and len(capped_rolled) <= 12
    assert not any(
        isinstance(m, ModelRequest)
        and any(isinstance(p, UserPromptPart) and p.content == "过期旧提问" for p in m.parts)
        for m in capped_rolled
    )
    assert any(
        isinstance(m, ModelRequest)
        and any(isinstance(p, UserPromptPart) and p.content == "后续唯一干净提问" for p in m.parts)
        for m in capped_rolled
    )

    # 主对话尊重配置 agent_max_history（不强行抬高到 40），显式入参照常生效；滚出过期首轮时置位补锚
    from gsuid_core.ai_core.agent_run.support import _ensure_session_anchor_on_first_user

    monkeypatch.setattr(ai_config.get_config("agent_max_history"), "data", 15)
    dummy_model = OpenAIChatModel("test-offline", provider=OpenAIProvider(api_key="test"))
    agent_main = GsCoreAIAgent(openai_chat_model=dummy_model, system_prompt="固定系统提示词", create_by="Chat")
    assert agent_main.max_history == 15
    agent_explicit = GsCoreAIAgent(openai_chat_model=dummy_model, system_prompt="s", create_by="Chat", max_history=12)
    assert agent_explicit.max_history == 12
    agent_main.history = _make_hist(old_dt)
    agent_main.thread_backfill_due = False
    agent_main._session_anchor_due = False
    agent_main.extract_history()
    assert agent_main.thread_backfill_due is True
    assert agent_main._session_anchor_due is True
    re_cur, re_lean, re_where, _ = _ensure_session_anchor_on_first_user(
        agent_main.history,
        "新回合消息",
        "新回合消息",
        persona_name="",
        marker="【角色沉浸要求】测试",
        force_reanchor=agent_main._session_anchor_due,
    )
    assert re_where == "current" and "【角色沉浸要求】测试" in re_cur and "【角色沉浸要求】测试" in re_lean

    # 2. P2-2：隐形字符昵称清洗（正常带零宽洗干净；纯隐形回落群友_尾号；UID+零宽与纯空均保留用户ID契约）
    assert sanitize_display_name("小\u200b明\ufeff", "100000001") == "小明"
    assert sanitize_display_name("\u200b\u3164\u2800", "100000001") == "群友_0001"
    assert sanitize_display_name("100000001\u200b", "100000001") == ""
    assert sanitize_display_name("", "100000001") == ""
    assert "群友_0001(用户ID:100000001)" in _build_relationship_description("\u200b\u3164", "100000001")

    # 3. P2-3：滞后追问之后已有针对同用户的出站交付，待回应块显式标注且切片保留该出站行
    t0 = 1_760_000_000.0
    recs = [
        MessageRecord(
            role="user",
            content="查一下测试数据",
            user_id="100000001",
            user_name="\u200b\u3164",
            timestamp=t0,
            metadata={},
            link=EventLink(seq=10, msg_id="m10"),
        ),
        MessageRecord(
            role="user",
            content="好了没",
            user_id="100000001",
            user_name="\u200b\u3164",
            timestamp=t0 + 20,
            metadata={},
            link=EventLink(seq=11, msg_id="m11"),
        ),
        MessageRecord(
            role="assistant",
            content="测试数据图已发给你啦",
            user_id="bot",
            user_name="测试人格",
            timestamp=t0 + 30,
            metadata={"reply_to_user_id": "100000001", "persona_name": "测试人格"},
            link=EventLink(seq=12, msg_id="m12", in_reply_to_seqs=(10,)),
        ),
    ]
    ev_lag = Event("OneBot", "900000001", "m11", "group", "group_1", "100000001", {}, 6, raw_text="好了没")
    turn_lag = InboxTurn(
        session_id="OneBot:onebot:900000001:group:group_1",
        persona_name="测试人格",
        entries=(PendingEntry(WakeItem(11, "100000001", "mention", "hard", t0 + 20), ev_lag, persona_name="测试人格"),),
        from_seq=9,
        head_seq=12,
    )
    pending_txt = render_pending_block(turn_lag, recs)
    assert "群友_0001(用户ID:100000001)" in pending_txt
    assert "在该条(#11)之后你已于 #12 向其发过回复/交付" in pending_txt
    assert "你上次回他是 #12" not in pending_txt

    delta_txt = render_delta(
        recs,
        from_seq=9,
        head_seq=12,
        backfill=False,
        pending_seqs=frozenset({11}),
        persona_name="测试人格",
        private=False,
    )
    assert "群友_0001(用户ID:100000001)" in delta_txt
    assert "#12 " in delta_txt

    # 同批存在更晚消息拉高 anchor_ts 超过 30 分钟时，已标注的交付行 #12 仍不得被 _SLICE_MAX_AGE_S 滤掉
    late_rec = MessageRecord(
        role="user",
        content="一小时后的新追问",
        user_id="100000002",
        user_name="测试用户B",
        timestamp=t0 + 3600,
        metadata={},
        link=EventLink(seq=13, msg_id="m13"),
    )
    delta_late = render_delta(
        [*recs, late_rec],
        from_seq=9,
        head_seq=13,
        backfill=False,
        pending_seqs=frozenset({11, 13}),
        persona_name="测试人格",
        private=False,
    )
    assert "#12 " in delta_late and "#13 " in delta_late


def test_hub_selection_disjoint_and_kanban_artifact_path_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """枢纽卡片展示与 extra_titles 严格不重叠；Kanban 删产物拒绝兄弟目录前缀越界。"""
    import gsuid_core.ai_core.cognition.hub as hub_mod
    from gsuid_core.ai_core.cognition.nodes import AICogNode
    from gsuid_core.ai_core.cognition.types import CogKind, CogScope, CognitiveHit
    from gsuid_core.ai_core.planning.kanban import _unlink_artifact_file

    nodes: dict[int, AICogNode] = {
        1: AICogNode(id=1, kind="entity", ref="world:demo:h1", title="测试专名实体"),
        2: AICogNode(id=2, kind="entity", ref="world:demo:h2", title="测试词条二"),
        3: AICogNode(id=3, kind="entity", ref="world:demo:h3", title="测试词条三"),
        4: AICogNode(id=4, kind="entity", ref="world:demo:h4", title="测试词条四"),
    }

    async def _get_by_id(nid: int) -> AICogNode | None:
        return nodes[nid] if nid in nodes else None

    async def _by_title(title: str) -> list[AICogNode]:
        return [nodes[1]] if title == "测试专名实体" else []

    monkeypatch.setattr(AICogNode, "get_by_id", staticmethod(_get_by_id))
    monkeypatch.setattr(AICogNode, "list_world_hubs_by_title", staticmethod(_by_title))
    monkeypatch.setattr(hub_mod, "lookup_surface", lambda _text: None)
    monkeypatch.setattr(hub_mod, "_rank_hubs_for_scope", lambda items, *_a, **_k: list(items))

    hits = [
        CognitiveHit(kind=CogKind.ENTITY, id="node_2", title="测试词条二", summary="", score=0.9),
        CognitiveHit(kind=CogKind.ENTITY, id="node_3", title="测试词条三", summary="", score=0.8),
        CognitiveHit(kind=CogKind.ENTITY, id="node_4", title="测试词条四", summary="", score=0.7),
    ]
    shown, extra_titles = asyncio.run(
        hub_mod._hubs_from_hits(hits, "测试专名实体", scope=CogScope(group_id="", user_id=""))
    )
    shown_titles = [n.title for n in shown]
    assert shown_titles == ["测试专名实体", "测试词条二"]
    assert extra_titles == ["测试词条三", "测试词条四"]
    assert set(shown_titles).isdisjoint(extra_titles)

    # Kanban 删单产物：兄弟目录 artifacts_other 拥有同字符串前缀，必须被 is_relative_to 拦截。
    artifact_root = tmp_path / "artifacts"
    inside = artifact_root / "root_1" / "out.txt"
    sibling = tmp_path / "artifacts_other" / "secret.txt"
    inside.parent.mkdir(parents=True, exist_ok=True)
    sibling.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("ok", encoding="utf-8")
    sibling.write_text("keep", encoding="utf-8")

    assert _unlink_artifact_file(str(sibling), artifact_root) is False
    assert sibling.exists()
    assert _unlink_artifact_file(str(inside), artifact_root) is True
    assert not inside.exists()
