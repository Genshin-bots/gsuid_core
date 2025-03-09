import math
from typing import List


def generate_y_ticks(values: List[float]) -> List[float]:
    if not values:
        return []

    max_val = max(values)
    avg_val = sum(values) / len(values)

    if max_val == 0:
        return [0.0] * 6

    # 计算d_min，确保步长下最高刻度和中间刻度满足条件
    d_min = max(max_val / 5, avg_val / 3)

    # 候选基数列表，用于生成整洁的步长
    candidates = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 9, 10]

    # 计算数量级和基数
    exponent = math.floor(math.log10(d_min)) if d_min > 0 else 0
    base = d_min / (10**exponent) if d_min > 0 else 0

    # 寻找合适的候选基数
    selected_base = None
    for c in candidates:
        if c >= base:
            selected_base = c
            break
    # 如果没有更大的基数，则使用最小基数并增加数量级
    if selected_base is None:
        selected_base = candidates[0]
        exponent += 1

    # 初始步长候选
    step_candidate = selected_base * (10**exponent)

    # 确保步长满足条件
    while True:
        if 5 * step_candidate > max_val and 3 * step_candidate > avg_val:
            break
        # 寻找下一个候选基数或增加数量级
        current_index = candidates.index(selected_base)
        if current_index < len(candidates) - 1:
            selected_base = candidates[current_index + 1]
        else:
            selected_base = candidates[0]
            exponent += 1
        step_candidate = selected_base * (10**exponent)

    # 生成等距的六个刻度值
    ticks = [i * step_candidate for i in range(6)]
    return ticks
