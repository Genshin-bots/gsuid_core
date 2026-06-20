"""AI 预算限制全局配置（`budget_config.json`）。

只放「全局开关 / 全局策略」类设置；具体的规则与白名单是动态数据，落库不在这里。
"""

from typing import Dict

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

# count_mode 合法值与对应的「计费 Token」算法
COUNT_MODES = ("input_output", "total_with_cache", "output_only")

DEFAULT_BLOCK_MESSAGE = "⚠️ 当前会话的 AI 使用额度已用完（{window}：{used}/{limit} tokens），请稍后再试。"

BUDGET_CONFIG: Dict[str, GSC] = {
    "enable": GsBoolConfig(
        "启用预算限制",
        "总开关。关闭后所有预算规则不生效（用量仍会记录, 便于先观察再开启）",
        False,
    ),
    "count_mode": GsStrConfig(
        "Token计费方式",
        "决定记入额度的 Token 口径: input_output=输入+输出(默认); "
        "total_with_cache=输入+输出+缓存读写; output_only=仅输出",
        "input_output",
        options=list(COUNT_MODES),
    ),
    "count_exempt_usage": GsBoolConfig(
        "白名单用量计入额度",
        "默认关闭: 白名单/主人消耗的 Token 不占用该会话的共享额度(他们突破限制且不拖累群额度)。"
        "开启则其用量也累加进窗口统计",
        False,
    ),
    "exempt_masters": GsBoolConfig(
        "主人豁免限制",
        "开启后, core 配置中的 masters 永远不受预算限制(等价于自动全局白名单)",
        True,
    ),
    "notify_on_block": GsBoolConfig(
        "超额时提示用户",
        "开启后, 被预算拦截时向当前会话发送一句提示(带冷却防刷屏); 关闭则静默丢弃",
        True,
    ),
    "notify_cooldown": GsIntConfig(
        "超额提示冷却(秒)",
        "同一会话两次超额提示之间的最小间隔, 防止持续刷屏",
        300,
        options=[60, 180, 300, 600, 1800],
    ),
    "block_message": GsStrConfig(
        "超额提示文案",
        "超额拦截时的提示模板, 支持占位符 {scope} {window} {used} {limit} {reset}",
        DEFAULT_BLOCK_MESSAGE,
    ),
}

budget_config = StringConfig(
    "GsCore AI 预算限制配置",
    get_res_path("ai_core") / "budget_config.json",
    BUDGET_CONFIG,
)


def compute_billable_tokens(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    count_mode: str,
) -> int:
    """按 count_mode 计算一次 run 记入额度的「计费 Token」。"""
    if count_mode == "output_only":
        return max(0, output_tokens)
    if count_mode == "total_with_cache":
        return max(0, input_tokens + output_tokens + cache_read_tokens + cache_write_tokens)
    # 默认 input_output
    return max(0, input_tokens + output_tokens)
