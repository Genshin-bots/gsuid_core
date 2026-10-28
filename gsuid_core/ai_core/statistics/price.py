"""AI 模块模型价格估算工具

基于 genai-prices 库计算模型调用的 Token 费用（美元 USD）。
"""

import re
from typing import Optional

from genai_prices import Usage, calc_price

_CLEAN_SUFFIX_RE = re.compile(r"-(fast|latest|preview|exp|turbo)$", re.IGNORECASE)
_VERSION_RE = re.compile(r"([a-z]+)(\d+)\.\d+", re.IGNORECASE)


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
    cleaned = _CLEAN_SUFFIX_RE.sub("", sub_model)
    if cleaned != sub_model and cleaned not in model_variants:
        model_variants.append(cleaned)

    v_clean = _VERSION_RE.sub(r"\1\2", cleaned)
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
    """使用 genai-prices 估算模型调用 Token 费用 (美元 USD)

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
        try:
            # genai_prices 找不到模型时抛出 LookupError 或 ValueError，按序尝试候选
            res = (
                calc_price(usage, cand_model, provider_id=cand_provider)
                if cand_provider
                else calc_price(usage, cand_model)
            )
            return round(float(res.total_price), 6)
        except (LookupError, ValueError):
            continue

    return 0.0
