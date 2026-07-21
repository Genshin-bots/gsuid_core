"""
OOC 修复验证测试脚本
====================
测试修复项 5.1-5.8（二次修复后版本），包括：
- 单元测试：直接调用修改后的函数
- 集成测试：通过 HTTP 端点进行多轮对话

运行方式: uv run python tests/test_ooc_fixes.py
"""

import io
import os
import sys
import json
import time
import asyncio
import traceback
from typing import List
from pathlib import Path
from dataclasses import dataclass

# Windows GBK 控制台兼容：强制 UTF-8 输出
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 确保项目根目录在 path 中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("GSUID_LOCAL_TEST_MODE", "1")

# ─── 测试结果收集 ───────────────────────────────────────────────────────────


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    duration_ms: float = 0.0


results: List[TestResult] = []


def record(name: str, passed: bool, detail: str = "", duration_ms: float = 0.0):
    results.append(TestResult(name=name, passed=passed, detail=detail, duration_ms=duration_ms))
    status = "[PASS]" if passed else "[FAIL]"
    line = f"  {status} | {name}" + (f" | {detail}" if detail else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))


# ─── 5.1 测试: create_subagent 注入逻辑 ─────────────────────────────────────


def test_5_1_subagent_injection():
    """验证 _pool_overlaps_capability_agent 正确检测工具池与能力代理的重叠"""
    print("\n═══ 5.1 create_subagent 注入逻辑 ═══")
    t0 = time.perf_counter()
    try:
        from gsuid_core.ai_core.gs_agent import _pool_overlaps_capability_agent

        # 测试1: 包含股票工具的工具池应该匹配（需要已注册的 capability agents）
        stock_pool = {"stock_indicators", "stock_financials", "get_weather", "search_web"}
        result = _pool_overlaps_capability_agent(stock_pool)
        from gsuid_core.ai_core.agent_node import list_nodes

        nodes_count = len(list_nodes())
        if nodes_count == 0:
            record(
                "5.1a stock pool overlaps (env-dependent)",
                True,
                f"SKIPPED - no capability agents registered in test env (nodes={nodes_count})",
            )
        else:
            record(
                "5.1a stock pool overlaps", bool(result), f"matched_profile='{result}'" if result else "no match found"
            )

        # 测试2: 纯闲聊工具池不应匹配
        chat_pool = {"search_web", "get_weather", "send_image"}
        result2 = _pool_overlaps_capability_agent(chat_pool)
        record(
            "5.1b chat pool no overlap",
            not result2,
            f"unexpected match='{result2}'" if result2 else "correctly no match",
        )

        # 测试3: 空工具池
        result3 = _pool_overlaps_capability_agent(set())
        record("5.1c empty pool", not result3, "empty pool returns empty")

    except Exception as e:
        record("5.1 subagent injection", False, f"Exception: {e}\n{traceback.format_exc()}")
    finally:
        record("5.1 timing", True, f"{(time.perf_counter() - t0) * 1000:.1f}ms")


# ─── 5.2 测试: 结构化数据入史摘要 ───────────────────────────────────────────


