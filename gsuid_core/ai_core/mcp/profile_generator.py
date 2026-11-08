"""MCP 服务中文介绍异步补全。

启用 ``register_as_ai_tools`` 且缺中文描述的 MCP 服务注册后，后台用高级模型生成
4~40 字中文能力说明 + 路由关键词，写进配置的 ``generated_profile`` 并即时生效
（重建能力节点 + 刷新工具 covers + 预热向量索引）。

失败只影响"描述更准"这一项：工具直调 / 节点委派 / 拼接兜底文案全部照旧。
"""

import re
import json
import time
import asyncio
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ValidationError

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.node_profile import (
    has_cjk,
    first_line,
    tools_hash,
    mcp_node_id,
    build_mcp_node,
    load_mcp_overlay,
)
from gsuid_core.ai_core.mcp.config_manager import (
    MCPConfig,
    GeneratedProfile,
    mcp_config_manager,
)

ProfileStatus = Literal["none", "pending", "ready", "failed"]

_MCP_PROFILE_SYSTEM_PROMPT = """\
你是一个中文技术编辑。给定一个 MCP 工具服务的名称与工具清单，为该服务写一段中文能力介绍：
- description：4~40 个字符，必须含中文；说明"这个服务能做什么"，不要罗列工具名，不要带标点收尾。
- keywords：最多 8 个关键词，每个 2~8 字符，供任务路由检索；不要输出纯英文词，不要输出"MCP/工具/服务器"这类通用词。
只输出一个 JSON 对象：{"description": "...", "keywords": ["...", "..."]}，不要输出任何其他文字。"""

_PENDING: dict[str, asyncio.Task[None]] = {}
_FAILED: set[str] = set()
_SEM: asyncio.Semaphore | None = None


class _ProfilePayload(BaseModel):
    description: str
    keywords: List[str] = []


def _semaphore() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(2)
    return _SEM


def get_profile_status(config_id: str) -> ProfileStatus:
    """内存态画像状态：pending > failed > ready > none。"""
    if config_id in _PENDING:
        return "pending"
    if config_id in _FAILED:
        return "failed"
    config = mcp_config_manager.get_config(config_id)
    if config is not None and config.generated_profile is not None:
        return "ready"
    return "none"


def _llm_enabled() -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config("enable").data) and bool(ai_config.get_config("mcp_profile_llm_enable").data)


def _registered_names(config: MCPConfig) -> List[str]:
    """该服务当前在注册表里的实际工具名（plugin 精确匹配）。"""
    from gsuid_core.ai_core.register import get_registered_tools

    plugin_name = f"mcp_{config.name}"
    mcp_tools = get_registered_tools().get("mcp", {})
    return [name for name, tool_base in mcp_tools.items() if tool_base.plugin == plugin_name]


def _needs_generation(config_id: str, config: MCPConfig) -> bool:
    """自动生成门槛：有注册工具 + 无中文描述 + 无用户覆盖 + 画像缺失或过期。"""
    if not _registered_names(config):
        return False
    if has_cjk(config.description):
        return False
    if "when_to_use" in load_mcp_overlay(mcp_node_id(config_id)):
        return False
    profile = config.generated_profile
    return profile is None or profile["source_hash"] != tools_hash(config)


def schedule_profile_generation(config_id: str, *, force: bool = False) -> bool:
    """满足条件则后台补全画像；已在生成 / 条件不满足返回 False。"""
    config = mcp_config_manager.get_config(config_id)
    if config is None or not config.enabled or not config.register_as_ai_tools:
        return False
    if not _llm_enabled():
        return False
    if config_id in _PENDING:
        return False
    if not force and not _needs_generation(config_id, config):
        return False
    _FAILED.discard(config_id)
    _PENDING[config_id] = asyncio.create_task(_run(config_id, tools_hash(config)))
    return True


def _clean_description(text: str) -> Optional[str]:
    """清洗生成描述：逗号/冒号归一空格、去尾标点、4~40 字、必须含 CJK。"""
    desc = re.sub(r"[，,：:；;]+", " ", text.strip())
    desc = re.sub(r"\s+", " ", desc).strip(" 。！？!?.,:;")
    if not (4 <= len(desc) <= 40) or not has_cjk(desc):
        return None
    return desc


def _clean_keywords(raw: List[str]) -> List[str]:
    """关键词去重、限长 2~8、去纯 ASCII 与通用词，最多 8 个。"""
    banned = {"mcp", "工具", "服务器", "server", "tool", "tools"}
    out: List[str] = []
    for item in raw:
        kw = item.strip()
        if not (2 <= len(kw) <= 8) or kw.isascii() or kw.lower() in banned:
            continue
        if kw not in out:
            out.append(kw)
        if len(out) >= 8:
            break
    return out


