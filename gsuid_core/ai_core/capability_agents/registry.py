"""能力代理注册表（拟人化 Agent 长任务执行能力 · 修复二）。

一个「画像（CapabilityAgentProfile）」= 一种**无人格**的专职执行角色。

设计动机：当前架构把「执行」与「人格表达」耦合在同一个会话——长任务的定时
唤醒唤醒的是人格本人（如懒惰、回避分析的早柚），导致执行侧人格抵制严肃执行、
表达侧人格被迫表达超边界内容（人格漂移）。本模块把执行能力从人格剥离，交给
专职能力代理：执行者无人格、专注白名单工具集、Plan-and-Solve，不拒绝也不漂移。

注册表为进程内存数据，启动时由 ``profiles.register_builtin_profiles()`` 重建，
插件可在自身启动钩子里 ``register_capability_agent(...)`` 注册业务画像。
"""

from typing import Dict, List, Optional
from dataclasses import field, dataclass

from gsuid_core.logger import logger


@dataclass
class CapabilityAgentProfile:
    """一个能力代理画像：描述一种专职执行角色的职能、提示词与工具集。"""

    profile_id: str  # "research_agent" / "code_agent" / "finance_agent"
    display_name: str  # 给人格 / 用户看的名字，如 "操盘助手"
    when_to_use: str  # 何时该派给它（一句话）
    system_prompt: str  # 纯职能 Plan-and-Solve 提示词，绝无人格
    match_keywords: List[str]  # 自然语言 hint 命中关键词（resolve 用）
    tool_names: List[str] = field(default_factory=list)  # 显式工具白名单（按名挂载）
    tool_query: str = ""  # 可选：再做一次向量检索补充工具的查询词
    max_iterations: int = 20
    max_tokens: int = 35000


# profile_id -> Profile
_PROFILES: Dict[str, CapabilityAgentProfile] = {}


def register_capability_agent(profile: CapabilityAgentProfile) -> None:
    """注册一个能力代理画像。

    框架内置 ``research_agent`` / ``code_agent``；插件可注册自己的业务画像
    （如炒股插件注册 ``finance_agent``，把 stock 工具名填进 ``tool_names``）。
    """
    if not profile.profile_id:
        logger.warning("🤖 [CapabilityAgent] 画像 profile_id 为空，已忽略")
        return
    _PROFILES[profile.profile_id] = profile
    logger.info(f"🤖 [CapabilityAgent] 注册画像: {profile.profile_id} ({profile.display_name})")


def get_profile(profile_id: str) -> Optional[CapabilityAgentProfile]:
    """按 profile_id 取回画像，不存在返回 None。"""
    if profile_id in _PROFILES:
        return _PROFILES[profile_id]
    return None


def unregister_capability_agent(profile_id: str) -> bool:
    """从内存注册表里移除一个画像；返回是否真的删了一项。

    仅 ``persistence.delete_user_profile`` 调用：避免外部模块直接读写
    内部 ``_PROFILES`` 字典（违反封装并触发 LLM.md §1.3 的 ``type: ignore``）。
    """
    if profile_id in _PROFILES:
        _PROFILES.pop(profile_id)
        return True
    return False


def list_profiles() -> List[CapabilityAgentProfile]:
    """列出所有已注册的画像。"""
    return list(_PROFILES.values())


def resolve_profile(hint: str, default: str = "research_agent") -> str:
    """自然语言 hint → profile_id（无 UUID 约束同款：用句柄不用 ID）。

    解析顺序：
    1. hint 直接就是已注册的 profile_id → 直接返回；
    2. hint 命中某画像的 match_keywords → 返回该 profile_id；
    3. 都不命中 → 回退 default（default 不存在时回退首个已注册画像）。
    """
    h = (hint or "").strip().lower()
    if not h:
        return default if default in _PROFILES else next(iter(_PROFILES), "")
    if h in _PROFILES:  # 也允许直接传 profile_id
        return h
    for p in _PROFILES.values():
        if any(kw.lower() in h for kw in p.match_keywords):
            return p.profile_id
    return default if default in _PROFILES else next(iter(_PROFILES), "")