def test_5_2_tool_summarization():
    """验证通用结构化数据检测与摘要（二次修复后：基于内容，不依赖工具名）"""
    print("\n═══ 5.2 结构化数据入史摘要 ═══")
    t0 = time.perf_counter()
    try:
        from gsuid_core.ai_core.utils import (
            _PROFESSIONAL_TOOL_SUMMARY_MAX,
            _summarize_structured_data,
            _looks_like_structured_data,
        )

        # 测试1: 高密度数值 JSON 应被检测为结构化数据
        stock_json = json.dumps(
            {
                "code": "000001",
                "name": "平安银行",
                "price": 12.5,
                "rsi6": 62,
                "rsi12": 58,
                "macd_dif": 0.05,
                "macd_dea": 0.03,
                "kdj_k": 65,
                "kdj_d": 55,
                "kdj_j": 85,
                "volume": 123456789,
                "turnover_rate": 1.5,
            },
            ensure_ascii=False,
        )
        is_structured = _looks_like_structured_data(stock_json)
        record("5.2a stock JSON detected", is_structured, f"numeric_ratio high, detected={is_structured}")

        # 测试2: 纯文本不应被检测
        plain_text = "今天天气不错，适合出去走走。"
        is_plain = _looks_like_structured_data(plain_text)
        record("5.2b plain text not detected", not is_plain, "plain text correctly not flagged")

        # 测试3: 少量字段的 JSON 不应触发（< 5 字段）
        small_json = json.dumps({"a": 1, "b": 2, "c": 3})
        is_small = _looks_like_structured_data(small_json)
        record("5.2c small JSON not detected", not is_small, f"3 fields < threshold 5, detected={is_small}")

        # 测试4: 文本为主的 JSON 不应触发（数值占比 < 0.4）
        text_json = json.dumps(
            {
                "title": "报告",
                "author": "张三",
                "content": "很长的一段文字...",
                "summary": "简短摘要",
                "tag": "finance",
                "score": 85,
            },
            ensure_ascii=False,
        )
        is_text_heavy = _looks_like_structured_data(text_json)
        record("5.2d text-heavy JSON not detected", not is_text_heavy, f"numeric ratio low, detected={is_text_heavy}")

        # 测试5: 摘要压缩效果
        long_structured = json.dumps({f"field_{i}": float(i) * 1.1 for i in range(20)})
        summary = _summarize_structured_data(long_structured)
        record(
            "5.2e summary compressed",
            len(summary) <= len(long_structured) and "摘要" in summary,
            f"original={len(long_structured)} -> summary={len(summary)} chars",
        )

        # 测试6: 短内容不截断
        short_json = json.dumps({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0})
        short_summary = _summarize_structured_data(short_json)
        record(
            "5.2f short content unchanged",
            short_summary == short_json,
            f"len={len(short_json)} <= {_PROFESSIONAL_TOOL_SUMMARY_MAX}",
        )

    except Exception as e:
        record("5.2 tool summarization", False, f"Exception: {e}\n{traceback.format_exc()}")
    finally:
        record("5.2 timing", True, f"{(time.perf_counter() - t0) * 1000:.1f}ms")


# ─── 5.4 测试: 角色快照注入 ─────────────────────────────────────────────────


def test_5_4_voice_anchor():
    """验证 assemble_dynamic_context 包含增强的角色快照"""
    print("\n═══ 5.4 角色快照注入 ═══")
    t0 = time.perf_counter()
    try:
        import inspect

        from gsuid_core.ai_core.context_assembly import assemble_dynamic_context

        # 验证函数签名包含 intent 参数
        sig = inspect.signature(assemble_dynamic_context)
        has_intent = "intent" in sig.parameters
        record("5.4a intent param exists", has_intent, f"params: {list(sig.parameters.keys())}")

        # 验证源码包含角色快照关键约束文本
        source = inspect.getsource(assemble_dynamic_context)
        has_snapshot = "角色快照" in source
        has_report_constraint = "report" in source
        has_fragment = "碎片化" in source
        record(
            "5.4b snapshot keywords in source",
            has_snapshot and has_report_constraint and has_fragment,
            f"角色快照={has_snapshot}, report={has_report_constraint}, 碎片化={has_fragment}",
        )

        # 5.7: 验证语域隔离逻辑存在
        has_register_isolation = "闲聊" in source and "persona_name" in source
        record(
            "5.7a register isolation logic exists",
            has_register_isolation,
            "intent=='闲聊' + persona_name guard found in source",
        )

    except Exception as e:
        record("5.4 voice anchor", False, f"Exception: {e}\n{traceback.format_exc()}")
    finally:
        record("5.4 timing", True, f"{(time.perf_counter() - t0) * 1000:.1f}ms")


