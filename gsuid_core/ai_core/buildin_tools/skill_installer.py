"""技能安装工具模块

给 AI 一个安装外部 Skill 的**唯一正确入口**：统一装进框架自己的技能目录
（``skills/resource.py::SKILLS_PATH``）并自动重载生效。防止 Agent 照抄第三方
setup 文档里的 npx / curl+unzip 命令，把技能装到 ``~/.workbuddy`` 等错误路径。
"""

import asyncio
from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.check_func import check_pm
from gsuid_core.ai_core.skills.operations import install_skill as _do_install
from gsuid_core.ai_core.buildin_tools.visibility import visible_to_admin


@ai_tools(
    category="common",
    check_func=check_pm,
    capability_domain="技能管理",
    visible_when=visible_to_admin,
    timeout=400.0,
)
async def install_skill(
    ctx: RunContext[ToolContext],
    source_url: str,
    skill_name: Optional[str] = None,
    update: bool = True,
) -> str:
    """
    安装/更新一个 AI 技能（Skill）到本框架并立即热加载

    这是给本框架安装技能的**唯一正确方式**。第三方 setup 文档里的
    `npx skills add ...`、`curl + unzip 到 ~/.workbuddy/skills` 等命令是给别的
    Agent 框架用的，一律**不要照抄执行**——只需从文档里找出技能的来源地址
    （git 仓库地址 / zip、tar.gz 压缩包直链 / SKILL.md 文件直链均可），传给本工具。
    技能会被安装到框架自己的技能目录并自动重载，无需重启；来源里含多个技能时全部安装。

    Args:
        ctx: 工具执行上下文
        source_url: 技能来源地址：git 仓库 / zip、tar.gz 直链 / SKILL.md 直链
        skill_name: 可选，指定技能名（仅当 SKILL.md 缺少 name 字段时作为命名依据）
        update: 同名技能已存在时是否覆盖更新，默认 True（安装/更新一体）

    Returns:
        安装结果描述，含本次安装并已加载的技能名列表
    """
    result = await asyncio.to_thread(_do_install, source_url, skill_name, update)
    if result["status"] != 0:
        return f"❌ 技能安装失败: {result['msg']}"
    return f"✅ {result['msg']}"
