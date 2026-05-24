"""Persona 模块 —— 主人格人设与提示词管理

主人格（Persona）是用户对话的入口；它和能力代理的关系是"对外说话的人 vs
干活的人"——能力代理把任务做完，主人格用自己的口吻转告主人。

## 模块组成

- ``persona.py``    : ``Persona`` 数据类（id / 名字 / 简介 / 立绘 / 音频锚等）
- ``models.py``     : ``PersonaFiles`` / ``PersonaMetadata`` 元信息
- ``resource.py``   : 文件系统级的人格资源 CRUD（落盘到 ``PERSONA_PATH``）
- ``config.py``     : ``PersonaConfigManager`` —— 全局人格选择 / 启用状态
- ``processor.py``  : ``build_persona_prompt`` —— 把人设拼装成最终 system prompt
- ``prompts.py``    : ``SYSTEM_CONSTRAINTS`` 决策树 + ``CHARACTER_BUILDING_TEMPLATE``
                      角色构建模板 + ``ROLE_PLAYING_START`` 角色扮演引语
- ``startup.py``    : ``init_default_personas`` —— 框架启动时把内置人格补齐

## 关键约束

1. 主人格 prompt 由 ``processor.build_persona_prompt`` 一次性拼装：人设
   + ``SYSTEM_CONSTRAINTS`` 决策树 + 自我认知 + 上下文摘要。
2. 决策树关键分支（``prompts.py``）：
   - §3.1  专业域强制委派：遇到证券 / 天气 / 代码 / 内部周报等专业问题必须
           走 ``create_subagent`` 或 ``register_kanban_task``，不允许自己用
           工具池 + ``web_search`` 拼答案。
   - §3.4  ``scheduled_task`` ↔ ``Kanban`` 边界：单步周期用 ``add_interval_task``；
           多步周期 / 含决策记账用 ``register_kanban_task(recurring_trigger=...)``。
   - §3.5  复合多代理任务必经路径：``evaluate_agent_mesh_capability`` →
           ``register_kanban_task``。
   - §3.6  追问溯源：必须 ``artifact_get_recent`` 取原文，不允许重新检索拼凑。
3. 主人格**唯一持有**与用户直接通信的工具（``send_message_by_ai`` / ``send_meme``
   等 ``category="self"`` 工具）；能力代理只对主人格交付，由 Kanban 转译后下发。
"""

from gsuid_core.ai_core.persona.config import (
    DEFAULT_PERSONA_CONFIG,
    PersonaConfigManager,
    persona_config_manager,
)
from gsuid_core.ai_core.persona.models import PersonaFiles, PersonaMetadata
from gsuid_core.ai_core.persona.persona import PERSONA_PATH, Persona
from gsuid_core.ai_core.persona.prompts import (
    ROLE_PLAYING_START,
    SYSTEM_CONSTRAINTS,
    CHARACTER_BUILDING_TEMPLATE,
)
from gsuid_core.ai_core.persona.startup import init_default_personas
from gsuid_core.ai_core.persona.resource import (
    load_persona,
    save_persona,
    delete_persona,
    get_voice_anchor,
    get_persona_metadata,
    get_persona_audio_path,
    get_persona_image_path,
    get_persona_avatar_path,
    list_available_personas,
    invalidate_voice_anchor_cache,
    migrate_voice_anchor_from_config,
)
from gsuid_core.ai_core.persona.processor import build_persona_prompt

__all__ = [
    # Persona 核心类
    "Persona",
    "PersonaFiles",
    "PersonaMetadata",
    "PERSONA_PATH",
    # 处理器
    "build_persona_prompt",
    # 资源管理（向后兼容的函数接口）
    "save_persona",
    "load_persona",
    "list_available_personas",
    "get_persona_avatar_path",
    "get_persona_image_path",
    "get_persona_audio_path",
    "get_persona_metadata",
    "get_voice_anchor",
    "invalidate_voice_anchor_cache",
    "migrate_voice_anchor_from_config",
    "delete_persona",
    # 提示词模板
    "CHARACTER_BUILDING_TEMPLATE",
    "ROLE_PLAYING_START",
    "SYSTEM_CONSTRAINTS",
    # 初始化函数
    "init_default_personas",
    # 配置管理
    "PersonaConfigManager",
    "persona_config_manager",
    "DEFAULT_PERSONA_CONFIG",
]
