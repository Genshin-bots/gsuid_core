"""
AI Statistics APIs
提供 AI 模块统计数据相关的 RESTful APIs

包括 Token 消耗、费用估算、延迟统计、意图分布、Heartbeat 决策等。
"""

from typing import Any, Dict, Optional
from datetime import datetime, timedelta

from fastapi import Depends

from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import AI_STATS


@app.get("/api/ai/statistics/summary", summary="获取统计数据摘要", tags=AI_STATS)
async def get_ai_statistics_summary(
    date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 AI 统计数据摘要

    返回 AI 模块的核心统计数据，包括 Token 消耗、费用、延迟、意图分布等。

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 统计摘要数据
    """
    try:
        # 如果指定了日期
        if date:
            # 如果是今天，优先从内存获取实时数据
            if date == statistics_manager._today:
                result = statistics_manager.get_summary()
                if result:
                    return {"status": 0, "msg": "ok", "data": result}

            # 如果内存没有，从数据库查询
            result = await statistics_manager.get_summary_by_date(date)
            if result:
                return {"status": 0, "msg": "ok", "data": result}
            return {
                "status": 1,
                "msg": f"未找到 {date} 日期的统计数据",
                "data": None,
            }

        # 没有指定日期时，返回今日内存中的数据
        result = statistics_manager.get_summary()
        if result:
            return {"status": 0, "msg": "ok", "data": result}

        return {
            "status": 1,
            "msg": "未找到统计数据",
            "data": None,
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取统计数据失败: {str(e)}",
            "data": None,
        }


@app.get("/api/ai/statistics/token-by-model", summary="获取按模型分组的 Token 消耗", tags=AI_STATS)
async def get_token_usage_by_model(
    date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取按模型分组的 Token 消耗统计

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 按模型分组的 Token 消耗列表
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 如果是今天，优先从内存获取实时数据
        if date == statistics_manager._today:
            summary = statistics_manager.get_summary()
            return {"status": 0, "msg": "ok", "data": summary.get("token_usage", {}).get("by_model", [])}

        # 如果不是今天，从数据库查询
        summary = await statistics_manager.get_summary_by_date(date)
        if summary:
            return {"status": 0, "msg": "ok", "data": summary.get("token_usage", {}).get("by_model", [])}
        return {"status": 1, "msg": f"未找到 {date} 日期的统计数据", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"获取 Token 消耗失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/token-by-range", summary="获取时间段 Token 消耗统计", tags=AI_STATS)
async def get_token_usage_by_range(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定时间段的 Token 消耗统计

    对 [start_date, end_date] 闭区间逐日聚合，返回时间段总量、按天趋势
    以及跨天的按模型分布。今日数据取内存实时值，历史数据从数据库读取，
    区间内无数据的日期以 0 补齐，保证 daily 序列连续。

    Args:
        start_date: 开始日期，格式为 "YYYY-MM-DD"，默认为 6 天前（即默认近 7 天）
        end_date: 结束日期，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 包含 total（总量）、daily（按天趋势）、by_model（按模型分布）的统计
    """
    try:
        now = datetime.now()
        if end_date is None:
            end_date = now.strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (now - timedelta(days=6)).strftime("%Y-%m-%d")

        # 校验日期格式，非法时返回友好错误
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return {
                "status": 1,
                "msg": "日期格式错误，应为 YYYY-MM-DD",
                "data": None,
            }

        result = await statistics_manager.get_token_usage_by_range(start_date, end_date)
        return {"status": 0, "msg": "ok", "data": result}
    except Exception as e:
        return {"status": 1, "msg": f"获取时间段 Token 统计失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/active-users", summary="获取活跃用户/群组排行", tags=AI_STATS)
async def get_active_users(
    date: Optional[str] = None,
    limit: int = 20,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取活跃用户/群组排行

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天
        limit: 返回数量限制

    Returns:
        status: 0成功，1失败
        data: 活跃用户/群组列表
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 如果是今天，优先从内存获取实时数据
        if date == statistics_manager._today:
            summary = statistics_manager.get_summary()
            return {"status": 0, "msg": "ok", "data": summary.get("active_users", [])[:limit]}

        # 如果不是今天，从数据库查询
        summary = await statistics_manager.get_summary_by_date(date)
        if summary:
            return {"status": 0, "msg": "ok", "data": summary.get("active_users", [])[:limit]}
        return {"status": 1, "msg": f"未找到 {date} 日期的统计数据", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"获取活跃用户失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/trigger-distribution", summary="获取触发方式占比", tags=AI_STATS)
async def get_trigger_distribution(
    date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取触发方式占比

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 触发方式分布
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 如果是今天，优先从内存获取实时数据
        if date == statistics_manager._today:
            summary = statistics_manager.get_summary()
            return {"status": 0, "msg": "ok", "data": summary.get("trigger_distribution", {})}

        # 如果不是今天，从数据库查询
        summary = await statistics_manager.get_summary_by_date(date)
        if summary:
            return {"status": 0, "msg": "ok", "data": summary.get("trigger_distribution", {})}
        return {"status": 1, "msg": f"未找到 {date} 日期的统计数据", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"获取触发方式失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/intent-distribution", summary="获取意图分布统计", tags=AI_STATS)
async def get_intent_distribution(
    date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取意图分布统计

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 意图分布
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 如果是今天，优先从内存获取实时数据
        if date == statistics_manager._today:
            summary = statistics_manager.get_summary()
            return {"status": 0, "msg": "ok", "data": summary.get("intent_distribution", {})}

        # 如果不是今天，从数据库查询
        summary = await statistics_manager.get_summary_by_date(date)
        if summary:
            return {"status": 0, "msg": "ok", "data": summary.get("intent_distribution", {})}
        return {"status": 1, "msg": f"未找到 {date} 日期的统计数据", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"获取意图分布失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/errors", summary="获取错误统计", tags=AI_STATS)
async def get_error_stats(
    date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取错误统计

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: 错误统计
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 如果是今天，优先从内存获取实时数据
        if date == statistics_manager._today:
            summary = statistics_manager.get_summary()
            return {"status": 0, "msg": "ok", "data": summary.get("errors", {})}

        # 如果不是今天，从数据库查询
        summary = await statistics_manager.get_summary_by_date(date)
        if summary:
            return {"status": 0, "msg": "ok", "data": summary.get("errors", {})}
        return {"status": 1, "msg": f"未找到 {date} 日期的统计数据", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"获取错误统计失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/heartbeat", summary="获取 Heartbeat 巡检统计", tags=AI_STATS)
async def get_heartbeat_stats(
    date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 Heartbeat 巡检统计

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: Heartbeat 统计
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 如果是今天，优先从内存获取实时数据
        if date == statistics_manager._today:
            summary = statistics_manager.get_summary()
            return {"status": 0, "msg": "ok", "data": summary.get("heartbeat", {})}

        # 如果不是今天，从数据库查询
        summary = await statistics_manager.get_summary_by_date(date)
        if summary:
            return {"status": 0, "msg": "ok", "data": summary.get("heartbeat", {})}
        return {"status": 1, "msg": f"未找到 {date} 日期的统计数据", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"获取 Heartbeat 统计失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/rag", summary="获取 RAG 知识库效果统计", tags=AI_STATS)
async def get_rag_stats(
    date: Optional[str] = None,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 RAG 知识库效果统计

    RAG 统计是全局数据，不区分 bot_id。

    Args:
        date: 日期字符串，格式为 "YYYY-MM-DD"，默认为今天

    Returns:
        status: 0成功，1失败
        data: RAG 统计
    """
    try:
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 如果是今天，优先从内存获取实时数据
        if date == statistics_manager._today:
            summary = statistics_manager.get_summary()
            return {"status": 0, "msg": "ok", "data": summary.get("rag", {})}

        # 如果不是今天，从数据库查询
        summary = await statistics_manager.get_summary_by_date(date)
        if summary:
            return {"status": 0, "msg": "ok", "data": summary.get("rag", {})}
        return {"status": 1, "msg": f"未找到 {date} 日期的统计数据", "data": None}
    except Exception as e:
        return {"status": 1, "msg": f"获取 RAG 统计失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/rag/documents", summary="获取 RAG 文档命中统计", tags=AI_STATS)
async def get_rag_document_stats(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取 RAG 文档命中统计

    注意：此接口返回的是累计数据，不区分日期。

    Returns:
        status: 0成功，1失败
        data: 文档命中统计列表
    """
    try:
        doc_stats = statistics_manager.get_rag_document_stats()
        return {"status": 0, "msg": "ok", "data": doc_stats}
    except Exception as e:
        return {"status": 1, "msg": f"获取 RAG 文档统计失败: {str(e)}", "data": None}


@app.get("/api/ai/statistics/history", summary="获取历史统计数据", tags=AI_STATS)
async def get_statistics_history(
    days: int = 7,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取历史统计数据

    Args:
        days: 查询天数，默认为 7

    Returns:
        status: 0成功，1失败
        data: 历史统计数据列表
    """
    try:
        result = []
        now = datetime.now()

        for i in range(days - 1, -1, -1):
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")

            # 今天是最后一天，使用内存数据
            if i == 0:
                summary = statistics_manager.get_summary()
                day_data = {"date": date, "data": summary}
            else:
                # 历史数据从数据库读取
                summary = await statistics_manager.get_summary_by_date(date)
                day_data = {"date": date, "data": summary}

            result.append(day_data)

        return {"status": 0, "msg": "ok", "data": result}
    except Exception as e:
        return {"status": 1, "msg": f"获取历史统计失败: {str(e)}", "data": None}
