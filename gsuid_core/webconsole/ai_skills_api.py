"""
AI Skills APIs

提供 AI Skills 技能管理相关的 RESTful APIs，包括获取技能列表、详情、删除、克隆和更新等。
"""

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.skills.resource import skills
from gsuid_core.ai_core.skills.operations import (
    SkillInstallResult,
    delete_skill,
    install_skill,
    is_plugin_skill,
    get_skill_source,
    update_skill_markdown,
    get_skill_markdown_path,
)

from ._api_tags import AI_SKILLS


class CloneSkillRequest(BaseModel):
    """克隆技能请求模型"""

    git_url: str
    skill_name: Optional[str] = None


class UpdateMarkdownRequest(BaseModel):
    """更新 Markdown 请求模型"""

    content: str


@app.get("/api/ai/skills/list", summary="获取 AI 技能列表", tags=AI_SKILLS)
async def get_ai_skills_list(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取所有已注册的 AI 技能列表

    Returns:
        status: 0成功，1失败
        data: 包含 skills 列表和总 count
    """
    skills_list: List[Dict[str, Any]] = []
    for name, skill in skills.items():
        source, plugin = get_skill_source(name)
        skills_list.append(
            {
                "name": skill.name,
                "description": skill.description,
                "content": skill.content,
                "license": skill.license,
                "compatibility": skill.compatibility,
                "uri": skill.uri,
                "metadata": skill.metadata,
                # 来源标记：plugin 来源的 skill 只读（由插件仓库维护）
                "source": source,
                "plugin": plugin,
                "editable": source == "data",
            }
        )

    # 按名称排序
    skills_list.sort(key=lambda x: x["name"])

    total_count = len(skills_list)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "skills": skills_list,
            "count": total_count,
        },
    }


@app.get("/api/ai/skills/{skill_name}", summary="获取指定技能详情", tags=AI_SKILLS)
async def get_ai_skill_detail(skill_name: str, _: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取指定 AI 技能的详细信息

    Args:
        skill_name: 技能名称

    Returns:
        status: 0成功，1失败
        data: 技能详情
    """
    skill = skills.get(skill_name)

    if skill is None:
        return {
            "status": 1,
            "msg": f"Skill '{skill_name}' not found",
            "data": None,
        }

    source, plugin = get_skill_source(skill_name)

    # 序列化 resources 和 scripts
    resources_list: List[Dict[str, Any]] = []
    for resource in skill.resources:
        resources_list.append(
            {
                "name": resource.name,
                "description": resource.description,
                "uri": resource.uri,
            }
        )

    scripts_list: List[Dict[str, Any]] = []
    for script in skill.scripts:
        scripts_list.append(
            {
                "name": script.name,
                "description": script.description,
                "uri": script.uri,
            }
        )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "name": skill.name,
            "description": skill.description,
            "content": skill.content,
            "license": skill.license,
            "compatibility": skill.compatibility,
            "uri": skill.uri,
            "metadata": skill.metadata,
            "resources": resources_list,
            "scripts": scripts_list,
            # 来源标记：plugin 来源的 skill 只读（由插件仓库维护）
            "source": source,
            "plugin": plugin,
            "editable": source == "data",
        },
    }


@app.delete("/api/ai/skills/{skill_name}", summary="删除 AI 技能", tags=AI_SKILLS)
async def remove_ai_skill(skill_name: str, _: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    删除指定的 AI 技能（删除整个文件夹）

    Args:
        skill_name: 技能名称

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    if is_plugin_skill(skill_name):
        plugin = get_skill_source(skill_name)[1]
        return {
            "status": 1,
            "msg": f"该技能由插件 {plugin} 管理，请在其仓库内修改",
        }
    result = delete_skill(skill_name)
    return result


@app.post("/api/ai/skills/clone", summary="安装 AI 技能（Git 仓库 / 压缩包直链 / SKILL.md 直链）", tags=AI_SKILLS)
async def clone_ai_skill(
    body: CloneSkillRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> SkillInstallResult:
    """
    从 Git 仓库 / zip 直链 / SKILL.md 直链安装 AI 技能

    Args:
        body: 包含 git_url（任意受支持的来源地址）和可选的 skill_name

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
        skill_name: 安装后的技能名称（仅成功时返回）
    """
    # git clone / 下载解包是阻塞 IO，放线程池避免卡事件循环
    result = await asyncio.to_thread(install_skill, body.git_url, body.skill_name)
    return result


@app.get("/api/ai/skills/{skill_name}/markdown", summary="获取 AI 技能 Markdown 内容", tags=AI_SKILLS)
async def get_ai_skill_markdown(
    skill_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定技能的 markdown 文件内容

    Args:
        skill_name: 技能名称

    Returns:
        status: 0成功，1失败
        data: markdown 文件内容
    """
    if skill_name not in skills:
        return {
            "status": 1,
            "msg": f"Skill '{skill_name}' not found",
            "data": None,
        }

    md_path = get_skill_markdown_path(skill_name)
    if md_path is None:
        return {
            "status": 1,
            "msg": f"Markdown file not found for skill '{skill_name}'",
            "data": None,
        }

    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "skill_name": skill_name,
                "content": content,
                "path": str(md_path),
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"Failed to read markdown: {str(e)}",
            "data": None,
        }


@app.put("/api/ai/skills/{skill_name}/markdown", summary="更新 AI 技能 Markdown 内容", tags=AI_SKILLS)
async def update_ai_skill_markdown(
    skill_name: str,
    body: UpdateMarkdownRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新指定技能的 markdown 文件内容

    Args:
        skill_name: 技能名称
        body: 包含新的 markdown 内容

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    if is_plugin_skill(skill_name):
        plugin = get_skill_source(skill_name)[1]
        return {
            "status": 1,
            "msg": f"该技能由插件 {plugin} 管理，请在其仓库内修改",
        }
    result = update_skill_markdown(skill_name, body.content)
    return result
