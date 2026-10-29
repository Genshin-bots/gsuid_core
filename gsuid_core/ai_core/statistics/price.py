"""AI 模块模型价格估算工具

基于 genai-prices 库计算模型调用的 Token 费用（美元 USD）。
"""

import re
from typing import Optional

from genai_prices import Usage, calc_price

_CLEAN_SUFFIX_RE = re.compile(r"-(fast|latest|preview|exp|turbo|flash|chat)$", re.IGNORECASE)
_VERSION_RE = re.compile(r"([a-z]+)(\d+)\.\d+", re.IGNORECASE)

# 补充未及时被上游 genai-prices 录入的主流模型定价 (单价为每 1M Token 的美元 USD)
# 元组: (input_per_1m, output_per_1m, cache_read_per_1m, cache_write_per_1m)
SUPPLEMENTAL_MODEL_PRICES: dict[str, tuple[float, float, float, float]] = {
    # MiniMax M3.1 Flash: 输入 $0.15/1M, 输出 $0.60/1M, 缓存读 $0.015/1M
    "minimax-m3.1-flash": (0.15, 0.60, 0.015, 0.0),
    "minimax-m3.1-flash-preview": (0.15, 0.60, 0.015, 0.0),
    "minimax/minimax-m3.1-flash": (0.15, 0.60, 0.015, 0.0),
    "minimax/minimax-m3.1-flash-preview": (0.15, 0.60, 0.015, 0.0),
}


def _generate_model_candidates(model_name: str) -> list[tuple[str, Optional[str]]]:
    """生成用于 genai-prices 查询的模型候选列表 (model_ref, provider_id)"""
    trimmed = model_name.strip()
    if not trimmed:
        return []

    provider: Optional[str] = None
    sub_model = trimmed
    if "/" in trimmed:
        parts = trimmed.split("/", 1)
        provider = parts[0].strip().lower()
        sub_model = parts[1].strip()

    model_variants: list[str] = [sub_model]

    # 逐级剥离修饰后缀 (如 -preview, -fast, -flash)
    curr = sub_model
    while True:
        next_curr = _CLEAN_SUFFIX_RE.sub("", curr)
        if next_curr == curr:
            break
        if next_curr not in model_variants:
            model_variants.append(next_curr)
        curr = next_curr

    # 对剥离后的各变体尝试版本降级 (如 m3.1 -> m3, v4.1 -> v4)
    for v in list(model_variants):
        v_clean = _VERSION_RE.sub(r"\1\2", v)
        if v_clean not in model_variants:
            model_variants.append(v_clean)

    candidates: list[tuple[str, Optional[str]]] = []
    # 优先原样查询
    candidates.append((trimmed, None))
    if provider:
        candidates.append((trimmed, "openrouter"))
        for v in model_variants:
            candidates.append((v, provider))
            candidates.append((v, None))
    else:
        for v in model_variants:
            if v != trimmed:
                candidates.append((v, None))

    return candidates


def calculate_model_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """使用 genai-prices 及补充价格表估算模型调用 Token 费用 (美元 USD)

    找不到对应模型价格或 Token 全为 0 时返回 0.0。
    """
    if input_tokens <= 0 and output_tokens <= 0 and cache_read_tokens <= 0 and cache_write_tokens <= 0:
        return 0.0

    usage = Usage(
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
        cache_read_tokens=max(0, cache_read_tokens),
        cache_write_tokens=max(0, cache_write_tokens),
    )

    for cand_model, cand_provider in _generate_model_candidates(model_name):
        # 1. 优先匹配补充价格表
        key = cand_model.lower()
        if key in SUPPLEMENTAL_MODEL_PRICES:
            inp_p, out_p, cr_p, cw_p = SUPPLEMENTAL_MODEL_PRICES[key]
            cost = (
                (input_tokens * inp_p)
                + (output_tokens * out_p)
                + (cache_read_tokens * cr_p)
                + (cache_write_tokens * cw_p)
            ) / 1_000_000.0
            return round(cost, 6)

        # 2. 尝试 genai_prices 库匹配
        try:
            res = (
                calc_price(usage, cand_model, provider_id=cand_provider)
                if cand_provider
                else calc_price(usage, cand_model)
            )
            return round(float(res.total_price), 6)
        except (LookupError, ValueError):
            continue

    return 0.0
