"""自然语言任务引用解析

用户问"昨天那个周报任务怎么样了"、"停止那个周报任务"时，框架据此把自然语言
引用解析到候选 ``AIAgentTask`` 根任务——**LLM 全程看不到也不回传 UUID**。

仅解析根任务（``node_kind="root"``），子任务由 ``"<root_ref>#sub<N>"`` 形式
在 ``kanban_tools._resolve_subtask`` 内二次解析。
多候选时由调用方向用户澄清，禁止 LLM 猜。
"""

import re
from typing import List, Optional

from .models import AIAgentTask

# 序号引用："任务#3" / "事项#3" / "task#3" / "#3" / "第3个"
_ORDINAL_RE = re.compile(r"(?:(?:任务|事项|task)\s*#?|#\s*|第\s*)([0-9一二三四五六七八九十]+)\s*个?", re.IGNORECASE)
_DLG_RE = re.compile(r"(?:\bdlg_)?([0-9a-fA-F]{8}-[0-9a-fA-F-]{8,}|[0-9a-fA-F]{12,})\b")
_TASK_STOPWORDS: frozenset[str] = frozenset(
    {
        "那个",
        "那条",
        "这个",
        "这条",
        "刚才",
        "之前",
        "最近",
        "上次",
        "上回",
        "昨天",
        "进行",
        "行中",
        "运行",
        "在跑",
        "暂停",
        "停止",
        "停掉",
        "取消",
        "关闭",
        "重派",
        "修改",
        "改成",
        "改为",
        "恢复",
        "任务",
        "事项",
        "子任",
        "帮我",
        "一下",
    }
)
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


def _query_terms(q: str) -> list[str]:
    chunks = [c for c in re.split(r"[\s,，。！？!?/\\|：:;；#()（）「」『』\"']+", q) if len(c) >= 2]
    terms: list[str] = []
    for chunk in chunks:
        if chunk.isascii():
            if chunk not in _TASK_STOPWORDS and chunk not in terms:
                terms.append(chunk)
            continue
        if len(chunk) == 2:
            if chunk not in _TASK_STOPWORDS and chunk not in terms:
                terms.append(chunk)
            continue
        for i in range(len(chunk) - 1):
            bg = chunk[i : i + 2]
            if bg not in _TASK_STOPWORDS and bg not in terms:
                terms.append(bg)
    return terms


def _match_score(task: AIAgentTask, q: str, terms: list[str]) -> int:
    score = 0
    if task.task_alias and (task.task_alias in q or q in task.task_alias):
        score += 10
    if task.display_name and task.display_name in q:
        score += 8
    haystack = f"{task.task_alias or ''} {task.display_name or ''} {(task.goal or '')[:200]}".lower()
    for term in terms:
        t_low = term.lower()
        if t_low in haystack:
            score += 3 if len(t_low) >= 3 else 1
    return score


async def resolve_task_ref(
    query: str,
    owner_user_id: str,
    scope_key: str = "",
) -> List[AIAgentTask]:
    """把一句自然语言引用解析为候选任务列表。

    解析优先级：dlg_/UUID 命中 > 序号命中 > 别名/关键词命中 > 状态词命中 > "最近/那个"取最新。
    返回 0 个表示未识别；返回 >1 个表示需要向用户澄清。
    """
    _ = scope_key
    tasks = await AIAgentTask.list_for_owner(owner_user_id, root_only=True)
    if not tasks:
        return []
    q = query.strip()
    if not q:
        return [tasks[0]]

    # 0) dlg_ / UUID 前缀直命
    dm = _DLG_RE.search(q)
    if dm is not None:
        raw_id = dm.group(1).lower()
        id_hits = [t for t in tasks if t.id.lower() == raw_id or t.id.lower().startswith(raw_id)]
        if id_hits:
            return id_hits

    # 1) 序号引用（含纯数字串）
    if q.isdigit():
        ord_hits = [t for t in tasks if t.ordinal == int(q)]
        if ord_hits:
            return ord_hits
    m = _ORDINAL_RE.search(q)
    if m:
        n = _parse_cn_int(m.group(1))
        if n is not None:
            hit = [t for t in tasks if t.ordinal == n]
            if hit:
                return hit

    # 2) 别名 / 显示名 / 任务目标关键词双向评分
    terms = _query_terms(q)
    scored = [(t, _match_score(t, q, terms)) for t in tasks]
    best_score = max((s for _, s in scored), default=0)
    name_hits = [t for t, s in scored if s > 0 and s == best_score]
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