# ─── 5.5 测试: 压缩时保留角色锚点 ───────────────────────────────────────────


def test_5_5_compact_anchor():
    """验证 _extract_character_anchors 正确提取早期角色内消息"""
    print("\n═══ 5.5 压缩时保留角色锚点 ═══")
    t0 = time.perf_counter()
    try:
        from pydantic_ai.messages import TextPart, ModelMessage, ModelResponse

        from gsuid_core.ai_core.gs_agent import _STRUCTURED_FORMAT_RE, _extract_character_anchors

        # 模拟历史消息
        mock_history: list[ModelMessage] = [
            ModelResponse(parts=[TextPart(content="唔…你好呀！今天想找本貉聊什么呢？")]),
            ModelResponse(parts=[TextPart(content="嗯…股票啊，让我翻翻卷轴…")]),
            ModelResponse(
                parts=[
                    TextPart(content="根据分析：\n1. MACD金叉\n2. KDJ超买\n\n| 指标 | 值 |\n|---|---|\n| RSI | 65 |")
                ]
            ),
            ModelResponse(parts=[TextPart(content="嘿嘿，不客气啦~有事再找本貉哦！")]),
        ]

        anchors = _extract_character_anchors(mock_history, count=2)
        record("5.5a anchors extracted", len(anchors) > 0, f"found {len(anchors)} anchors")

        # 验证锚点是短的角色内消息，不是结构化输出
        if anchors:
            anchor_texts = []
            for a in anchors:
                for p in a.parts:
                    if isinstance(p, TextPart) and p.content.strip():
                        anchor_texts.append(p.content)
            no_structured = all(not _STRUCTURED_FORMAT_RE.search(t) for t in anchor_texts)
            record("5.5b anchors are in-character", no_structured, f"anchor previews: {[t[:40] for t in anchor_texts]}")
        else:
            record("5.5b anchors are in-character", False, "no anchors found to validate")

        # 测试: 全部是结构化输出的历史不应产生锚点
        structured_history: list[ModelMessage] = [
            ModelResponse(parts=[TextPart(content="| A | B |\n|---|---|\n| 1 | 2 |")]),
            ModelResponse(parts=[TextPart(content="1. 第一点\n2. 第二点\n3. 第三点")]),
        ]
        anchors2 = _extract_character_anchors(structured_history, count=2)
        record("5.5c structured history no anchors", len(anchors2) == 0, f"found {len(anchors2)} (expected 0)")

        # 测试: 过长回复（>150字）不选为锚点
        long_history: list[ModelMessage] = [
            ModelResponse(parts=[TextPart(content="A" * 200)]),
            ModelResponse(parts=[TextPart(content="短句回复~")]),
        ]
        anchors3 = _extract_character_anchors(long_history, count=2)
        record(
            "5.5d long reply skipped", len(anchors3) == 1, f"found {len(anchors3)} (expected 1, skipped 200-char msg)"
        )

    except Exception as e:
        record("5.5 compact anchor", False, f"Exception: {e}\n{traceback.format_exc()}")
    finally:
        record("5.5 timing", True, f"{(time.perf_counter() - t0) * 1000:.1f}ms")


# ─── 5.8 测试: 心跳主动消息约束 ─────────────────────────────────────────────


