"""
AI Skills APIs

提供 AI Skills 技能管理相关的 RESTful APIs，包括获取技能列表、详情、删除、克隆和更新等。
"""

from typing import Any, Dict, List, Optional

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.skills.resource import skills
from gsuid_core.ai_core.skills.operations import (
    delete_skill,
    clone_skill_from_git,
    update_skill_markdown,
    get_skill_markdown_path,
)


class CloneSkillRequest(BaseModel):
    """克隆技能请求模型"""

    git_url: str
    skill_name: Optional[str] = None


class UpdateMarkdownRequest(BaseModel):
    """更新 Markdown 请求模型"""

    content: str


@app.get("/api/ai/skills/list")
async def get_ai_skills_list(_: Dict = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取所有已注册的 AI 技能列表

    Returns:
        status: 0成功，1失败
        data: 包含 skills 列表和总 count
    """
    skills_list: List[Dict[str, Any]] = []
    for name, skill in skills.items():
        skills_list.append(
            {
                "name": skill.name,
                "description": skill.description,
                "content": skill.content,
                "license": skill.license,
                "compatibility": skill.compatibility,
                "uri": skill.uri,
                "metadata": skill.metadata,
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


@app.get("/api/ai/skills/{skill_name}")
async def get_ai_skill_detail(skill_name: str, _: Dict = Depends(require_auth)) -> Dict[str, Any]:
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
        },
    }


@app.delete("/api/ai/skills/{skill_name}")
async def remove_ai_skill(skill_name: str, _: Dict = Depends(require_auth)) -> Dict[str, Any]:
    """
    删除指定的 AI 技能（删除整个文件夹）

    Args:
        skill_name: 技能名称

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    result = delete_skill(skill_name)
    return result


@app.post("/api/ai/skills/clone")
async def clone_ai_skill(
    body: CloneSkillRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    从 Git URL 克隆 AI 技能

    Args:
        body: 包含 git_url 和可选的 skill_name

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
        skill_name: 克隆后的技能名称（仅成功时返回）
    """
    result = clone_skill_from_git(body.git_url, body.skill_name)
    return result


@app.get("/api/ai/skills/{skill_name}/markdown")
async def get_ai_skill_markdown(
    skill_name: str,
    _: Dict = Depends(require_auth),
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


@app.put("/api/ai/skills/{skill_name}/markdown")
async def update_ai_skill_markdown(
    skill_name: str,
    body: UpdateMarkdownRequest,
    _: Dict = Depends(require_auth),
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
    result = update_skill_markdown(skill_name, body.content)
    return result