def _parse_payload(raw: str) -> Optional[_ProfilePayload]:
    """从模型自由文本提取 JSON 并校验+清洗。LLM 输出属不可信外部输入。"""
    text = raw.strip()
    try:
        start = text.index("{")
        end = text.rindex("}")
        data: Any = json.loads(text[start : end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    try:
        payload = _ProfilePayload.model_validate(data)
    except ValidationError:
        return None
    description = _clean_description(payload.description)
    if description is None:
        return None
    return _ProfilePayload(description=description, keywords=_clean_keywords(payload.keywords))


async def _call_llm(config: MCPConfig) -> str:
    """高级档模型一次生成调用；超时/异常向外抛（由 _run 兜底计 failed）。"""
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.configs.ai_config import ai_config

    tools_brief = "\n".join(f"- {tool.name}: {first_line(tool.description)}" for tool in config.tools[:20])
    display = config.market_meta.get("chinese_name") or config.name
    user_msg = f"服务名称：{display}\n内部名：{config.name}\n工具清单：\n{tools_brief or '（空）'}"
    timeout = float(ai_config.get_config("mcp_profile_llm_timeout_sec").data or 60)
    agent = create_agent(
        system_prompt=_MCP_PROFILE_SYSTEM_PROMPT,
        max_tokens=600,
        create_by="MCPProfileGenerator",
        task_level="high",
        session_id=f"mcp_profile_{config.name}_{int(time.time())}",
        is_subagent=True,
        dynamic_tools=False,
    )
    try:
        result = await asyncio.wait_for(agent.run(user_message=user_msg, return_mode="return"), timeout=timeout)
        return result if isinstance(result, str) else str(result)
    finally:
        agent._session_logger.close()


async def _refresh_after_profile(config_id: str) -> None:
    """画像生效：重建节点 + 刷新工具 covers + 同步向量 + 预热节点索引。"""
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.agent_node import register_agent_node
    from gsuid_core.ai_core.mcp.startup import _sync_tool_index, _warm_agent_node_index
    from gsuid_core.ai_core.mcp.node_profile import mcp_tool_covers

    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return
    names = _registered_names(config)
    if not names:
        return
    register_agent_node(build_mcp_node(config_id, config, names))
    tool_defs = {tool.name: tool for tool in config.tools}
    prefix = f"mcp_{config.name}_"
    for registered in names:
        tool_base = find_tool_base(registered)
        tool_def = tool_defs.get(registered[len(prefix) :]) if registered.startswith(prefix) else None
        if tool_base is not None and tool_def is not None:
            tool_base.covers = mcp_tool_covers(config, tool_def.description)
    await _sync_tool_index()
    await _warm_agent_node_index()


async def _run(config_id: str, expected_hash: str) -> None:
    try:
        async with _semaphore():
            config = mcp_config_manager.get_config(config_id)
            if config is None or not config.enabled or not config.register_as_ai_tools:
                return
            if tools_hash(config) != expected_hash:
                logger.debug(t("log.mcp.profile_generate_discarded_stale", p0=config.name))
                return
            logger.info(t("log.mcp.profile_generate_start", p0=config.name))

            payload: Optional[_ProfilePayload] = None
            for _attempt in range(2):
                payload = _parse_payload(await _call_llm(config))
                if payload is not None:
                    break
            if payload is None:
                _FAILED.add(config_id)
                logger.warning(t("log.mcp.profile_generate_failed", p0=config.name))
                return

            config2 = mcp_config_manager.get_config(config_id)
            if config2 is None or not config2.enabled or tools_hash(config2) != expected_hash:
                logger.debug(t("log.mcp.profile_generate_discarded_stale", p0=config.name))
                return

            from gsuid_core.ai_core.configs.ai_config import ai_config

            model_name = ai_config.get_config("high_level_provider_config_name").data or "high"
            profile = GeneratedProfile(
                description=payload.description,
                keywords=payload.keywords,
                source_hash=expected_hash,
                model=model_name,
                generated_at=int(time.time()),
            )
            ok, msg = mcp_config_manager.update_config(config_id, {"generated_profile": dict(profile)})
            if not ok:
                _FAILED.add(config_id)
                logger.warning(t("log.mcp.profile_generate_failed", p0=f"{config.name}: {msg}"))
                return
            _FAILED.discard(config_id)
            await _refresh_after_profile(config_id)
            logger.info(t("log.mcp.profile_generate_done", p0=config.name))
    except Exception as e:
        _FAILED.add(config_id)
        logger.warning(t("log.mcp.profile_generate_failed", p0=f"{config_id}: {e}"))
    finally:
        _PENDING.pop(config_id, None)