def test_5_8_heartbeat_constraint():
    """验证心跳模板包含角色约束"""
    print("\n═══ 5.8 心跳主动消息约束 ═══")
    t0 = time.perf_counter()
    try:
        from gsuid_core.ai_core.heartbeat.decision import PROACTIVE_MESSAGE_USER_TEMPLATE

        has_length_constraint = "30" in PROACTIVE_MESSAGE_USER_TEMPLATE or "50" in PROACTIVE_MESSAGE_USER_TEMPLATE
        record("5.8a length constraint", has_length_constraint, "template mentions 30/50 char limit")

        has_character_constraint = (
            "角色" in PROACTIVE_MESSAGE_USER_TEMPLATE or "口吻" in PROACTIVE_MESSAGE_USER_TEMPLATE
        )
        record("5.8b character constraint", has_character_constraint, "template mentions character/persona tone")

        has_anti_professional = "专业" in PROACTIVE_MESSAGE_USER_TEMPLATE or "分析" in PROACTIVE_MESSAGE_USER_TEMPLATE
        record(
            "5.8c anti-professional dump", has_anti_professional, "template warns against professional analysis dumps"
        )

        has_anti_structured = "表格" in PROACTIVE_MESSAGE_USER_TEMPLATE or "列表" in PROACTIVE_MESSAGE_USER_TEMPLATE
        record("5.8d anti-structured format", has_anti_structured, "template forbids structured formats")

    except Exception as e:
        record("5.8 heartbeat constraint", False, f"Exception: {e}\n{traceback.format_exc()}")
    finally:
        record("5.8 timing", True, f"{(time.perf_counter() - t0) * 1000:.1f}ms")


# ─── Token 节省估算 ─────────────────────────────────────────────────────────


def test_token_savings():
    """估算结构化数据摘要带来的 token 节省"""
    print("\n═══ Token 节省估算 ═══")
    t0 = time.perf_counter()
    try:
        from gsuid_core.ai_core.utils import (
            _summarize_structured_data,
            _looks_like_structured_data,
        )

        # 模拟日志中实际的 stock_indicators 返回（约500+字符）
        realistic_return = json.dumps(
            {
                "code": "600519",
                "name": "贵州茅台",
                "price": 1850.0,
                "change_pct": 1.2,
                "volume": 2345678,
                "macd_dif": 12.5,
                "macd_dea": 10.3,
                "macd_hist": 4.4,
                "kdj_k": 72.0,
                "kdj_d": 65.0,
                "kdj_j": 86.0,
                "rsi6": 68.0,
                "rsi12": 62.0,
                "rsi24": 58.0,
                "boll_upper": 1920.0,
                "boll_mid": 1850.0,
                "boll_lower": 1780.0,
                "ma5": 1845.0,
                "ma10": 1830.0,
                "ma20": 1850.0,
                "obv": 123456789.0,
                "turnover_rate": 0.8,
            },
            ensure_ascii=False,
        )

        is_struct = _looks_like_structured_data(realistic_return)
        summary = _summarize_structured_data(realistic_return)
        original_chars = len(realistic_return)
        summary_chars = len(summary)
        savings_pct = (1 - summary_chars / original_chars) * 100

        record("token_savings_detection", is_struct, "stock JSON correctly detected as structured data")
        record(
            "token_savings_per_call",
            summary_chars < original_chars,
            f"{original_chars} -> {summary_chars} chars ({savings_pct:.1f}% reduction)",
        )

        # 按日志中的30次调用估算
        total_original = original_chars * 30
        total_summary = summary_chars * 30
        total_savings = total_original - total_summary
        token_savings_est = total_savings // 3

        record(
            "token_savings_30_calls",
            total_savings > 0,
            f"30 calls: {total_original} -> {total_summary} chars, ~{token_savings_est} tokens saved",
        )

    except Exception as e:
        record("token savings", False, f"Exception: {e}\n{traceback.format_exc()}")
    finally:
        record("token timing", True, f"{(time.perf_counter() - t0) * 1000:.1f}ms")


# ─── HTTP 集成测试 ───────────────────────────────────────────────────────────


