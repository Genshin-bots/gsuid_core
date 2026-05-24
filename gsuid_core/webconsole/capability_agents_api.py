"""
Capability Agents APIs · 修复七

提供给前端管理"能力代理画像（CapabilityAgentProfile）"的 RESTful 接口。

画像分三种来源：

- ``builtin`` ：框架内置（``research_agent`` / ``code_agent``），仅可读，**禁止改 / 删**。
- ``plugin``  ：其他插件在自身启动钩子里 ``register_capability_agent`` 注册的画像，
  仅可读，**禁止改 / 删**（前端可看作用以诊断"插件挂没挂上"）。
- ``user``    ：管理员在 webconsole 上手工新建 / 编辑的画像，**允许 PATCH / DELETE**，
  落盘在 ``data/ai_core/capability_agents/<profile_id>.json``，启动时由
  ``planning.startup`` 调 ``load_user_profiles`` 挂回。

新建画像时可以指定 ``base``——以某个 builtin / plugin / user 画像为模板复制，再
覆盖字段——是一种"复制内置画像再改一改"的工作流。
"""

import re
from typing import Any, Dict, List, Optional

from fastapi import Query, Depends
from pydantic import Field, BaseModel

from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,
    get_profile,
    save_user_profile,
    get_profile_as_dto,
    get_profile_source,
    delete_user_profile,
    register_capability_agent,
    export_all_profiles_as_dto,
)
from gsuid_core.ai_core.capability_agents.persistence import CapabilityAgentDTO

# profile_id 命名规范：英数下划线，1~64 字符。和 ``register_capability_agent``
# 内部使用的句柄保持一致，避免 URL 编码不一致。
_PROFILE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")


# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────


class CreateProfileRequest(BaseModel):
    profile_id: str = Field(..., description="英数下划线，1~64 字符，字母开头")
    display_name: str = Field(..., description="给人格 / 用户看的名字，如「操盘助手」")
    when_to_use: str = Field("", description="何时该派给它（一句话）")
    system_prompt: str = Field(..., description="纯职能 Plan-and-Solve 提示词，绝无人格")
    match_keywords: List[str] = Field(default_factory=list)
    tool_names: List[str] = Field(default_factory=list, description="显式工具白名单（按名）")
    tool_query: str = Field("", description="可选：再做一次向量检索补充工具的查询词")
    max_iterations: int = Field(20, ge=1, le=200)
    max_tokens: int = Field(35000, ge=1000, le=200000)
    base: Optional[str] = Field(None, description="（可选）以哪个已存在画像为模板复制字段，再用本请求覆盖")


class PatchProfileRequest(BaseModel):
    """对一个用户画像做局部更新。所有字段都可选，未传的保持不变。"""

    display_name: Optional[str] = None
    when_to_use: Optional[str] = None
    system_prompt: Optional[str] = None
    match_keywords: Optional[List[str]] = None
    tool_names: Optional[List[str]] = None
    tool_query: Optional[str] = None
    max_iterations: Optional[int] = Field(None, ge=1, le=200)
    max_tokens: Optional[int] = Field(None, ge=1000, le=200000)


# ─────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────


@app.get("/api/ai/capability-agents/list")
async def list_capability_agents(
    _: Dict = Depends(require_auth),
    source: Optional[str] = Query(None, description="按来源筛选：builtin / plugin / user"),
) -> Dict[str, Any]:
    """列出所有能力代理画像（包含来源标记）。"""
    items: List[CapabilityAgentDTO] = export_all_profiles_as_dto()
    if source:
        # _profile_to_dto 必填 "source"（见 persistence.py），直接键访问
        items = [x for x in items if "source" in x and x["source"] == source]
    return {"status": 0, "msg": "ok", "data": {"items": items, "count": len(items)}}


