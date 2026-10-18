"""search_web_images 工具及多图发送的单元测试与意图召回验证。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.buildin_tools.image_finder import (
    FoundImage,
    search_web_images,
)
from gsuid_core.ai_core.buildin_tools.find_tools_rank import (
    classify_callable_tool,
    need_matches_tool_text,
)


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.deps = ToolContext(
        bot=MagicMock(),
        ev=MagicMock(),
        extra={},
        parent_session_id=None,
    )
    return ctx


def test_search_web_images_formatted_output() -> None:
    """搜图成功后，输出中包含 img_ 句柄以及指导模型调用 send_message_by_ai(image_ids=[...]) 的指引。"""
    fake_results = [
        (
            "原神 芙宁娜",
            [
                FoundImage(
                    resource_id="img_furina01",
                    width=800,
                    height=600,
                    title="芙宁娜 Cosplay",
                    source="360",
                ),
            ],
        ),
    ]

    with patch(
        "gsuid_core.ai_core.buildin_tools.image_finder.search_and_verify_images",
        new=AsyncMock(return_value=(fake_results, "")),
    ):
        ctx = _make_ctx()
        out = asyncio.run(search_web_images(ctx, queries=["原神 芙宁娜"]))

    assert "img_furina01" in out
    assert "send_message_by_ai(image_ids=" in out
    assert "read_image" in out


def test_search_web_images_empty_result() -> None:
    """搜索结果为空时，给出清晰的未找到提示。"""
    with patch(
        "gsuid_core.ai_core.buildin_tools.image_finder.search_and_verify_images",
        new=AsyncMock(return_value=([], "")),
    ):
        ctx = _make_ctx()
        out = asyncio.run(search_web_images(ctx, queries=["不存在的词汇xyz"]))

    assert "未能搜到可用的网络图片" in out


def test_search_web_images_intent_matching() -> None:
    """意图词匹配：验证 session 中的实际提问能准确匹配 search_web_images。"""
    from gsuid_core.ai_core.register import find_tool_base

    tb = find_tool_base("search_web_images")
    assert tb is not None, "search_web_images 必须成功注册进全局工具表"
    covers = list(tb.covers)
    retrieval = tb.retrieval_text

    # 1. 本次 session 用户原话
    assert need_matches_tool_text("搜索网络上的coser图片并发送到群聊", retrieval, covers)
    assert need_matches_tool_text("帮我找三个coser图片发群里", retrieval, covers)

    # 2. 用户提到的表情包诉求
    assert need_matches_tool_text("网上找点鲸鱼的表情包", retrieval, covers)

    # 3. 询问外貌/长相诉求
    assert need_matches_tool_text("你知道云璃长什么样吗？", retrieval, covers)


def test_search_web_images_tier_not_exclusive() -> None:
    """search_web_images 为 common 分类且无 domain，绝不应被独占剥离为 fold。"""
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.agent_run.support import _capability_exclusive_tool_names

    tb = find_tool_base("search_web_images")
    assert tb is not None
    exclusive = _capability_exclusive_tool_names()
    assert "search_web_images" not in exclusive

    tier = classify_callable_tool(
        name=tb.name,
        category=tb.category,
        plugin=tb.plugin,
        hide_from_main=tb.hide_from_main,
        exclusive=exclusive,
    )
    assert tier != "fold"