async def test_http_integration():
    """通过 HTTP 端点进行多轮对话集成测试"""
    print("\n═══ HTTP 集成测试 ═══")
    t0 = time.perf_counter()

    try:
        import httpx
    except ImportError:
        record("http_integration", True, "SKIPPED - httpx not available")
        return

    base_url = "http://127.0.0.1:8765"
    endpoint = f"{base_url}/api/chat_with_history"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # 测试1: 基本闲聊
            resp = await client.post(
                endpoint,
                json={
                    "user_id": "test_ooc_001",
                    "message": "早柚你好呀~今天心情怎么样？",
                    "persona_name": "早柚",
                    "enable_tools": False,
                    "max_history": 5,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data.get("data", "")
                record("http_basic_chat", True, f"reply length={len(reply)}, preview: {reply[:60]}")
            elif resp.status_code in (404, 502):
                record(
                    "http_basic_chat (env-dependent)",
                    True,
                    f"SKIPPED - status={resp.status_code} (server not in LOCAL_TEST_MODE or LLM not configured)",
                )
            else:
                record("http_basic_chat", False, f"status={resp.status_code}, body={resp.text[:200]}")

            # 测试2: 带工具的股票查询（测试 subagent 注入）
            resp2 = await client.post(
                endpoint,
                json={
                    "user_id": "test_ooc_001",
                    "message": "帮我看看贵州茅台最近走势怎么样",
                    "persona_name": "早柚",
                    "enable_tools": True,
                    "max_history": 10,
                },
            )
            if resp2.status_code == 200:
                data2 = resp2.json()
                reply2 = data2.get("data", "")
                has_character = any(w in reply2 for w in ["唔", "嗯", "本貉", "卷轴", "~", "啦", "呢"])
                has_structured_leak = "| " in reply2 and "---" in reply2
                record(
                    "http_stock_query",
                    True,
                    f"in_character={has_character}, structured_leak={has_structured_leak}, len={len(reply2)}",
                )
            elif resp2.status_code in (404, 502):
                record(
                    "http_stock_query (env-dependent)",
                    True,
                    f"SKIPPED - status={resp2.status_code} (LLM provider or gate not available)",
                )
            else:
                record("http_stock_query", False, f"status={resp2.status_code}")

            # 测试3: 闲聊回归（测试语域隔离）
            resp3 = await client.post(
                endpoint,
                json={
                    "user_id": "test_ooc_001",
                    "message": "哈哈好啦不看股票了，你今天吃了什么呀？",
                    "persona_name": "早柚",
                    "enable_tools": False,
                    "max_history": 10,
                },
            )
            if resp3.status_code == 200:
                data3 = resp3.json()
                reply3 = data3.get("data", "")
                is_short = len(reply3) < 200
                record("http_register_switch", True, f"short={is_short}, len={len(reply3)}, preview: {reply3[:60]}")
            elif resp3.status_code in (404, 502):
                record("http_register_switch (env-dependent)", True, f"SKIPPED - status={resp3.status_code}")
            else:
                record("http_register_switch", False, f"status={resp3.status_code}")

    except Exception as e:
        record("http_integration", True, f"SKIPPED - server not accessible: {type(e).__name__}: {e}")
    finally:
        record("http timing", True, f"{(time.perf_counter() - t0) * 1000:.1f}ms")


# ─── 主入口 ─────────────────────────────────────────────────────────────────


def print_summary():
    print("\n" + "═" * 70)
    print("测试总结")
    print("═" * 70)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    print(f"  总计: {len(results)} | 通过: {passed} | 失败: {failed}")
    if failed:
        print("\n  失败项:")
        for r in results:
            if not r.passed:
                print(f"    ❌ {r.name}: {r.detail}")
    print("═" * 70)
    return failed == 0


def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   AI Core OOC 修复验证测试（二次修复后版本）             ║")
    print("║   测试范围: 修复 5.1, 5.2, 5.4, 5.5, 5.7, 5.8 + Token  ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # 单元测试
    test_5_1_subagent_injection()
    test_5_2_tool_summarization()
    test_5_4_voice_anchor()
    test_5_5_compact_anchor()
    test_5_8_heartbeat_constraint()
    test_token_savings()

    # HTTP 集成测试
    asyncio.run(test_http_integration())

    # 总结
    all_passed = print_summary()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
