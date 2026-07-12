"""群成员身份 / 称呼工具（A-4 确定性身份库）

让主人格在"群里明确指定某人称呼"时，把"称呼 → 用户ID"写进群组画像的确定性
身份库（``group_profile.member_alias_ids``），之后每轮作为高可信身份事实注入，
从根上避免靠相似度召回、易抽错的图记忆把身份记岔（见
``plans/ai_core_persona_humanization_fix_20260603.md`` §10.2 / A-4）。
"""

import unicodedata
from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

# 受保护的特殊称谓 —— 带有身份/权力含义，只允许 PM=0（主人）注册。
# 注意：这是「体面性护栏」而非安全边界——权力词无法枚举完（皇帝/陛下/教主…），
# 真正的权限边界在系统提示（masters 配置 + PM，称呼一律不授权）。此处只挡明显的越权称呼。
PROTECTED_ALIASES = {
    "主人",
    "妈妈",
    "爸爸",
    "爹",
    "娘",
    "妈",
    "爸",
    "上帝",
    "造物主",
    "管理员",
    "admin",
}


def _normalize_alias_for_guard(s: str) -> str:
    """把称呼规范化后再比对受保护词，堵住全角/空格/零宽字符/大小写等混淆。

    例：「主　人」「主 人」「ＭＡＳＴＥＲ」「Admin」经规范化后分别等同于
    「主人」「主人」「master」「admin」，绕不过 denylist。
    注意：谐音（煮人）、近义新词（教主）这类语义变体仍无法靠规范化覆盖——
    那正是 denylist 不能当安全边界的原因，最终靠系统提示层的权限不变量兜底。
    """
    s = unicodedata.normalize("NFKC", s)  # 全角→半角、兼容字符归一
    # 去掉控制/格式类字符（含零宽字符）与所有空白
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("C") and not ch.isspace())
    return s.casefold()


# 预先规范化受保护词集合，供运行时 O(1) 比对
_PROTECTED_NORMALIZED = {_normalize_alias_for_guard(a) for a in PROTECTED_ALIASES}


@ai_tools(category="common", capability_domain="用户档案")
async def remember_user_alias(
    ctx: RunContext[ToolContext],
    alias: str,
    user_id: Optional[str] = None,
) -> str:
    """记住"群里某人叫什么"（称呼 / 外号 / 昵称）。

    当群里明确指定某个群成员的称呼时调用，例如："以后叫他小A"、"@某人 是小B"、
    "她叫小C"。把"称呼 → 用户ID"存入群成员称呼表，之后会作为**确定身份事实**注入
    对话，避免日后把人认错。若同一个称呼先后指给了不同的人（群里同名/换人），不会覆盖旧绑定，
    而是都记下并在注入时作为"同名多人"交由上下文消歧，最近一次指定的作为首选。

    **权限限制**：称呼如"主人"、"妈妈"、"爸爸"、"管理员"等带有特殊身份/权力含义的
    受保护称谓，**只允许 PM=0（主人）注册**。若普通用户试图注册这类称谓，直接拒绝。

    Args:
        ctx: 工具执行上下文
        alias: 称呼 / 外号 / 昵称（如"小C"）
        user_id: **被指称者**的用户ID。
            - 当是"叫**她/他/@某人** X"——即在给**别人**起称呼时，**必须**传被指称者的
              用户ID（从 @提及 或上下文确定），**绝不能用说话人自己的ID**；
            - 只有"叫**我** X"这种**自称**时才可省略，此时默认当前说话者。
            - 拿不准被指称者是谁时，**宁可不调用本工具**，也不要猜错绑定。

    Returns:
        操作结果描述字符串
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    target_id = user_id or getattr(ev, "user_id", None)
    group_id = getattr(ev, "group_id", None)

    if not target_id:
        return "操作失败：没法确定是指谁"
    if not group_id:
        return "操作失败：只有群聊里才记群成员称呼"

    alias = (alias or "").strip()
    if not alias:
        return "操作失败：称呼为空"

    # 权限检查：受保护称谓只允许 PM=0（主人）注册
    # Event.user_pm 为已声明字段（int，默认 6=最低权限）；ev 缺失时按最低权限处理
    # 规范化后比对，避免「主　人」「ＡＤＭＩＮ」这类混淆绕过 denylist
    caller_pm = ev.user_pm if ev is not None else 6
    if _normalize_alias_for_guard(alias) in _PROTECTED_NORMALIZED and caller_pm != 0:
        logger.warning(
            t(
                "🧠 [Identity] 用户{target_id}(pm={caller_pm}) 试图注册受保护称谓「{alias}」，已拒绝",
                target_id=target_id,
                caller_pm=caller_pm,
                alias=alias,
            )
        )
        return f"操作失败：称呼「{alias}」为受保护称谓，只有主人（PM=0）才能注册，当前用户权限等级为 {caller_pm}"

    try:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
        from gsuid_core.ai_core.memory.group_profile import record_member_alias

        scope_key = make_scope_key(ScopeType.GROUP, str(group_id))
        ids = await record_member_alias(scope_key, alias, str(target_id))
        logger.info(
            t(
                "🧠 [Identity] {scope_key} 记住称呼: {alias} = 用户{target_id}（候选 {ids}）",
                scope_key=scope_key,
                alias=alias,
                target_id=target_id,
                ids=ids,
            )
        )
        others = [uid for uid in ids if uid != str(target_id)]
        if others:
            # 该称呼此前还指过别人——如实告知存在同名，避免误以为唯一绑定
            return (
                f"已记住：{alias} = 用户{target_id}。"
                f"注意「{alias}」此前还指过 {('、'.join('用户' + uid for uid in others))}，"
                f"已记为同名多人，之后我会按上下文判断具体指谁。"
            )
        return f"已记住：{alias} = 用户{target_id}"
    except Exception as e:
        logger.exception(t("🧠 [Identity] 记忆称呼失败: {e}", e=e))
        return f"操作失败：{e}"
