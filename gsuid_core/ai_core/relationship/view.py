"""``RelationshipView``：跨计划冻结接口（路线图 §3.2）。

消费方（CheapGate / Heartbeat / 记忆 priority_speakers / 装配 / 好感套件）一律按这个
类型写签名，不各自去查库、也不各自划档。

``is_master`` 放进 View 而不是让每个消费点再查一次 ``_is_master_user``——否则
CheapGate / Heartbeat / 装配三处会各自引权限模块，又是一次「多套实现」。
"""

from typing import TYPE_CHECKING, Set, Dict, Optional, Sequence
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.relationship.zones import Zone, zone_of, zone_voice, render_relationship_line

if TYPE_CHECKING:
    from gsuid_core.message_history.manager import MessageRecord


@dataclass(frozen=True)
class RelationshipView:
    """一次装配所需的关系全貌。**不可变**：消费方不得回写。"""

    score: int
    zone: Zone
    line: str
    is_master: bool
    # None 表示该 (user, bot) 从未被打过分；score 此时为 0 但语义是「未知」而非「陌生」
    scored: bool = True

    @property
    def voice(self) -> str:
        """当前 zone 的口吻例句（装配层注入 user 侧）；**未打分时为空**。

        ``scored=False`` 是「没有依据」，不是「陌生」。此时 ``zone`` 只是 score=0 的
        算术结果（distant），拿它去注入「公事公办、少废话」等于凭空给一个从没打过分的
        群友编一个冷淡立场——与 ``UNSCORED_LINE``（「打过照面的群友」）自相矛盾，
        叠加人设卡的字数上限后会把模型压成拒办。没依据就不要拧这个旋钮。
        """
        if not self.scored:
            return ""
        return zone_voice(self.zone)

    @property
    def is_quiet_zone(self) -> bool:
        """是否处于「群聊未 @ 更倾向沉默」的档位（hostile / cold）。"""
        return self.zone in (Zone.HOSTILE, Zone.COLD)


def view_from_score(score: Optional[int], is_master: bool) -> RelationshipView:
    """纯函数：分数 + 主人身份 → View。评测夹具直接用它注入，不必写 SQL。"""
    effective = 0 if score is None else score
    return RelationshipView(
        score=effective,
        zone=zone_of(effective),
        line=render_relationship_line(score, is_master),
        is_master=is_master,
        scored=score is not None,
    )


async def collect_priority_speakers(
    *,
    bot_id: str,
    group_id: Optional[str],
    history: Sequence["MessageRecord"],
    max_speakers: int = 8,
) -> Set[str]:
    """算出本 scope 的「记忆预算优先发言者」= masters ∪ 在场 close 用户。

    只负责**算出集合**，注入由记忆侧接收（避免 CheapGate / 装配 / 记忆三处各查一次库）。
    集合同时含 user_id 与昵称：``to_prompt_text`` 的优先判据是 edge 的 ``source_name``
    （实体名），只放 id 在真实图谱里几乎命中不到。
    """
    from gsuid_core.config import core_config

    priority: Set[str] = {str(m) for m in (core_config.get_config("masters") or [])}
    if not history:
        return priority

    # 只看最近的几位不同发言者：这是「在场」的操作定义，也给批量查询设上界
    names: Dict[str, Set[str]] = {}
    for record in reversed(history):
        uid = str(record.user_id or "")
        if not uid:
            continue
        if uid not in names and len(names) >= max_speakers:
            continue
        bucket = names.setdefault(uid, {uid})
        if record.user_name:
            bucket.add(str(record.user_name))

    from gsuid_core.ai_core.database.models import UserFavorability

    try:
        scores = await UserFavorability.get_scores_for(list(names), bot_id)
    except Exception as e:
        logger.debug(t("log.ai.relationship_priority_speakers_degraded", e=e))
        return priority

    for uid, score in scores.items():
        if zone_of(score) is Zone.CLOSE:
            priority |= names[uid] if uid in names else {uid}
    logger.debug(
        t(
            "log.ai.relationship_priority_speakers",
            group=group_id or "private",
            n=len(priority),
        )
    )
    return priority


async def fetch_relationship(user_id: str, bot_id: str) -> RelationshipView:
    """读关系温度（外部存储，非模型推断）。失败降级为「未打分」View，不抛。

    替代历史的 ``fetch_favorability() -> Optional[int]``：那个返回值让每个消费点
    自己划档，是「同一语义 N 套翻译」的根。
    """
    from gsuid_core.ai_core.utils import _is_master_user

    is_master = _is_master_user(str(user_id))
    score: Optional[int] = None
    try:
        from gsuid_core.ai_core.database.models import UserFavorability

        record = await UserFavorability.get_user_favorability(user_id=user_id, bot_id=bot_id)
        if record is not None:
            score = record.favorability
    except Exception as e:
        logger.debug(t("log.ai.relationship_fetch_degraded", e=e))
    return view_from_score(score, is_master)
