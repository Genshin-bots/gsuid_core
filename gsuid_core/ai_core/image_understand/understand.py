"""
Image Understand 公共 API 模块

提供统一的图片理解接口：优先使用大模型原生的多模态能力（model_support 含 image
时走 OpenAI / Anthropic 兼容请求），仅在模型不支持图片时才回退到独立的图片转述
模型（MCP）。外部模块应通过本模块的函数调用图片理解，无需关心底层实现细节。

会话日志：原生多模态转述同样是一次真实 LLM 调用——统一走 ``create_agent``
（自动派生 ``auto_ImageUnderstand_*`` 的 subagent 日志），并在拿到调用方
``parent_session_id`` 时 ``link_agent`` 挂到调用方主 session 的 linked_agents 上，
保证"任何 AI 调用都有日志"（不再裸用 pydantic_ai ``Agent()``）。MCP 回退路径是
外部工具调用而非 LLM agent run，由 MCP 侧自行记录，不进 AISessionLogger。
详见 ``docs/AI_SESSION_LOGGING.md``。
"""

import os
import time
import base64
import hashlib
import tempfile
from typing import Union, Literal, Optional

import aiofiles
from pydantic_ai.messages import ImageUrl
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.anthropic import AnthropicModel

from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.models import (
    get_model_for_task,
    get_model_config_for_task,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.mcp.mcp_tool_caller import call_mcp_tool
from gsuid_core.ai_core.mcp.mcp_tools_config import mcp_tools_config


def _get_provider() -> str:
    """
    获取当前配置的图片理解服务提供方

    Returns:
        提供方名称，如 "MCP"
    """
    return ai_config.get_config("image_understand_provider").data


# ── 图片理解结果短期缓存（O-C）─────────────────────────────────────────────
# memory 摄入（ImageUnderstandWorker）与即时回复（_prepare_user_message，模型不支持图时）
# 会对同一张图各调一次 understand_image——命中缓存即复用，省一次多模态 LLM/MCP 调用，并消除
# 两路描述不一致(desync)。N-1 说明：键是图片**来源字符串**(http URL 或 base64 DataURI)的 md5、
# **忽略 prompt**——对 DataURI（base64 直传）平台等价于"内容哈希"；但对返回**每次不同鉴权 URL**
# 的平台（如部分图床），同一张图跨消息会换 URL 而缓存不命中（仅多一次转述，非正确性问题）。
# 同一条消息的两路（即时回复 / 异步 memory worker）拿到的是同一 URL 串，复用始终有效——这正是
# TTL=600s 要覆盖的窗口。先到者写入、后到者复用同一段客观描述。纯进程内存、重启清空。
_UNDERSTAND_CACHE_TTL = 600.0  # 10 分钟，覆盖即时回复与异步 memory worker 的时间差
_UNDERSTAND_CACHE_MAX = 512
_understand_cache: dict[str, tuple[float, str]] = {}


def _img_cache_key(image_url: str) -> str:
    # N-1：哈希的是来源字符串（URL 或 DataURI），不是下载后的图片字节——详见上方缓存说明。
    return hashlib.md5(image_url.encode("utf-8", "ignore")).hexdigest()


def _understand_cache_get(key: str) -> Optional[str]:
    item = _understand_cache.get(key)
    if not item:
        return None
    expire_at, value = item
    if time.time() >= expire_at:
        _understand_cache.pop(key, None)
        return None
    return value


def _understand_cache_put(key: str, value: str) -> None:
    if not value:
        return
    now = time.time()
    # 容量上限：先清过期项，仍满则丢最早到期的一条
    if len(_understand_cache) >= _UNDERSTAND_CACHE_MAX:
        for k in [k for k, (exp, _) in _understand_cache.items() if exp <= now]:
            _understand_cache.pop(k, None)
        if len(_understand_cache) >= _UNDERSTAND_CACHE_MAX:
            oldest = min(_understand_cache, key=lambda k: _understand_cache[k][0])
            _understand_cache.pop(oldest, None)
    _understand_cache[key] = (now + _UNDERSTAND_CACHE_TTL, value)


def _resolve_native_image_model(
    task_level: Literal["high", "low"],
) -> Optional[Union[OpenAIChatModel, AnthropicModel]]:
    """若指定级别的模型在 model_support 中声明了 image，则返回其原生模型实例。

    模型原生支持图片时，应直接用大模型的多模态能力（OpenAI / Anthropic 兼容请求）
    转述图片，无需再单独配置图片转述模型（MCP）。不支持时返回 None，交由 MCP 兜底。
    """
    model_config = get_model_config_for_task(task_level)
    model_support = model_config.get_config("model_support").data
    if "image" in model_support:
        return get_model_for_task(task_level)
    return None


async def _understand_image_native(
    image_url: str,
    prompt: str,
    task_level: Literal["high", "low"],
    parent_session_id: Optional[str] = None,
) -> str:
    """用大模型原生多模态能力转述图片（OpenAI / Anthropic 兼容请求）。

    统一走 ``create_agent``：图片以 ImageUrl 形式连同 prompt 一起喂给大模型，由
    ``GsCoreAIAgent._execute_run`` 选 provider 并自动写一份 ``auto_ImageUnderstand_*``
    的 subagent 会话日志（``is_subagent=True``，落 ``session_logs/subagents/``）。

    会话窗口规则下 ``_resolve_native_image_model`` 已确认该 task_level 模型支持图片，
    因此 create_agent 内部的 ``_prepare_user_message`` 会保留 ImageUrl 而**不会**反过来
    递归调用本函数。

    Args:
        parent_session_id: 调用方（如主对话 GsCoreAIAgent）的 session_id；非空且仍在
            内存注册表时，把本次图片理解的 subagent 日志 link 到调用方 session 的
            linked_agents，便于 webconsole 下钻（"附到调用方 session"策略）。
    """
    from gsuid_core.ai_core.utils import _normalize_image_url
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    agent = create_agent(
        system_prompt="你是一个图片理解助手，只输出对图片内容的客观描述，不要输出多余的解释或寒暄。",
        max_tokens=1024,
        max_iterations=1,
        create_by="ImageUnderstand",
        task_level=task_level,
        is_subagent=True,
    )
    try:
        result = await agent.run(
            [prompt, ImageUrl(url=_normalize_image_url(image_url))],
            return_mode="return",
        )
        return str(result).strip()
    finally:
        # "附到调用方 session"：拿得到父 session 就把本次图片理解日志 link 过去，
        # 否则它仍以独立 auto_ImageUnderstand_* subagent 日志存在（webconsole 列表可见）。
        if parent_session_id:
            parent = get_ai_session_registry().get_ai_session(parent_session_id)
            if parent is not None:
                parent._session_logger.link_agent(
                    agent_session_id=agent.session_id,
                    agent_session_uuid=agent._session_logger.session_uuid,
                    agent_type="sub_agent",
                    create_by="ImageUnderstand",
                    log_file=str(agent._session_logger._file_path),
                )
        agent._session_logger.close()


async def _prepare_image_for_mcp(image_url: str) -> str:
    """
    准备图片数据给 MCP 工具使用

    MiniMax MCP 的 understand_image 工具期望文件路径，不是 base64 或 URL。
    如果是 base64 DataURI，需要先保存为临时文件。

    Args:
        image_url: 图片来源，支持 HTTP/HTTPS URL、base64 DataURI 或文件路径

    Returns:
        文件路径（临时文件路径或原始 URL/路径）
    """
    # 如果已经是 HTTP/HTTPS URL 或文件路径，直接返回
    if image_url.startswith("http://") or image_url.startswith("https://"):
        return image_url

    if os.path.exists(image_url):
        return image_url

    # 如果是 base64 DataURI 格式，保存为临时文件
    if image_url.startswith("data:"):
        # 解析 DataURI 格式: data:image/png;base64,xxxxx
        if ";base64," in image_url:
            header, b64_data = image_url.split(";base64,", 1)
            # 提取 MIME 类型
            mime_type = header.replace("data:", "")
            # 解码 base64
            image_bytes = base64.b64decode(b64_data)

            # 创建临时文件
            suffix = ".png"
            if mime_type == "image/jpeg":
                suffix = ".jpg"
            elif mime_type == "image/gif":
                suffix = ".gif"
            elif mime_type == "image/webp":
                suffix = ".webp"

            temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
            os.close(temp_fd)
            async with aiofiles.open(temp_path, "wb") as f:
                await f.write(image_bytes)

            logger.debug(f"🖼️ [ImageUnderstand] 已保存图片到临时文件: {temp_path}")
            return temp_path

    # 其他情况直接返回
    return image_url


async def understand_image(
    image_url: str,
    prompt: str | None = None,
    task_level: Literal["high", "low"] = "high",
    parent_session_id: Optional[str] = None,
) -> str:
    """
    统一的图片理解接口

    将图片内容转述为文本描述。优先使用大模型原生的多模态能力：
    - 当前模型在 model_support 中声明了 image 时，直接用大模型（OpenAI / Anthropic
      兼容请求）转述图片，**无需配置图片转述模型，配置了也优先走原生多模态**。
    - 仅当模型不支持图片时，才回退到 image_understand_provider 配置的转述模型（MCP）。

    Args:
        image_url: 图片来源，支持 HTTP/HTTPS URL、base64 DataURI 或文件路径
        prompt: 对图片的提问或分析要求，默认为通用描述
        task_level: 用于判断 model_support 并选择原生多模态模型的任务级别
        parent_session_id: 调用方 session_id（如主对话 GsCoreAIAgent.session_id）；
            原生多模态路径会把图片理解的 subagent 日志 link 到该 session（见
            ``_understand_image_native``）。

    Returns:
        图片内容的文本描述

    Raises:
        RuntimeError: 图片理解失败时抛出

    Example:
        >>> description = await understand_image("https://example.com/image.png")
        >>> print(description)
        "这是一张风景照片，画面中有一座山..."
    """
    if not prompt:
        prompt = "请详细描述这张图片的内容，包括主要对象、场景、文字、颜色等信息。"

    # O-C 缓存：同图短期内复用同一段描述（按来源字符串 URL/DataURI 哈希、忽略 prompt，见 N-1）
    cache_key = _img_cache_key(image_url)
    cached = _understand_cache_get(cache_key)
    if cached:
        logger.debug("🖼️ [ImageUnderstand] 命中图片理解缓存，跳过重复解析")
        return cached

    # 优先：当前模型原生支持图片时，直接走大模型多模态，无需配置转述模型(MCP)
    native_model = _resolve_native_image_model(task_level)
    if native_model is not None:
        logger.debug("🖼️ [ImageUnderstand] 当前模型原生支持图片，使用大模型多模态能力转述")
        desc = await _understand_image_native(
            image_url,
            prompt,
            task_level=task_level,
            parent_session_id=parent_session_id,
        )
        _understand_cache_put(cache_key, desc)
        return desc

    provider = _get_provider()

    if provider == "MCP":
        mcp_tool_id = mcp_tools_config.get_config("image_understand_mcp_tool_id").data

        if not mcp_tool_id:
            raise RuntimeError("Image Understand MCP 工具未配置，请前往 AI 配置页面设置")

        # MiniMax MCP 的 understand_image 工具期望文件路径
        # 需要将图片数据保存为临时文件
        image_source = await _prepare_image_for_mcp(image_url)

        arguments = {
            "image_source": image_source,
            "prompt": prompt,
        }

        try:
            result = await call_mcp_tool(mcp_tool_id=mcp_tool_id, arguments=arguments)

            if result.is_error:
                raise RuntimeError(f"Image Understand MCP 调用失败: {result.text}")

            _understand_cache_put(cache_key, result.text)
            return result.text
        finally:
            # 清理临时文件
            if image_source != image_url and os.path.exists(image_source):
                try:
                    os.unlink(image_source)
                    logger.debug(f"🖼️ [ImageUnderstand] 已删除临时文件: {image_source}")
                except Exception as e:
                    logger.warning(f"🖼️ [ImageUnderstand] 删除临时文件失败: {e}")

    # 未知 provider
    logger.warning(f"🖼️ [ImageUnderstand] 未知的提供方 '{provider}'，仅支持 MCP")
    raise RuntimeError(f"Image Understand 不支持该提供方: {provider}")
