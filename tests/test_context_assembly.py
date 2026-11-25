"""装配统一（§5.3）与上下文管线测试：AST 架构锁、动态块预算、历史压缩与线程视图。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from collections.abc import Sequence

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from tests._support import make_run_state
from gsuid_core.models import Event
from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.inbox.turn import InboxTurn
from gsuid_core.ai_core.inbox.state import Strength, WakeItem, WakeKind, PendingEntry, ReceiptContext
from gsuid_core.message_history.link import EventLink

_ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _ast_calls_and_imports(rel: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(_src(rel))
    calls: set[str] = set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
        elif isinstance(node, ast.Import):
            imports.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(a.name for a in node.names)
    return calls, imports


def _load_kits_once() -> None:
    from gsuid_core.ai_core.kits import occupants_of, load_enabled_kits

    if not occupants_of("mood"):
        asyncio.run(load_enabled_kits(run_init_steps=False))


def _user_rec(
    seq: int,
    uid: str,
    name: str,
    text: str,
    ts: float,
    *,
    msg_id: str | None = None,
    user_type: str = "group",
    reply_to_seq: int | None = None,
    reply_to_author: str | None = None,
    at_self: bool = False,
    extra_meta: dict[str, object] | None = None,
) -> MessageRecord:
    mid = msg_id if msg_id is not None else f"m{seq}"
    meta: dict[str, object] = {"user_type": user_type, "msg_id": mid}
    if extra_meta:
        meta.update(extra_meta)
    return MessageRecord(
        role="user",
        content=text,
        user_id=uid,
        user_name=name,
        timestamp=ts,
        metadata=meta,
        link=EventLink(
            seq=seq,
            msg_id=mid,
            reply_to_seq=reply_to_seq,
            reply_to_author=reply_to_author,
            at_self=at_self,
        ),
    )


def _bot_rec(
    seq: int,
    text: str,
    ts: float,
    *,
    msg_id: str,
    reply_to_uid: str,
    reply_to_name: str = "",
    in_reply_to: tuple[int, ...] = (),
    seen_seq: int | None = None,
    evidence: tuple[str, ...] = (),
    speech_channel: str = "persona",
    user_id: str = "900000001",
    persona: str = "测试人格",
) -> MessageRecord:
    meta: dict[str, object] = {
        "msg_id": msg_id,
        "speech_channel": speech_channel,
        "persona_name": persona,
        "reply_to_user_id": reply_to_uid,
    }
    if reply_to_name:
        meta["reply_to_user_name"] = reply_to_name
    return MessageRecord(
        role="assistant",
        content=text,
        user_id=user_id,
        user_name=persona,
        timestamp=ts,
        metadata=meta,
        link=EventLink(
            seq=seq,
            msg_id=msg_id,
            in_reply_to_seqs=in_reply_to,
            seen_seq=seen_seq,
            evidence=evidence,
        ),
    )


def _make_turn(
    seq: int,
    uid: str,
    kind: WakeKind,
    strength: Strength,
    ts: float,
    *,
    session_id: str = "OneBot:onebot:900000001:group:70002",
    group_id: str = "70002",
    msg_id: str | None = None,
    raw_text: str = "",
    user_pm: int = 6,
    from_seq: int | None = None,
    head_seq: int | None = None,
    persona: str = "测试人格",
    receipt: ReceiptContext | None = None,
    reply_id: str | None = None,
    reply: str | None = None,
) -> InboxTurn:
    ev = Event(
        "OneBot",
        "900000001",
        msg_id if msg_id is not None else f"m{seq}",
        "group",
        group_id,
        uid,
        {},
        user_pm,
        raw_text=raw_text,
        reply_id=reply_id,
        reply=reply,
    )
    return InboxTurn(
        session_id=session_id,
        persona_name=persona,
        entries=(PendingEntry(WakeItem(seq, uid, kind, strength, ts), ev, receipt=receipt, persona_name=persona),),
        from_seq=seq - 1 if from_seq is None else from_seq,
        head_seq=seq if head_seq is None else head_seq,
    )


def _user_texts(msgs: Sequence[ModelMessage]) -> list[str]:
    return [
        p.content
        for m in msgs
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart) and isinstance(p.content, str)
    ]


def test_both_entries_consume_shared_assembly() -> None:
    endpoint_calls, endpoint_imports = _ast_calls_and_imports("gsuid_core/webconsole/chat_with_history_api.py")
    assert {"run_interactive_turn", "build_session_system_prompt"} <= endpoint_calls
    assert endpoint_calls.isdisjoint({"dual_route_retrieve", "settle_turn"})
    assert endpoint_imports.isdisjoint({"dual_route_retrieve", "settle_turn", "classifier_service"})
    assert "build_session_system_prompt" in _ast_calls_and_imports("gsuid_core/ai_core/ai_router.py")[0]
    assert "assemble_dynamic_context" in _ast_calls_and_imports("gsuid_core/ai_core/handle_ai.py")[0]


def test_dynamic_context_ordering_and_suffix_policy() -> None:
    from gsuid_core.ai_core.hooks.models import AgentHookContext
    from gsuid_core.ai_core.hooks.points import AgentHookPoint
    from gsuid_core.ai_core.context_assembly import (
        _SUFFIX_PRODUCT_CAP,
        suffix_allowed_blocks,
        assemble_dynamic_context,
    )
    from gsuid_core.ai_core.interaction_scaffold import TurnGraph

    _load_kits_once()
    full, _ = asyncio.run(
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
    assert 0 <= full.find("[历史对话]") < full.find("[长期记忆")

    def _run_with_ctx(ctx: AgentHookContext) -> str:
        return asyncio.run(
            assemble_dynamic_context(
                query="hi",
                user_id="u1",
                bot_id="TEST",
                persona_name=None,
                mood_key="u1",
                rel=None,
                history_context="H" * 500,
                memory_context_text="M" * 80,
                hook_ctx=ctx,
            )
        )[0]

    # 产品块总长超 300 时真实触发截断与丢弃；豁免块（口吻/历史/收件箱/记忆）原样保留
    tg = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="p",
        is_tome=True,
        primary_speaker="u1",
        call_to_self=True,
    )
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=tg)
    voice = "（口吻：" + "轻软简短" * 45 + "）"
    pending = "（待你回应）" + "甲" * 160
    task_text = "事项#1｜进行中" + "X" * 170
    ctx.blocks = {
        "voice_anchor": voice,
        "inbox_pending": pending,
        "task": task_text,
        "relationship": "R" * 150,
        "presence": "P" * 80,
    }
    assembled = _run_with_ctx(ctx)
    assert ctx.blocks["voice_anchor"] == voice and ctx.blocks["history"] == "H" * 500
    assert ctx.blocks["inbox_pending"] == pending and "memory" in ctx.blocks
    assert ctx.blocks["task"] == task_text
    assert ctx.blocks["relationship"].endswith("…") and len(ctx.blocks["relationship"]) < 150
    assert "presence" not in ctx.blocks
    exempt = frozenset({"voice_anchor", "history", "memory", "inbox_pending", "evidence"})
    assert sum(len(v) for k, v in ctx.blocks.items() if k not in exempt) == _SUFFIX_PRODUCT_CAP
    assert voice in assembled and "H" * 500 in assembled and "P" * 10 not in assembled

    # 未寻址群轮与软触发群轮：剥离产品块与记忆，保留口吻/历史/收件箱结构块
    idle = TurnGraph(
        user_type="group",
        message_text="hi",
        persona_name="p",
        is_tome=False,
        primary_speaker="u1",
        call_to_self=False,
    )
    expected_idle = frozenset({"voice_anchor", "history", "inbox_pending", "evidence"})
    idle_ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=idle)
    soft_ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT, turn_graph=idle, soft_triggered=True)
    assert suffix_allowed_blocks(idle_ctx) == expected_idle
    assert suffix_allowed_blocks(soft_ctx) == expected_idle
    idle_ctx.blocks = {"voice_anchor": voice, "inbox_pending": pending, "task": "任务块"}
    _run_with_ctx(idle_ctx)
    assert "task" not in idle_ctx.blocks and "memory" not in idle_ctx.blocks
    assert idle_ctx.blocks["voice_anchor"] == voice and idle_ctx.blocks["inbox_pending"] == pending


def test_alias_and_session_anchor_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    import json as _json

    import httpx2
    from openai import AsyncOpenAI
    from pydantic_ai import RunContext
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.usage import RunUsage
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.messages import ImageUrl, TextPart, SystemPromptPart
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.profiles.openai import openai_model_profile
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.entity_index import clear_entity_index, register_entity_surface
    from gsuid_core.ai_core.control.lines import TOOLS_EXECUTION_GATED_DENY, journal_delta_line
    from gsuid_core.ai_core.configs.models import openai_user_thinking_profile
    from gsuid_core.ai_core.history_compact import _relean_user_turn, _peel_ephemeral_system_lines
    from gsuid_core.ai_core.context_assembly import assemble_dynamic_context, history_line_is_assistant
    from gsuid_core.ai_core.agent_run.support import _ensure_session_anchor_on_first_user
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
    from gsuid_core.ai_core.planning.handle_resolver import ResolvedHandle
    from gsuid_core.webconsole.chat_with_history_api import ChatHistoryTurn, _load_request_history
    from gsuid_core.ai_core.planning.tool_output_tools import _tool_output_access_allowed
    from gsuid_core.ai_core.planning.tool_output_helper import should_fold_for_model
    from gsuid_core.ai_core.memory.retrieval.prompt_render import MemoryContext

    for line, expected in [
        ("#4 [14:32:18] 你: 晴天〔已发〕", True),
        ("#5 [14:32:20] 你→#3(小明): 晴天〔已发〕", True),
        ("#6 [14:32:30] 你[回执]→小明: 已记录", True),
        ("#3 [14:32:05] 小明(用户ID:456): 今天天气怎么样", False),
    ]:
        assert history_line_is_assistant(line) is expected, line

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

    full_req = ModelRequest(parts=[UserPromptPart(content=f"[历史对话]\nA: 1\n\n[用户发言]\n你好\n{delta}")])
    _relean_user_turn([full_req], "[用户发言]\n你好")
    assert isinstance(full_req.parts[0], UserPromptPart) and full_req.parts[0].content == "[用户发言]\n你好"

    mm_req = ModelRequest(
        parts=[
            UserPromptPart(
                content=[f"[历史对话]\nA: 1\n\n[用户发言]\n看图\n{delta}", ImageUrl(url="https://x.invalid/a.png")]
            )
        ]
    )
    _relean_user_turn([mm_req], "[用户发言]\n看图")
    assert isinstance(mm_req.parts[0], UserPromptPart) and isinstance(mm_req.parts[0].content, list)
    assert mm_req.parts[0].content[0] == "[用户发言]\n看图" and isinstance(mm_req.parts[0].content[1], ImageUrl)

    assert tool_return_is_gate_reject(TOOLS_EXECUTION_GATED_DENY) is True

    # 通过公开 MemoryContext.to_prompt_text 验证点查过滤：稠密上限 2、停用词剔除、实词置顶
    def _ep(eid: str, content: str, score: float | None = None) -> Episode:
        ep: Episode = {"id": eid, "content": content, "valid_at": "", "scope_key": "group:group_1", "embedding": []}
        if score is not None:
            ep["score"] = score
        return ep

    eps = [_ep(f"e{i}", f"无关日常片段 {i}", 0.0210 - i * 0.0001) for i in range(6)]
    rendered_cap = MemoryContext(episodes=eps).to_prompt_text(4000, query="今天天气如何", wrap_recall=False)
    assert (
        "无关日常片段 0" in rendered_cap and "无关日常片段 1" in rendered_cap and "无关日常片段 2" not in rendered_cap
    )

    rendered_stop = MemoryContext(episodes=[*eps, _ep("p_stop", "测试用户A: 今天午饭吃面")]).to_prompt_text(
        4000, query="今天天气如何", wrap_recall=False
    )
    assert "今天午饭吃面" not in rendered_stop
    assert "无关日常片段 0" in rendered_stop and "无关日常片段 1" in rendered_stop

    rendered_hit = MemoryContext(episodes=[*eps, _ep("p_hit", "测试用户A: 猫粮牌子选了测试款")]).to_prompt_text(
        4000, query="今天猫粮牌子是什么", wrap_recall=False
    )
    assert 0 <= rendered_hit.find("猫粮牌子选了测试款") < rendered_hit.find("无关日常片段 0")
    assert "无关日常片段 1" in rendered_hit and "无关日常片段 2" not in rendered_hit

    # OpenAI 协议自愈：reasoning_effort 学习、strict 误判防护、混传 strict 400 重试与跨实例失效
    clear_openai_wire_facts()
    try:
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
                return httpx2.Response(400, json={"message": nested, "type": "invalid_request_error"})
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

                prepped_settings, prepped_params = stream_m.prepare_request(None, req_params)
                async with stream_m.request_stream(msg, prepped_settings, prepped_params) as stream:
                    async for _ in stream:
                        pass
                    streamed = stream.get()
                assert streamed.parts and isinstance(streamed.parts[0], TextPart)
                assert streamed.parts[0].content == "ok-stream"

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

        err_strict_again = ModelHTTPError(
            status_code=400,
            model_name=gw_model,
            body={"error": {"message": "Cannot use strict tool with non-strict tool in the same request"}},
        )
        assert revise_openai_attempt(gw_url, gw_model, err_strict_again, None, ModelRequestParameters()) is None
    finally:
        clear_openai_wire_facts()

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


def test_render_delta_quote_edges_and_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程视图：引用边、连发合并、依据透传、切片折叠、窗口外引用与经历去重。"""
    import time as _time

    from pydantic_ai.messages import TextPart, ModelResponse

    import gsuid_core.ai_core.outbound as _outbound_mod
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.outbound import QuoteResolve
    from gsuid_core.ai_core.inbox.turn import bind_turn, reset_turn
    from gsuid_core.ai_core.thread_view import render_delta, render_window, render_router_view
    from gsuid_core.ai_core.inbox.blocks import quote_fact_lines, render_pending_block
    from gsuid_core.ai_core.evidence.store import Observation, get_evidence_store
    from gsuid_core.ai_core.history_format import is_persona_speech, remember_silence_handles
    from gsuid_core.ai_core.self_cognition import collect_speaker_preference_rules
    from gsuid_core.ai_core.evidence.capture import record_tool_result
    from gsuid_core.ai_core.agent_run.prepare import _journal_delta_in_recent_history

    base = 1_780_000_000.0
    records = [
        _user_rec(1, "100000003", "测试用户C", "pil活", base + 8),
        _user_rec(2, "100000003", "测试用户C", "让你画图", base + 16),
        _user_rec(3, "100000002", "测试用户B", "ぴー活是什么意思", base + 24, at_self=True),
        _user_rec(4, "100000002", "测试用户B", "神了", base + 32, reply_to_seq=2, reply_to_author="100000003"),
        _bot_rec(
            5,
            "唔…P活一般是パパ活的缩写，简单说就是…",
            base + 40,
            msg_id="b5",
            reply_to_uid="100000002",
            reply_to_name="测试用户B",
            in_reply_to=(3,),
            seen_seq=4,
            user_id="100000002",
        ),
        _user_rec(6, "100000004", "测试用户D", "谁叫汉堡", base + 48, reply_to_seq=5, reply_to_author="self"),
        _user_rec(7, "100000004", "测试用户D", "x2", base + 56),
        _user_rec(8, "100000004", "测试用户D", "x3", base + 64),
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
    assert quote_idx < sent_idx and "↩#2(测试用户C" in lines[quote_idx]

    quoted = next(ln for ln in lines if "谁叫汉堡" in ln)
    assert quoted.startswith("#6 ") and "↩#5(你→测试用户B" in quoted and "前 · 闲聊回复" in quoted
    for w in ("要回", "应该", "请回", "必须", "不要"):
        assert w not in quoted
    merged = next(ln for ln in lines if ln.startswith("#7–#8"))
    assert "x2 / x3" in merged

    # metadata 不含 session_id 时，delta / router / window 均透传〔依据〕标签
    sess_id = "OneBot:onebot:900000001:group:70001"
    store = get_evidence_store(sess_id)
    store.observations.clear()
    obs_now = _time.time()
    store.append(
        Observation(
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
    )
    try:
        ev_records = [
            *records[:4],
            _bot_rec(
                5,
                "已帮你完成签到啦",
                base + 42,
                msg_id="b_ev",
                reply_to_uid="100000002",
                reply_to_name="测试用户B",
                in_reply_to=(3,),
                seen_seq=4,
                evidence=("ob_basis01",),
            ),
            _user_rec(6, "100000004", "测试用户D", "测试人格，帮我签到", base + 48),
        ]
        delta_ev = render_delta(
            ev_records,
            from_seq=0,
            head_seq=6,
            backfill=False,
            pending_seqs=(6,),
            persona_name="测试人格",
            private=False,
            session_id=sess_id,
        )
        turn_ev = _make_turn(
            6,
            "100000004",
            "name",
            "hard",
            base + 48,
            session_id=sess_id,
            group_id="70001",
            raw_text="测试人格，帮我签到",
        )
        assert "〔依据: 替测试用户B查" in delta_ev
        assert "〔依据: 替测试用户B查" in render_router_view(turn_ev, ev_records)
        assert "〔依据: 替测试用户B查" in render_window(ev_records, persona_name="测试人格", session_id=sess_id)
    finally:
        store.observations.clear()

    # 静默轮写入 A 轨的 dlg_ 委派句柄标为非角色台词，不渲染进窗口
    hm = get_history_manager()
    ev_dlg = Event("OneBot", "900000001", "dlg_m1", "group", "group_dlg_1", "100000001", {}, 6)
    hm.clear_history(ev_dlg)
    try:
        hm.add_message(ev_dlg, role="user", content="帮我画一张测试指标图", user_name="测试用户A")
        remember_silence_handles(ev_dlg, ["dlg_2f7fe0a6-db1c-4aff-8195-b80c721ae644"], manager=hm)
        stored = hm.get_history(ev_dlg)
        assert len(stored) == 2 and not is_persona_speech(stored[-1])
        assert "dlg_2f7fe0a6" not in render_window(stored, persona_name="测试人格")
    finally:
        hm.clear_history(ev_dlg)

    # 群聊切片：裁 30 分钟前无关命令回执、折叠 4× 重复、私聊冷启动与刷屏折叠
    t_now = base + 7200.0
    slice_records = [
        _user_rec(
            10, "100000099", "测试用户Z", "旧指令触发", t_now - 3600.0, msg_id="old1", extra_meta={"command_hit": True}
        ),
        _bot_rec(
            11,
            "[图片]",
            t_now - 3595.0,
            msg_id="old2",
            reply_to_uid="100000099",
            reply_to_name="测试用户Z",
            speech_channel="command_aside",
        ),
        _bot_rec(
            12,
            "这是之前回复给测试用户A 的内容",
            t_now - 90.0,
            msg_id="bot12",
            reply_to_uid="100000001",
            reply_to_name="测试用户A",
            in_reply_to=(10,),
        ),
        *[
            _user_rec(
                13 + idx,
                "100000002",
                "测试用户B",
                "哈哈",
                t_now - 60.0 + idx * 5.0,
                msg_id=f"rep_{idx}",
                reply_to_seq=12,
                reply_to_author="self",
            )
            for idx in range(4)
        ],
        _user_rec(
            17,
            "100000001",
            "测试用户A",
            "测试一下这个",
            t_now,
            msg_id="q17",
            reply_to_seq=12,
            reply_to_author="self",
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

    dm_old_records = [
        _user_rec(1, "100000001", "测试用户A", "两小时前的私聊问题", t_now - 7200.0, msg_id="dm1", user_type="direct"),
        _user_rec(2, "100000001", "测试用户A", "继续聊", t_now, msg_id="dm2", user_type="direct"),
    ]
    dm_delta = render_delta(
        dm_old_records, from_seq=1, head_seq=2, backfill=True, pending_seqs=(2,), persona_name="测试人格", private=True
    )
    assert "两小时前的私聊问题" in dm_delta

    unquoted_spam = [
        _user_rec(20 + idx, "100000002", "测试用户B", "刷屏词", t_now - 30.0 + idx * 2.0, msg_id=f"sp_{idx}")
        for idx in range(4)
    ] + [_user_rec(25, "100000001", "测试用户A", "正经问题", t_now, msg_id="sp_q")]
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

    turn_qself = _make_turn(
        17, "100000001", "quote_self", "soft", t_now, msg_id="q17", raw_text="测试一下这个", user_pm=0, reply_id="bot12"
    )
    rview_q = render_router_view(turn_qself, slice_records, relation_label="主人", group_rate_1m=5)
    assert "引用对象=你→本人" in rview_q
    assert "[场景] 关系=主人 · 近1分钟群消息=5条 · 触发=quote_self" in rview_q
    assert "(用户ID:" not in rview_q

    turn_rcpt = _make_turn(
        11,
        "100000099",
        "receipt",
        "soft",
        t_now,
        msg_id="old1",
        raw_text="旧指令触发",
        receipt=ReceiptContext(None, "测试插件", "entry.py", "错误码 -400016"),
    )
    rview_rcpt = render_router_view(turn_rcpt, slice_records)
    assert "★#11 receipt · 插件=测试插件" in rview_rcpt and "你上次回他是" not in rview_rcpt

    pblock_rcpt = render_pending_block(turn_rcpt, slice_records)
    assert "★#11 [命令回执] 插件=测试插件" in pblock_rcpt and "AI(" not in pblock_rcpt

    # 窗口外引用触发 resolve_quote；窗口内已有 reply_to_seq 命中 seq_index 则跳过
    resolve_calls: list[str] = []

    async def _fake_resolve_quote(ev: Event) -> QuoteResolve:
        resolve_calls.append(ev.msg_id)
        return QuoteResolve(
            line="（被引用的是你于 14:40 发给群内的「测试播报」）", topic="测试播报", handle="", target="群内"
        )

    monkeypatch.setattr(_outbound_mod, "resolve_quote", _fake_resolve_quote)
    turn_oow = _make_turn(
        18,
        "100000001",
        "quote_self",
        "soft",
        t_now,
        msg_id="q_oow",
        raw_text="这",
        user_pm=0,
        reply_id="missing_mid",
        reply="测试播报内容",
    )
    oow_rec = _user_rec(
        18,
        "100000001",
        "测试用户A",
        "这",
        t_now,
        msg_id="q_oow",
        reply_to_seq=None,
        reply_to_author="self",
        extra_meta={"reply_id": "missing_mid", "reply_preview": "测试播报内容"},
    )
    assert asyncio.run(quote_fact_lines(turn_oow, [*slice_records, oow_rec])) == [
        "★#18 （被引用的是你于 14:40 发给群内的「测试播报」）"
    ]
    assert asyncio.run(quote_fact_lines(turn_qself, slice_records)) == []
    assert resolve_calls == ["q_oow"]

    # 日志经历去重表驱动 / 偏好 2-gram 去重 / 拒写空壳 read_image 观测
    hist_j = [ModelResponse(parts=[TextPart(content="刚刚帮测试用户A查了今日天气，结果是晴天")])]
    two_seg = (
        "（系统提示：刚刚经历：[12:00 | 闲聊] 刚刚帮测试用户A查了今日天气，结果是晴天；"
        "[12:01 | 闲聊] 全新的另一条未复述事项记录）"
    )
    for delta_text, expected in [
        ("刚刚帮测试用户A查了今日天气，结果是晴天", True),
        ("完全不同的另一件新日程安排记录", False),
        (two_seg, False),
    ]:
        assert _journal_delta_in_recent_history(delta_text, hist_j) is expected

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

    empty_sess = "OneBot:onebot:900000001:group:70098_ev"
    empty_store = get_evidence_store(empty_sess)
    empty_store.observations.clear()
    tok_ev = bind_turn(
        _make_turn(1, "100000001", "mention", "hard", obs_now, session_id=empty_sess, group_id="70098", msg_id="ev1")
    )
    try:
        record_tool_result("read_image", b"raw-bytes", None)
        assert not empty_store.observations
    finally:
        reset_turn(tok_ev)
        empty_store.observations.clear()


def test_phase2a_history_roll_invisible_name_and_later_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR 2A：过期前缀滚出与补锚、配置上限、隐形昵称清洗与滞后追问已交付标注。"""
    from datetime import datetime, timezone, timedelta

    from pydantic_ai.messages import (
        TextPart,
        ToolCallPart,
        ModelResponse,
        ToolReturnPart,
        SystemPromptPart,
    )
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from gsuid_core.ai_core.utils import sanitize_display_name, _build_relationship_description
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.thread_view import render_delta
    from gsuid_core.ai_core.inbox.blocks import render_pending_block
    from gsuid_core.ai_core.history_compact import compact_session_history
    from gsuid_core.ai_core.configs.ai_config import ai_config

    # 1. 活跃窗内保头；3 小时过期前缀滚出但原样保留 SystemPromptPart
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

    fresh_compacted, did_fresh = compact_session_history(
        _make_hist(fresh_dt), max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert did_fresh is True and "三小时前大盘走势" in _user_texts(fresh_compacted[:1])

    rolled, did_roll = compact_session_history(
        _make_hist(old_dt), max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert did_roll is True and isinstance(rolled[0], ModelRequest)
    assert any(isinstance(p, SystemPromptPart) and p.content == "固定系统提示词" for p in rolled[0].parts)
    rolled_users = _user_texts(rolled)
    assert "三小时前大盘走势" not in rolled_users and "近期提问9" in rolled_users

    # naive 时间戳按 UTC 归一化
    naive_fresh, _ = compact_session_history(
        _make_hist(datetime(2026, 11, 24, 11, 58, 0)),
        max_history=12,
        trim_ratio=0.6,
        max_prefix_age_s=1800.0,
        now_ts=now_dt.timestamp(),
    )
    assert "三小时前大盘走势" in _user_texts(naive_fresh[:1])
    naive_old, _ = compact_session_history(
        _make_hist(datetime(2026, 11, 24, 9, 0, 0)),
        max_history=12,
        trim_ratio=0.6,
        max_prefix_age_s=1800.0,
        now_ts=now_dt.timestamp(),
    )
    assert "三小时前大盘走势" not in _user_texts(naive_old)

    def _append_tool_chain(msgs: list[ModelRequest | ModelResponse], count: int, prefix: str) -> None:
        for i in range(count):
            ts = fresh_dt + timedelta(seconds=i * 2)
            msgs.append(
                ModelResponse(
                    parts=[ToolCallPart(tool_name="demo_tool", args={"i": i}, tool_call_id=f"{prefix}{i}")],
                    timestamp=ts,
                )
            )
            msgs.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="demo_tool", content=f"ok{i}", tool_call_id=f"{prefix}{i}", timestamp=ts
                        )
                    ]
                )
            )

    # 单轮长工具链不产出 SystemPrompt -> ModelResponse(ToolCall)
    single_turn_tools: list[ModelRequest | ModelResponse] = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="固定系统提示词", timestamp=old_dt),
                UserPromptPart(content="超长单轮工具提问", timestamp=old_dt),
            ]
        )
    ]
    _append_tool_chain(single_turn_tools, 8, "c")
    safe_tools_hist, _ = compact_session_history(
        single_turn_tools, max_history=10, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    assert _user_texts(safe_tools_hist[:1])

    # 滚出过期首轮后仍超 max_history 时继续压回上限以内
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
    _append_tool_chain(oversized_after_roll, 10, "r")
    capped_rolled, did_cap_roll = compact_session_history(
        oversized_after_roll, max_history=12, trim_ratio=0.6, max_prefix_age_s=1800.0, now_ts=now_dt.timestamp()
    )
    capped_users = _user_texts(capped_rolled)
    assert did_cap_roll is True and len(capped_rolled) <= 12
    assert "过期旧提问" not in capped_users and "后续唯一干净提问" in capped_users

    # 主会话尊重配置 agent_max_history；滚出过期首轮后下一真人轮自动一次性补锚
    monkeypatch.setattr(ai_config.get_config("agent_max_history"), "data", 15)
    monkeypatch.setattr(ai_config.get_config("enable_deepseek_rp"), "data", True)
    dummy_model = OpenAIChatModel("test-offline", provider=OpenAIProvider(api_key="test"))
    agent_main = GsCoreAIAgent(openai_chat_model=dummy_model, system_prompt="固定系统提示词", create_by="Chat")
    assert agent_main.max_history == 15
    agent_main.history = _make_hist(old_dt)
    agent_main.thread_backfill_due = False
    agent_main.extract_history()
    assert agent_main.thread_backfill_due is True

    def _inject_marker(text: str) -> tuple[str, str]:
        st = make_run_state(text)
        st.final_user_message = text
        st.lean_user_message = text
        agent_main._inject_deepseek_rp_marker(st)
        assert isinstance(st.final_user_message, str) and isinstance(st.lean_user_message, str)
        return st.final_user_message, st.lean_user_message

    re_cur, re_lean = _inject_marker("新回合消息")
    assert "【角色沉浸要求】" in re_cur and "【角色沉浸要求】" in re_lean
    assert _inject_marker("再下一轮") == ("再下一轮", "再下一轮")

    # 2. 隐形字符昵称清洗表驱动 + 关系描述回落
    for raw_name, expected in [
        ("小\u200b明\ufeff", "小明"),
        ("\u200b\u3164\u2800", "群友_0001"),
        ("100000001\u200b", ""),
        ("", ""),
    ]:
        assert sanitize_display_name(raw_name, "100000001") == expected
    assert "群友_0001(用户ID:100000001)" in _build_relationship_description("\u200b\u3164", "100000001")

    # 3. 滞后追问之后已有针对同用户的出站交付，待回应块显式标注且切片保留该出站行
    t0 = 1_760_000_000.0
    recs = [
        _user_rec(10, "100000001", "\u200b\u3164", "查一下测试数据", t0),
        _user_rec(11, "100000001", "\u200b\u3164", "好了没", t0 + 20),
        _bot_rec(
            12,
            "测试数据图已发给你啦",
            t0 + 30,
            msg_id="m12",
            reply_to_uid="100000001",
            in_reply_to=(10,),
            user_id="bot",
        ),
    ]
    turn_lag = _make_turn(
        11,
        "100000001",
        "mention",
        "hard",
        t0 + 20,
        session_id="OneBot:onebot:900000001:group:group_1",
        group_id="group_1",
        raw_text="好了没",
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
    assert "群友_0001(用户ID:100000001)" in delta_txt and "#12 " in delta_txt

    late_rec = _user_rec(13, "100000002", "测试用户B", "一小时后的新追问", t0 + 3600)
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
    from gsuid_core.ai_core.entity_index import clear_entity_index
    from gsuid_core.ai_core.cognition.nodes import AICogNode
    from gsuid_core.ai_core.cognition.types import CogKind, CogScope, CognitiveHit
    from gsuid_core.ai_core.planning.kanban import _unlink_artifact_file

    clear_entity_index()
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

    # Kanban 删单产物：兄弟目录 artifacts_other 拥有同字符串前缀，必须被拦截
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
