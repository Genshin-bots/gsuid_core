"""用户自定义能力代理画像的本地持久化（修复七 · webconsole 后端依赖）。

框架内置画像（``research_agent`` / ``code_agent``）由 ``profiles.py`` 在进程启动
时调 ``register_builtin_profiles`` 重建，不需要持久化；插件画像由插件自己的
启动钩子负责。**本模块只管"用户在 webconsole 上手工新建 / 编辑的画像"**：

- 落盘路径：``data/ai_core/capability_agents/<profile_id>.json``
- 落盘字段：``CapabilityAgentProfile`` 的全部字段 + ``source="user"`` 标记。
- 启动顺序：``init_planning()`` 调完 ``register_builtin_profiles()`` 之后立即调
  ``load_user_profiles()``，把磁盘上的用户画像挂回内存注册表。

只有 ``source="user"`` 的画像才允许通过 webconsole 修改 / 删除；内置 / 插件画像
对前端只读，避免改坏框架默认行为。
"""

import json
from typing import List, Literal, Optional, TypedDict
from pathlib import Path

from gsuid_core.logger import logger

from .registry import (
    CapabilityAgentProfile,
    get_profile,
    register_capability_agent,
    unregister_capability_agent,
)

# 与 `ai_core/resource.py` 解耦——直接落到 data/ai_core/capability_agents/
_PERSIST_DIR: Path = Path(__file__).resolve().parents[3] / "data" / "ai_core" / "capability_agents"

# 画像来源的字面量类型，与 CapabilityAgentDTO.source 完全一致。
ProfileSource = Literal["builtin", "plugin", "user"]
ProfileSourceWithMissing = Literal["builtin", "plugin", "user", "missing"]


# 进程内"哪些 profile 是用户在 webconsole 上建/编辑的"的标记表。
# 仅 webconsole 后端会消费它，决定一个画像是否允许被 PATCH / DELETE。
# 启动时 load_user_profiles 把所有磁盘画像登记进来；POST 新建时也登记。
_USER_PROFILE_IDS: set[str] = set()


class CapabilityAgentDTO(TypedDict, total=False):
    """webconsole / 持久化层共享的画像传输 / 落盘字典。"""

    profile_id: str
    display_name: str
    when_to_use: str
    system_prompt: str
    match_keywords: List[str]
    tool_names: List[str]
    tool_query: str
    max_iterations: int
    max_tokens: int
    source: ProfileSource


def _profile_to_dto(profile: CapabilityAgentProfile, source: ProfileSource) -> CapabilityAgentDTO:
    """把内存里的 dataclass 序列化为 DTO（落盘 + 前端 JSON 都用这个形状）。"""
    return CapabilityAgentDTO(
        profile_id=profile.profile_id,
        display_name=profile.display_name,
        when_to_use=profile.when_to_use,
        system_prompt=profile.system_prompt,
        match_keywords=list(profile.match_keywords),
        tool_names=list(profile.tool_names),
        tool_query=profile.tool_query,
        max_iterations=profile.max_iterations,
        max_tokens=profile.max_tokens,
        source=source,
    )


def _dto_to_profile(dto: CapabilityAgentDTO) -> CapabilityAgentProfile:
    """把 DTO 反序列化为内存 dataclass。

    DTO 是 ``total=False`` 的 TypedDict —— 磁盘旧数据可能缺字段。这里用
    ``"key" in dto`` 显式存在性检查（遵循 LLM.md §1.4），缺失则用 dataclass
    构造器的默认值（display_name 等必填项已在 DTO 校验阶段保证）。
    """
    match_keywords: List[str] = list(dto["match_keywords"]) if "match_keywords" in dto else []
    tool_names: List[str] = list(dto["tool_names"]) if "tool_names" in dto else []
    tool_query: str = dto["tool_query"] if "tool_query" in dto and dto["tool_query"] else ""
    max_iterations: int = int(dto["max_iterations"]) if "max_iterations" in dto and dto["max_iterations"] else 20
    max_tokens: int = int(dto["max_tokens"]) if "max_tokens" in dto and dto["max_tokens"] else 35000
    return CapabilityAgentProfile(
        profile_id=dto["profile_id"],
        display_name=dto["display_name"],
        when_to_use=dto["when_to_use"],
        system_prompt=dto["system_prompt"],
        match_keywords=match_keywords,
        tool_names=tool_names,
        tool_query=tool_query,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
    )


