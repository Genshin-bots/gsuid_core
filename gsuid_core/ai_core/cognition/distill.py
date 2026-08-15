"""执行世界 → 认知世界的回流蒸馏。

Kanban / 落盘结论有 TTL，到期后正文消失。这里只留结构化摘要节点
（``source=self_action``），让过期后仍能召回「上次算出什么」。
助手台词不进事实图；失败只丢节点，不丢 FileOS / Artifact 真身。
"""

import re
import hashlib
from typing import List, Optional

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.cognition.nodes import CogEdgeKind, sync_node, link_nodes
from gsuid_core.ai_core.cognition.types import CogKind

# 值得回流的落盘特征：含数字 / 承诺 / 稳定属性。纯叙事长文不回流（噪声大、价值低）。
_WORTH_DISTILL_RE = re.compile(r"(\d[\d,.%]*|承诺|约定|结论|方案|配置|阈值|上限|下限|口径)")
# 单条结论的长度上限：节点只存摘要，不存正文
_SUMMARY_MAX = 200
# 太短的片段没有可回想的信息量；与关系温度的 meaningful 阈值同源
_MIN_DISTILL_LEN = 12


def is_worth_distilling(text: str) -> bool:
    """纯规则门：这段落盘/产物值不值得留一条结论节点。"""
    body = (text or "").strip()
    if len(body) < _MIN_DISTILL_LEN:
        return False
    return bool(_WORTH_DISTILL_RE.search(body))


def _clip(text: str) -> str:
    body = " ".join((text or "").split())
    return body[:_SUMMARY_MAX]


async def distill_tool_output(
    *,
    record_id: str,
    tool_name: str,
    summary: str,
    scope_key: str,
    owner_user_id: str,
    as_of: str,
) -> Optional[int]:
    """FileOS 落盘成功后登记 ``tool_output`` 节点（+ 值得时补一条 ``fact``）。

    落盘**不晋升为知识库**（落盘会过时，这是有意的）；这里只留「结论边」，
    使 30 天 TTL 到期后仍能召回「上次算出什么」，正文取不到时如实说过时。

    ``owner_user_id`` 必填：FileOS 真身是行级 owner 过滤的，节点层漏了它就等于
    把同一份数据的 ACL 从 owner 级降到 group 级。
    """
    node_id = await sync_node(
        CogKind.TOOL_OUTPUT,
        record_id,
        scope_key=scope_key,
        owner_user_id=owner_user_id,
        title=tool_name or "落盘",
        summary=_clip(summary),
        as_of=as_of,
        source="tool",
        handle=record_id,
    )
    if node_id is None or not is_worth_distilling(summary):
        return node_id
    fact_ref = f"tool:{record_id}"
    fact_id = await sync_node(
        CogKind.FACT,
        fact_ref,
        scope_key=scope_key,
        owner_user_id=owner_user_id,
        title=_clip(summary)[:60],
        summary=_clip(summary),
        as_of=as_of,
        # self_action：来源是「我做过的事」，不是群友陈述的事实
        source="self_action",
    )
    if fact_id is not None:
        await link_nodes(
            (CogKind.FACT, fact_ref),
            (CogKind.TOOL_OUTPUT, record_id),
            CogEdgeKind.DERIVED_FROM,
        )
    logger.debug(i18n_t("log.ai.cognition_distilled_tool_output", rid=record_id, tool=tool_name or "-"))
    return node_id


async def distill_task_terminal(
    *,
    root_task_id: str,
    goal: str,
    status: str,
    conclusion: str,
    scope_key: str,
    owner_user_id: str,
    artifact_ids: List[str],
    as_of: str,
) -> Optional[int]:
    """Kanban 根任务终态（完成/失败）回流：目标 + 结论摘要 → ``episode`` (+ ``fact``)。

    挂 ``DERIVED_FROM`` 到相关 artifact，使 artifact TTL 过期后结论仍可召回。
    整棵任务日志**不回流**——那是流水，不是认知。

    ``owner_user_id`` 必填：Kanban 的 ``scope_key`` 是 ``group:{gid}``，只按它过滤
    会让同群任何人都能召回别人的任务结论与产物摘要。
    """
    ref = f"task:{root_task_id}"
    episode_id = await sync_node(
        CogKind.EPISODE,
        ref,
        scope_key=scope_key,
        owner_user_id=owner_user_id,
        title=_clip(goal)[:60],
        summary=_clip(f"[{status}] {goal} → {conclusion}"),
        as_of=as_of,
        source="self_action",
    )
    if episode_id is None:
        return None
    for res_id in artifact_ids[:5]:
        await sync_node(
            CogKind.ARTIFACT,
            res_id,
            scope_key=scope_key,
            owner_user_id=owner_user_id,
            title="任务产物",
            summary=_clip(conclusion),
            as_of=as_of,
            source="self_action",
            handle=res_id,
        )
        await link_nodes((CogKind.EPISODE, ref), (CogKind.ARTIFACT, res_id), CogEdgeKind.DERIVED_FROM)
    if is_worth_distilling(conclusion):
        fact_ref = f"task_fact:{root_task_id}"
        fact_id = await sync_node(
            CogKind.FACT,
            fact_ref,
            scope_key=scope_key,
            owner_user_id=owner_user_id,
            title=_clip(conclusion)[:60],
            summary=_clip(conclusion),
            as_of=as_of,
            source="self_action",
        )
        if fact_id is not None:
            await link_nodes((CogKind.FACT, fact_ref), (CogKind.EPISODE, ref), CogEdgeKind.SUPPORTS)
    logger.info(i18n_t("log.ai.cognition_distilled_task", task=root_task_id, status=status))
    return episode_id


async def distill_self_note(*, note: str, note_type: str, bot_id: str) -> Optional[int]:
    """``update_self_note`` 写入后同步 ``self_note`` 节点。

    与 ``AIMemPreference`` 的去重方向：**称呼/禁忌类规则优先落 preference**，
    self_note 只留非规则的反思——否则同一件事会有两份平行记录且无人对账。
    """
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

    # 必须用**稳定**摘要：Python 的 str.__hash__ 带 PYTHONHASHSEED 随机化，进程重启后
    # 同一条 note 会算出不同 ref，(kind, ref) 幂等键失效 → 每次重启都堆一份重复节点。
    digest = hashlib.sha1(note.encode("utf-8")).hexdigest()[:16]
    ref = f"self:{bot_id}:{note_type}:{digest}"
    return await sync_node(
        CogKind.SELF_NOTE,
        ref,
        scope_key=make_scope_key(ScopeType.SELF, bot_id or "default"),
        title=note_type,
        summary=_clip(note),
        source="self_action",
    )
