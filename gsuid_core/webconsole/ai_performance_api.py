"""
AI Performance APIs
提供 AI 模块小时级性能统计相关的 RESTful APIs

包括 TTFT、TPS、Token 消耗、工具调用次数等按小时聚合的统计。
"""

from typing import Dict, Optional
from datetime import datetime, timedelta

from fastapi import Depends

from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.statistics.models import AIHourlyPerformance


@app.get("/api/ai/performance/hourly")
async def get_hourly_performance(
    date: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取按小时分组的 AI 性能统计

    返回指定日期各小时段的 TTFT、TPS、Token 消耗、工具调用次数等。
    按提供商和模型名称分组，DB 基线数据与内存未持久化增量自动合并。

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 按小时分组的性能统计列表
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        result = await statistics_manager.get_hourly_performance_by_date(date)
        return {"status": 0, "msg": "ok", "data": result}
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取小时性能统计失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/performance/hourly/range")
async def get_hourly_performance_range(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定日期范围的小时级性能统计

    Args:
        start_date: 开始日期，格式为 "YYYY-MM-DD"，默认为7天前
        end_date: 结束日期，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 日期-小时分组的数据列表
    """
    try:
        now = datetime.now()
        if end_date is None:
            end_date = now.strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")

        data = await AIHourlyPerformance.get_range_data(start_date, end_date)

        result = []
        for row in data:
            result.append(
                {
                    "date": row.date,
                    "hour": row.hour,
                    "provider": row.provider,
                    "model": row.model_name,
                    "request_count": row.request_count,
                    "ttft_min_ms": row.ttft_min_ms,
                    "ttft_max_ms": row.ttft_max_ms,
                    "ttft_avg_ms": round(row.ttft_sum_ms / row.ttft_sample_count, 2)
                    if row.ttft_sample_count > 0
                    else 0.0,
                    "tps_min": row.tps_min,
                    "tps_max": row.tps_max,
                    "tps_avg": round(row.tps_sum / row.tps_sample_count, 2) if row.tps_sample_count > 0 else 0.0,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "cache_read_tokens": row.cache_read_tokens,
                    "cache_write_tokens": row.cache_write_tokens,
                    "tool_call_count": row.tool_call_count,
                }
            )

        return {"status": 0, "msg": "ok", "data": result}
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取范围性能统计失败: {str(e)}",
            "data": None,
        }