def _profile_path(profile_id: str) -> Path:
    return _PERSIST_DIR / f"{profile_id}.json"


def mark_as_user_profile(profile_id: str) -> None:
    """把某 profile_id 标记为"用户自建"。webconsole 后端用于判定是否可改 / 删。"""
    _USER_PROFILE_IDS.add(profile_id)


def is_user_profile(profile_id: str) -> bool:
    """判定一个画像是否由用户在 webconsole 上新建。"""
    return profile_id in _USER_PROFILE_IDS


def get_profile_source(profile_id: str) -> ProfileSourceWithMissing:
    """判定画像的来源（用于前端展示与权限）。

    - "user"    : 用户在 webconsole 上自建（_USER_PROFILE_IDS 命中）。
    - "builtin" : 框架内置（research_agent / code_agent / 等 5 个通用画像 +
                  内部 evaluator）。
    - "plugin"  : 其他插件注册的画像。
    - "missing" : 画像不存在。
    """
    if get_profile(profile_id) is None:
        return "missing"
    if profile_id in _USER_PROFILE_IDS:
        return "user"
    # 内置画像 id 由 profiles.register_builtin_profiles 决定（v3 收敛后为 5 个）
    if profile_id in (
        "research_agent",
        "code_agent",
        "internal_reporter",
        "memory_curator",
        "scheduler_assistant",
        # 能力评估代理也算内置（仅框架内部调用，不暴露给主人格直接派活）
        "capability_evaluator",
    ):
        return "builtin"
    return "plugin"


def save_user_profile(profile: CapabilityAgentProfile) -> Path:
    """把用户自建 / 编辑后的画像落盘并登记 source。

    内部用，外部一般通过 webconsole API。本函数不调 ``register_capability_agent``——
    调用方需要自己决定 register 时机（一般是 webconsole 后端先 register 再 save）。
    """
    _PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    path = _profile_path(profile.profile_id)
    payload = _profile_to_dto(profile, source="user")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # 原子替换，避免半文件
    import os

    os.replace(tmp_path, path)
    mark_as_user_profile(profile.profile_id)
    logger.info(f"🤖 [CapabilityAgent] 已落盘用户画像: {profile.profile_id} → {path.name}")
    return path


def delete_user_profile(profile_id: str) -> bool:
    """从磁盘和内存里删除某用户自建画像；返回是否删除成功。

    仅删除 ``source="user"`` 的画像；内置 / 插件画像拒绝删除。
    """
    if not is_user_profile(profile_id):
        return False
    path = _profile_path(profile_id)
    if path.exists():
        path.unlink()
    # 走 registry 暴露的公开接口清掉内存注册表项（不再访问私有 _PROFILES）
    unregister_capability_agent(profile_id)
    _USER_PROFILE_IDS.discard(profile_id)
    logger.info(f"🤖 [CapabilityAgent] 已删除用户画像: {profile_id}")
    return True


def load_user_profiles() -> int:
    """启动时把磁盘上的用户画像挂回内存注册表。返回挂回的画像数量。"""
    if not _PERSIST_DIR.exists():
        return 0
    count = 0
    for path in _PERSIST_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                dto = json.load(f)
            if not isinstance(dto, dict) or "profile_id" not in dto:
                logger.warning(f"🤖 [CapabilityAgent] 跳过不合法画像文件: {path.name}")
                continue
            profile = _dto_to_profile(dto)
            register_capability_agent(profile)
            mark_as_user_profile(profile.profile_id)
            count += 1
        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning(f"🤖 [CapabilityAgent] 加载用户画像失败: {path.name}: {e}")
    if count:
        logger.info(f"🤖 [CapabilityAgent] 启动加载用户画像: {count} 个")
    return count


def export_all_profiles_as_dto() -> List[CapabilityAgentDTO]:
    """按 source 标注，导出当前注册表所有画像的 DTO（webconsole list 端点用）。"""
    from .registry import list_profiles

    out: List[CapabilityAgentDTO] = []
    for p in list_profiles():
        out.append(_profile_to_dto(p, source=get_profile_source(p.profile_id)))
    return out


def get_profile_as_dto(profile_id: str) -> Optional[CapabilityAgentDTO]:
    """按 source 标注导出单个画像的 DTO（webconsole detail 端点用）。"""
    p = get_profile(profile_id)
    if p is None:
        return None
    return _profile_to_dto(p, source=get_profile_source(profile_id))
