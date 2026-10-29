from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.statistics.price import calculate_model_cost
from gsuid_core.ai_core.statistics.models import (
    AIDailyStatistics,
    AITokenUsageByType,
    AITokenUsageByModel,
)


def test_calculate_model_cost_zero():
    assert calculate_model_cost("gpt-4o", 0, 0) == 0.0
    assert calculate_model_cost("unknown-model", 0, 0) == 0.0


def test_calculate_model_cost_known_models():
    cost_gpt4o = calculate_model_cost("gpt-4o", 1000, 500, 200, 100)
    assert cost_gpt4o > 0.0

    cost_claude = calculate_model_cost("claude-3-5-sonnet-20241022", 1000, 500)
    assert cost_claude > 0.0

    cost_deepseek = calculate_model_cost("deepseek-chat", 1000, 500)
    assert cost_deepseek > 0.0

    cost_minimax = calculate_model_cost("minimax-m3.1-flash", 1000, 500)
    assert cost_minimax > 0.0

    cost_minimax_preview = calculate_model_cost("minimax-m3.1-flash-preview", 1000, 500)
    assert cost_minimax_preview > 0.0


def test_calculate_model_cost_with_provider_prefix():
    cost_openrouter = calculate_model_cost("openai/gpt-4o", 1000, 500)
    assert cost_openrouter > 0.0

    cost_ds_slash = calculate_model_cost("deepseek/deepseek-chat", 1000, 500)
    assert cost_ds_slash > 0.0


def test_calculate_model_cost_unknown_fallback():
    cost_unknown = calculate_model_cost("some-totally-invented-model-12345", 1000, 500)
    assert cost_unknown == 0.0


@pytest.mark.asyncio
async def test_statistics_manager_summary_and_range_cost(monkeypatch):
    monkeypatch.setattr(AIDailyStatistics, "get_stats_between", AsyncMock(return_value=[]))
    monkeypatch.setattr(AITokenUsageByModel, "get_data_between", AsyncMock(return_value=[]))
    monkeypatch.setattr(AITokenUsageByType, "get_data_between", AsyncMock(return_value=[]))

    statistics_manager.record_token_usage(
        model_name="gpt-4o",
        chat_type="group",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_write_tokens=100,
    )
    summary = statistics_manager.get_summary()
    assert "token_usage" in summary
    assert "total_cost" in summary["token_usage"]
    assert summary["token_usage"]["total_cost"] > 0.0

    by_model = summary["token_usage"]["by_model"]
    assert len(by_model) > 0
    gpt_item = next((item for item in by_model if item["model"] == "gpt-4o"), None)
    assert gpt_item is not None
    assert "cost" in gpt_item
    assert gpt_item["cost"] > 0.0

    today = datetime.now().strftime("%Y-%m-%d")
    range_data = await statistics_manager.get_token_usage_by_range(today, today)
    assert "total" in range_data
    assert "total_cost" in range_data["total"]
    assert range_data["total"]["total_cost"] > 0.0

    assert len(range_data["daily"]) == 1
    assert "total_cost" in range_data["daily"][0]
    assert range_data["daily"][0]["total_cost"] > 0.0

    assert len(range_data["by_model"]) > 0
    assert "cost" in range_data["by_model"][0]
    assert range_data["by_model"][0]["cost"] > 0.0

    daily_counts = await statistics_manager.get_daily_token_counts(days=7)
    assert len(daily_counts) == 7
    today_count = daily_counts[-1]
    assert "total_cost" in today_count
    assert today_count["total_cost"] > 0.0