@app.get("/api/ai/capability-agents/{profile_id}")
async def get_capability_agent_detail(
    profile_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """获取单个画像详情。"""
    dto = get_profile_as_dto(profile_id)
    if dto is None:
        return {"status": 1, "msg": f"画像 {profile_id} 不存在", "data": None}
    return {"status": 0, "msg": "ok", "data": dto}


@app.post("/api/ai/capability-agents")
async def create_capability_agent(
    body: CreateProfileRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """新建一个用户自定义能力代理画像（写入磁盘 + 内存注册表）。

    ``base`` 字段允许"以某画像为模板复制字段再覆盖"：当请求中某字段为 None / 空时，
    自动 fallback 到 ``base`` 画像的同名字段；显式传值就以请求为准。
    """
    if not _PROFILE_ID_RE.match(body.profile_id):
        return {
            "status": 1,
            "msg": "profile_id 必须以字母开头，仅含英数下划线，长度 1~64",
            "data": None,
        }
    if get_profile(body.profile_id) is not None:
        return {"status": 1, "msg": f"画像 {body.profile_id} 已存在，请改用 PATCH 编辑", "data": None}

    # base 模板复制：未传或为空的字段从 base 取。base 为 None 时各字段回退到
    # CreateProfileRequest 的默认值或 dataclass 默认值。
    base_profile = get_profile(body.base) if body.base else None

    # 逐字段显式 fallback：避免 getattr 在已知 dataclass 字段上使用（LLM.md §1.4）。
    # CapabilityAgentProfile 的字段集合是显式声明的，逐字段写出反而比 getattr
    # 元编程更可读、IDE 跳转友好。
    display_name = body.display_name or (base_profile.display_name if base_profile is not None else body.profile_id)
    when_to_use = body.when_to_use or (base_profile.when_to_use if base_profile is not None else "")
    system_prompt = body.system_prompt or (base_profile.system_prompt if base_profile is not None else "")
    match_keywords: List[str] = list(
        body.match_keywords
        if body.match_keywords
        else (base_profile.match_keywords if base_profile is not None else [])
    )
    tool_names: List[str] = list(
        body.tool_names if body.tool_names else (base_profile.tool_names if base_profile is not None else [])
    )
    tool_query = body.tool_query or (base_profile.tool_query if base_profile is not None else "")
    max_iterations = (
        body.max_iterations
        if body.max_iterations
        else (base_profile.max_iterations if base_profile is not None else 20)
    )
    max_tokens = (
        body.max_tokens if body.max_tokens else (base_profile.max_tokens if base_profile is not None else 35000)
    )

    profile = CapabilityAgentProfile(
        profile_id=body.profile_id,
        display_name=display_name,
        when_to_use=when_to_use,
        system_prompt=system_prompt,
        match_keywords=match_keywords,
        tool_names=tool_names,
        tool_query=tool_query,
        max_iterations=int(max_iterations),
        max_tokens=int(max_tokens),
    )

    register_capability_agent(profile)
    save_user_profile(profile)
    logger.info(
        f"🤖 [CapabilityAgent] webconsole 新建用户画像: {profile.profile_id}"
        f"（{'基于 ' + body.base if body.base else '无模板'}）"
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": get_profile_as_dto(profile.profile_id),
    }


@app.patch("/api/ai/capability-agents/{profile_id}")
async def patch_capability_agent(
    profile_id: str,
    body: PatchProfileRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """编辑一个**用户自建**画像。builtin / plugin 画像拒绝修改。"""
    source = get_profile_source(profile_id)
    if source == "missing":
        return {"status": 1, "msg": f"画像 {profile_id} 不存在", "data": None}
    if source != "user":
        return {
            "status": 1,
            "msg": f"画像 {profile_id} 是 {source} 画像，框架不允许通过网页修改；"
            "请用 POST /api/ai/capability-agents 复制一份用户版本再改",
            "data": None,
        }

    existing = get_profile(profile_id)
    if existing is None:  # 双保险：source 标记和注册表应同步
        return {"status": 1, "msg": f"画像 {profile_id} 已失踪", "data": None}

    # 用 body 中明确传入的字段覆盖 existing
    patched = CapabilityAgentProfile(
        profile_id=existing.profile_id,
        display_name=body.display_name if body.display_name is not None else existing.display_name,
        when_to_use=body.when_to_use if body.when_to_use is not None else existing.when_to_use,
        system_prompt=body.system_prompt if body.system_prompt is not None else existing.system_prompt,
        match_keywords=list(body.match_keywords) if body.match_keywords is not None else list(existing.match_keywords),
        tool_names=list(body.tool_names) if body.tool_names is not None else list(existing.tool_names),
        tool_query=body.tool_query if body.tool_query is not None else existing.tool_query,
        max_iterations=body.max_iterations if body.max_iterations is not None else existing.max_iterations,
        max_tokens=body.max_tokens if body.max_tokens is not None else existing.max_tokens,
    )
    register_capability_agent(patched)
    save_user_profile(patched)
    return {"status": 0, "msg": "ok", "data": get_profile_as_dto(profile_id)}


@app.delete("/api/ai/capability-agents/{profile_id}")
async def delete_capability_agent(
    profile_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """删除一个**用户自建**画像（同时清磁盘文件 + 内存注册表）。builtin / plugin 画像拒绝删除。"""
    source = get_profile_source(profile_id)
    if source == "missing":
        return {"status": 1, "msg": f"画像 {profile_id} 不存在", "data": None}
    if source != "user":
        return {
            "status": 1,
            "msg": f"画像 {profile_id} 是 {source} 画像，框架不允许通过网页删除",
            "data": None,
        }
    ok = delete_user_profile(profile_id)
    if not ok:
        return {"status": 1, "msg": "删除失败（非用户画像）", "data": None}
    return {"status": 0, "msg": "ok", "data": {"profile_id": profile_id}}


@app.get("/api/ai/capability-agents/_tools/available")
async def list_available_tools_for_profiles(
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """枚举所有可挂载到画像的工具名，前端做 tool_names 多选框时用。

    复用 ``ai_core/register`` 的全量工具表，按 (category, plugin, name) 组织。
    """
    from gsuid_core.ai_core.register import get_registered_tools

    by_cat = get_registered_tools()
    items: List[Dict[str, Any]] = []
    for cat, tools in by_cat.items():
        for name, base in tools.items():
            items.append(
                {
                    "name": name,
                    "description": base.description,
                    "category": cat,
                    "plugin": base.plugin,
                }
            )
    items.sort(key=lambda x: (x["plugin"], x["category"], x["name"]))
    return {"status": 0, "msg": "ok", "data": {"items": items, "count": len(items)}}
