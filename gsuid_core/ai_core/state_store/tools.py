"""
通用持久状态存储 - AI 工具

向 Agent 暴露 state_get / state_set / state_delete / state_list / state_append 五个工具。
其中 bootstrap 读写对 state_set / state_get 为框架保底工具（任何 session 默认注入）；
低频的 state_list / state_delete / state_append 降为检索池工具，靠能力族（持久状态）/ 会话驻留 /
向量检索按需召回——用到 KV 写读时整族带出，避免每轮闲聊都常驻 5 个 state_* 抬高 Token。
（state_list 仅用于"任务初始化没"这类判断，频率低于 set/get，故不进保底。）
"""

import json
from typing import Any, Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

from .store import (
    state_get_value,
    state_list_keys,
    state_set_value,
    state_append_item,
    state_delete_value,
)


def _resolve_scope(ctx: RunContext[ToolContext], scope: Optional[str]) -> str:
    """将 Agent 传入的 scope 解析为实际的隔离 key。

    传入 "auto"、空或 None 时，根据当前会话上下文自动推断：
    - 群聊 → "group:{group_id}"
    - 私聊 → "user:{user_id}"
    其它情况按 Agent 显式传入的 scope 原样使用（如 "global"、"user:123"）。
    """
    if scope and scope.strip().lower() not in ("auto", ""):
        return scope.strip()

    ev = ctx.deps.ev
    if ev is not None:
        if ev.group_id:
            return f"group:{ev.group_id}"
        if ev.user_id:
            return f"user:{ev.user_id}"
    return "global"


def _parse_value(value: str) -> Any:
    """尝试将字符串值解析为 JSON 结构，失败则按纯字符串保留。"""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


@ai_tools(category="buildin", capability_domain="持久状态")
async def state_set(
    ctx: RunContext[ToolContext],
    key: str,
    value: str,
    scope: str = "auto",
    ttl_days: Optional[int] = None,
) -> str:
    """
    写入一个跨会话持久化的键值数据。

    用于保存任务的结构化状态（如虚拟账户余额、任务进度、报名名单等），
    这些数据在会话结束后依然存在，可在后续对话或定时任务中读回。

    Args:
        key: 键名，建议格式 "插件名:业务名"，如 "myplugin:progress"
        value: 要保存的值，传入 JSON 字符串（对象/数组/数字/字符串均可）
        scope: 数据隔离范围。"auto"=按当前会话自动判断（群聊/私聊）；
               也可显式传入 "user:用户ID"、"group:群ID"、"global"
        ttl_days: 可选，数据保留天数，不填则永久保留

    Returns:
        操作结果描述
    """
    real_scope = _resolve_scope(ctx, scope)
    parsed = _parse_value(value)
    try:
        version = await state_set_value(real_scope, key, parsed, ttl_days=ttl_days)
        return f"已保存状态 [{key}] (scope={real_scope}, 版本v{version})"
    except Exception as e:
        logger.exception(t("🗄️ [StateStore] state_set 失败: {e}", e=e))
        return f"保存失败: {e}"


@ai_tools(category="buildin", capability_domain="持久状态")
async def state_get(
    ctx: RunContext[ToolContext],
    key: str,
    scope: str = "auto",
) -> str:
    """
    读取一个跨会话持久化的键值数据。

    用于在对话或定时任务中读回之前用 state_set 保存的状态。

    Args:
        key: 键名，与写入时一致
        scope: 数据隔离范围，规则同 state_set

    Returns:
        JSON 格式的值；键不存在时返回提示
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        value = await state_get_value(real_scope, key)
        if value is None:
            return f"状态 [{key}] 不存在 (scope={real_scope})"
        return json.dumps(value, ensure_ascii=False)
    except Exception as e:
        logger.exception(t("🗄️ [StateStore] state_get 失败: {e}", e=e))
        return f"读取失败: {e}"


@ai_tools(category="common", capability_domain="持久状态")
async def state_delete(
    ctx: RunContext[ToolContext],
    key: str,
    scope: str = "auto",
) -> str:
    """
    删除一个持久化的键值数据。

    Args:
        key: 要删除的键名
        scope: 数据隔离范围，规则同 state_set

    Returns:
        操作结果描述
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        deleted = await state_delete_value(real_scope, key)
        if deleted:
            return f"已删除状态 [{key}] (scope={real_scope})"
        return f"状态 [{key}] 不存在，无需删除"
    except Exception as e:
        logger.exception(t("🗄️ [StateStore] state_delete 失败: {e}", e=e))
        return f"删除失败: {e}"


@ai_tools(category="common", capability_domain="持久状态")
async def state_list(
    ctx: RunContext[ToolContext],
    prefix: str = "",
    scope: str = "auto",
) -> str:
    """
    列出某个范围下的所有持久化状态键。

    用于确认某项任务是否已初始化（如检查某插件前缀下是否已有键）。

    Args:
        prefix: 可选，只返回以此前缀开头的键
        scope: 数据隔离范围，规则同 state_set

    Returns:
        键名列表
    """
    real_scope = _resolve_scope(ctx, scope)
    try:
        keys = await state_list_keys(real_scope, prefix=prefix)
        if not keys:
            return f"范围 {real_scope} 下没有匹配的状态键"
        return f"范围 {real_scope} 下的状态键: {', '.join(keys)}"
    except Exception as e:
        logger.exception(t("🗄️ [StateStore] state_list 失败: {e}", e=e))
        return f"列出失败: {e}"


@ai_tools(category="common", capability_domain="持久状态")
async def state_append(
    ctx: RunContext[ToolContext],
    key: str,
    item: str,
    scope: str = "auto",
    max_length: Optional[int] = None,
    ttl_days: Optional[int] = None,
) -> str:
    """
    向一个列表型的持久化状态追加一个元素。

    适合追加操作历史、交易记录、报名条目等，自动处理"键不存在则创建列表"，
    并避免手动 get→修改→set 带来的竞态风险。

    Args:
        key: 列表键名
        item: 要追加的元素，传入 JSON 字符串
        scope: 数据隔离范围，规则同 state_set
        max_length: 可选，列表最大长度，超出时自动丢弃最旧的元素
        ttl_days: 可选，数据保留天数

    Returns:
        操作结果描述（含追加后的列表长度）
    """
    real_scope = _resolve_scope(ctx, scope)
    parsed = _parse_value(item)
    try:
        length = await state_append_item(real_scope, key, parsed, max_length=max_length, ttl_days=ttl_days)
        return f"已追加到 [{key}] (scope={real_scope}, 当前长度={length})"
    except Exception as e:
        logger.exception(t("🗄️ [StateStore] state_append 失败: {e}", e=e))
        return f"追加失败: {e}"
