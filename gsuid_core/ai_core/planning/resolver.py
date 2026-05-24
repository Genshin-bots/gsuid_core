"""自然语言任务引用解析

用户问"昨天那个炒股任务怎么样了"、"停止那个周报任务"时，框架据此把自然语言
引用解析到候选 ``AIAgentTask`` 根任务——**LLM 全程看不到也不回传 UUID**。

仅解析根任务（``node_kind="root"``），子任务由 ``"<root_ref>#sub<N>"`` 形式
在 ``kanban_tools._resolve_subtask`` 内二次解析。
多候选时由调用方向用户澄清，禁止 LLM 猜。
"""

import re
from typing import List, Optional

from .models import AIAgentTask

# 序号引用："任务#3" / "第3个" / "第三个"
_ORDINAL_RE = re.compile(r"(?:任务\s*#?|第)\s*([0-9一二三四五六七八九十]+)\s*个?")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 状态关键词 → 任务 status 集合
_STATUS_HINTS = {
    "运行中": ("running",),
    "进行中": ("running",),
    "在跑": ("running",),
    "等审批": ("waiting_approval",),
    "待审批": ("waiting_approval",),
    "等待审批": ("waiting_approval",),
    "暂停": ("paused",),
    "完成": ("completed",),
    "做完": ("completed",),
    "失败": ("failed",),
}


def _parse_cn_int(token: str) -> Optional[int]:
    if token.isdigit():
        return int(token)
    if token in _CN_NUM:
        return _CN_NUM[token]
    # "十一" 之类的简单组合
    if token.startswith("十") and len(token) == 2 and token[1] in _CN_NUM:
        return 10 + _CN_NUM[token[1]]
    return None


async def resolve_task_ref(
    query: str,
    owner_user_id: str,
    scope_key: str = "",
) -> List[AIAgentTask]:
    """把一句自然语言引用解析为候选任务列表。

    解析优先级：序号命中 > 别名/显示名字面命中 > 状态词命中 > "最近/那个"取最新。
    返回 0 个表示未识别；返回 >1 个表示需要向用户澄清。

    Args:
        query:          用户自然语言（如"昨天那个炒股任务"）
        owner_user_id:  当前用户 ID——只解析其名下任务（建议七权限边界）
        scope_key:      作用域（暂作软过滤，保留扩展位）

    Returns:
        候选任务列表（已去重）
    """
    tasks = await AIAgentTask.list_for_owner(owner_user_id, root_only=True)
    if not tasks:
        return []
    q = query.strip()

    # 1) 序号引用
    m = _ORDINAL_RE.search(q)
    if m:
        n = _parse_cn_int(m.group(1))
        if n is not None:
            hit = [t for t in tasks if t.ordinal == n]
            if hit:
                return hit

    # 2) 别名 / 显示名字面命中
    name_hits = [
        t
        for t in tasks
        if (t.task_alias and t.task_alias in q)
        or (t.display_name and t.display_name in q)
        or any(kw and kw in q for kw in t.goal[:20].split())
    ]
    if len(name_hits) == 1:
        return name_hits

    # 3) 状态词命中
    status_filter: tuple = ()
    for kw, statuses in _STATUS_HINTS.items():
        if kw in q:
            status_filter = statuses
            break
    pool = name_hits if name_hits else tasks
    if status_filter:
        pool = [t for t in pool if t.status in status_filter]

    # 4) "那个 / 上次 / 昨天 / 最近"——在剩余候选里取最新
    if pool and (len(pool) == 1 or re.search(r"(那个|上次|上回|昨天|最近|刚才)", q)):
        # list_for_owner 已按 updated_at 倒序
        return [pool[0]]

    return pool
