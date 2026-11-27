"""触发器 → AI 工具桥接模块

提供以下能力：
1. `ai_return()` — 在触发器函数内向 AI 返回纯文本中间结果
2. `MockBot` — AI 调用时拦截 bot.send，将文本内容收集返回给 AI；图片通过 RM 注册并返回资源 ID
3. `_register_trigger_as_ai_tool()` — 将触发器函数包装为 AI 工具并注册到 _TOOL_REGISTRY
"""

import re
import json
import inspect
import contextvars
from copy import deepcopy
from typing import Any, Dict, List, Tuple, Union, Optional, TypeGuard

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import _TOOL_REGISTRY, ToolBase, _get_plugin_name_from_module
from gsuid_core.utils.resource_manager import RM

# ─── ContextVar ───────────────────────────────────────────────────────────────
# AI 调用时为 dict（收集 send 内容），普通用户调用时为 None
_AI_CALL_CONTEXT: contextvars.ContextVar[Optional[Dict[str, list]]] = contextvars.ContextVar(
    "_AI_CALL_CONTEXT", default=None
)

# MCP Trigger Registry
# 存储所有带 to_ai 的触发器的原始信息，供 MCP Server 模块使用
_MCP_TRIGGER_REGISTRY: Dict[str, Dict[str, Any]] = {}


# ─── ai_return ────────────────────────────────────────────────────────────────


def ai_return(text: str) -> None:
    """
    在触发器函数内调用，向 AI 返回纯文本中间结果。

    当触发器由真实用户触发时，此函数什么也不做（静默忽略）。
    当触发器由 AI 工具调用时，文本会被收集，最终作为工具返回值返回给 AI。

    用法示例::

        from gsuid_core.ai_core.trigger_bridge import ai_return

        @sv.on_command("天气", to_ai=\"\"\"
        查询指定城市的天气。
        Args:
            text: 城市名，例如 "上海" 或 "北京"
        \"\"\")
        async def send_weather(bot: Bot, ev: Event):
            content = ev.text.strip()
            if not content:
                ai_return("请提供城市名，例如：上海")
                return await bot.send("请后跟城市名使用")
            ...
    """
    ctx = _AI_CALL_CONTEXT.get()
    if ctx is not None:
        ctx["texts"].append(text)


# ─── MockBot ──────────────────────────────────────────────────────────────────


class MockBot:
    _real_bot: Bot
    _ctx: Dict[str, Any]

    """
    AI 调用触发器时使用的代理 Bot。

    - **文本/消息 (str)**: 拦截并存入上下文，最终作为工具返回值返回给 AI（不发送给用户）
    - **图片/音视频/文件 (bytes, Message(type="image"/"record"/"video"/"file"))**: 逐段登记
      到 RM 并返回资源 ID；AI 收到文本描述后决定是否调用 ``send_message_by_ai`` 真正发送
    - **其他属性**: 代理到真实 Bot，保证触发器内部对 ``bot.bot_self_id`` 等属性的访问正常

    普通用户触发时不会使用此类，触发器直接拿到真实 Bot。
    """

    def __init__(self, real_bot: Bot, ctx: Dict[str, Any]) -> None:
        # 使用 object.__setattr__ 避免触发 msgspec.Struct 的 __setattr__
        object.__setattr__(self, "_real_bot", real_bot)
        object.__setattr__(self, "_ctx", ctx)

    async def send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        wait_recall: bool = False,
    ) -> Optional[List[str]]:
        """拦截 send：文本存入上下文返回给 AI，图片/音频/视频/文件通过 RM 注册并返回资源 ID。

        AI 上下文下不真正发送消息，故 wait_recall 被忽略、恒返回 None（无真实出站消息）。
        """
        ctx = object.__getattribute__(self, "_ctx")
        texts: List[str] = []
        self._collect(ctx, message, texts)
        merged = " ".join(part for part in texts if part)
        if merged:
            ctx["bot_messages"].append(merged)
        return None

    def _collect(self, ctx: Dict[str, Any], message: object, texts: List[str]) -> None:
        """逐段收集：媒体段登记 RM，文字片段进 texts 待拼成一条。"""
        if isinstance(message, bytes):
            ctx["image_ids"].append(RM.register(message))
        elif isinstance(message, str):
            if _is_image_string(message):
                ctx["image_ids"].append(RM.register(message))
            else:
                texts.append(message)
        elif isinstance(message, Message):
            data = message.data
            if message.type == "image" and isinstance(data, (str, bytes)):
                ctx["image_ids"].append(RM.register(data))
            elif message.type == "record" and isinstance(data, (str, bytes)):
                ctx["audio_ids"].append(RM.register_audio(data))
            elif message.type == "video" and isinstance(data, (str, bytes)):
                ctx["video_ids"].append(RM.register_video(data))
            elif message.type == "file" and isinstance(data, str):
                file_id = RM.register_file(data)
                if file_id is None:
                    texts.append(f"[文件 {data.split('|', 1)[0][:64]} 超过10MB或为空，未登记]")
                else:
                    ctx["file_ids"].append(file_id)
            else:
                texts.append(_message_to_text(message))
        elif isinstance(message, (list, tuple)):
            for item in message:
                self._collect(ctx, item, texts)
        else:
            texts.append(_message_to_text(message))

    async def reply(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        wait_recall: bool = False,
    ) -> Optional[List[str]]:
        """拦截 reply，行为与 send 相同。"""
        return await self.send(message, at_sender, wait_recall)

    async def send_option(
        self,
        reply: Any = None,
        option_list: Any = None,
        **kwargs: Any,
    ) -> None:
        """拦截 send_option：只处理 reply 中的图片/文字，忽略按钮。"""
        if reply is not None:
            await self.send(reply)

    async def receive_resp(
        self,
        reply: Any = None,
        option_list: Any = None,
        **kwargs: Any,
    ) -> None:
        """拦截 receive_resp：只处理 reply 中的图片/文字，返回 None 表示无用户响应。

        AI 调用触发器时不支持交互式等待用户输入，因此始终返回 None。
        """
        if reply is not None:
            await self.send(reply)
        return None

    def __getattr__(self, name: str) -> Any:
        """代理所有其他属性到真实 Bot。"""
        return getattr(object.__getattribute__(self, "_real_bot"), name)


def _is_image_string(s: str) -> bool:
    """检查字符串是否为图片数据（base64 编码或 data URI）。"""
    stripped = s.strip()
    return (
        stripped.startswith("link://")
        or stripped.startswith("base64://")
        or stripped.startswith("data:image/")
        or stripped.startswith("http://")
        or stripped.startswith("https://")
    )


def _message_to_text(message: Any) -> str:
    """将纯文字 Message 对象转为字符串，避免将图片数据序列化。"""
    if isinstance(message, str):
        return message
    if isinstance(message, Message):
        if message.type == "text" and isinstance(message.data, str):
            return message.data
        # 非文字类型的 Message，只返回类型描述，不序列化 data
        return f"[{message.type or 'unknown'}消息]"
    if isinstance(message, (list, tuple)):
        return " ".join(_message_to_text(m) for m in message)
    return str(message)


def _as_str_list(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    found: List[str] = []
    for item in value:
        if isinstance(item, str):
            found.append(item)
    return found


def _ctx_str_list(call_ctx: Dict[str, Any], key: str) -> List[str]:
    if key not in call_ctx:
        return []
    return _as_str_list(call_ctx[key])


def _is_mcp_extra(extra: object) -> TypeGuard[Dict[str, object]]:
    return isinstance(extra, dict) and "source" in extra and extra["source"] == "mcp_server"


def _remember_mcp_image_ids(extra: Dict[str, object], image_ids: List[str]) -> None:
    if "mcp_image_ids" not in extra or not isinstance(extra["mcp_image_ids"], list):
        extra["mcp_image_ids"] = []
    bucket = extra["mcp_image_ids"]
    assert isinstance(bucket, list)
    for iid in image_ids:
        if iid not in bucket:
            bucket.append(iid)


def _append_mcp_av_notes(parts: List[str], audio_ids: List[str], video_ids: List[str], file_ids: List[str]) -> None:
    if audio_ids:
        parts.append(f"[已生成 {len(audio_ids)} 个音频，MCP 结果未附带音频字节。]")
    if video_ids:
        parts.append(f"[已生成 {len(video_ids)} 个视频，MCP 结果未附带视频字节。]")
    if file_ids:
        parts.append(f"[已生成 {len(file_ids)} 个文件，MCP 结果未附带文件字节。]")


def _assemble_trigger_output(call_ctx: Dict[str, Any], extra: object = None) -> str:
    """把 MockBot 收集到的文本和资源说明组装成返回字符串。图片字节不进字符串。"""
    parts: List[str] = []
    parts.extend(_ctx_str_list(call_ctx, "texts"))
    parts.extend(_ctx_str_list(call_ctx, "bot_messages"))
    image_ids = _ctx_str_list(call_ctx, "image_ids")
    audio_ids = _ctx_str_list(call_ctx, "audio_ids")
    video_ids = _ctx_str_list(call_ctx, "video_ids")
    file_ids = _ctx_str_list(call_ctx, "file_ids")
    if _is_mcp_extra(extra):
        _remember_mcp_image_ids(extra, image_ids)
        _append_mcp_av_notes(parts, audio_ids, video_ids, file_ids)
        return "\n".join(parts)
    from gsuid_core.ai_core.control.lines import format_rm_resource_hint

    if image_ids:
        parts.append(format_rm_resource_hint("image", image_ids))
    if audio_ids:
        parts.append(format_rm_resource_hint("audio", audio_ids))
    if video_ids:
        parts.append(format_rm_resource_hint("video", video_ids))
    if file_ids:
        parts.append(format_rm_resource_hint("file", file_ids))
    return "\n".join(parts)


async def run_trigger_via_mockbot(real_bot: Bot, fake_ev: Event, func: Any, extra: object = None) -> str:
    """用 MockBot 实跑一个触发器处理函数并收集其产出（不真正发给用户）。

    供两处复用：
    - to_ai 触发器的 AI 工具包装（``_ai_tool_wrapper`` 内联了等价逻辑）；
    - 插件开发自测工具（``plugin_developer.test_plugin_command``）对**纯命令**触发器
      （未声明 to_ai）的实跑——纯命令插件本就无需 to_ai 即可工作，自测也应支持，
      避免开发代理因"找不到 to_ai 触发器"反复改代码陷入死循环。

    返回收集到的文本 / 资源摘要；触发器无任何产出时返回空串（话术交调用方决定）。
    异常向上抛出，由调用方按需包装。
    """
    call_ctx: Dict[str, Any] = {
        "texts": [],
        "image_ids": [],
        "audio_ids": [],
        "video_ids": [],
        "file_ids": [],
        "bot_messages": [],
    }
    token = _AI_CALL_CONTEXT.set(call_ctx)
    try:
        await func(MockBot(real_bot, call_ctx), fake_ev)
    finally:
        _AI_CALL_CONTEXT.reset(token)
    return _assemble_trigger_output(call_ctx, extra)


# ─── _register_trigger_as_ai_tool ─────────────────────────────────────────────


def _register_trigger_as_ai_tool(
    func: Any,
    keyword: Union[str, Tuple[str, ...]],
    to_ai_doc: str,
    sv: Any,
    trigger_type: str,
    covers: list[str] | None = None,
    aliases: list[str] | None = None,
    timeout: Optional[float] = 60.0,
    background: bool = False,
    background_grace: float = 5.0,
) -> None:
    """
    将一个触发器函数包装为 AI 工具并注册到 _TOOL_REGISTRY["by_trigger"]。

    生成工具签名::

        async def <func_name>(ctx: RunContext[ToolContext], text: str) -> str

    AI 调用时：
    1. 按照 to_ai_doc 中的说明构建 text 参数
    2. 包装函数使用 MockBot 拦截 bot.send，将图片/消息内容收集而非真正发送
    3. 模拟 ev.text = text，以及触发器命中所需的 ev.command
    4. 调用原始触发器函数
    5. 收集 ai_return() 写入的中间文本 + bot.send 拦截的内容作为工具返回值

    Args:
        func: 原始触发器函数
        keyword: 触发器关键字（字符串或元组）
        to_ai_doc: AI 工具的 docstring
        sv: SV 实例
        trigger_type: 触发器类型（command/prefix/keyword/fullmatch/suffix/regex 等）
        covers: 数据/能力覆盖面，进向量检索与 find_tools 单向命中
        aliases: 带领域前缀的同义问法，进检索文本
    """
    # 检查AI是否启用，未启用则跳过触发器工具注册
    try:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        if not ai_config.get_config("enable").data:
            return
    except Exception:
        pass

    # 取第一个 keyword 作为命令（用于填充 ev.command）
    primary_keyword = keyword[0] if isinstance(keyword, tuple) else keyword

    # 工具函数名：使用原函数名
    tool_func_name = func.__name__

    def _check_sv_access(ctx: RunContext[ToolContext]) -> Tuple[bool, str]:
        ev = ctx.deps.ev
        if ev is None:
            return False, "❌ 无法获取事件上下文。"
        if sv.plugins is not None and not sv.plugins.enabled:
            return False, "❌ 该插件已禁用。"
        if not sv.enabled:
            return False, "❌ 该功能已禁用。"
        if sv.plugins is not None and ev.user_pm > sv.plugins.pm:
            return False, f"❌ 权限不足：该插件需要权限等级 {sv.plugins.pm}，当前用户权限等级为 {ev.user_pm}。"
        if ev.user_pm > sv.pm:
            return False, f"❌ 权限不足：该功能需要权限等级 {sv.pm}，当前用户权限等级为 {ev.user_pm}。"
        return True, ""

    async def _ai_tool_wrapper(
        ctx: RunContext[ToolContext],
        text: str = "",
        image_id: str = "",
        audio_id: str = "",
    ) -> str:
        from gsuid_core.ai_core.register import execute_guarded_tool

        async def _call_with_ctx(run_ctx: RunContext[ToolContext]) -> str:
            real_bot = run_ctx.deps.bot
            ev = run_ctx.deps.ev
            assert real_bot is not None, "触发器 AI 工具调用时 bot 不能为 None"
            assert ev is not None, "触发器 AI 工具调用时 ev 不能为 None"

            fake_ev = deepcopy(ev)
            fake_ev.text = text
            fake_ev.command = primary_keyword
            fake_ev.raw_text = f"{primary_keyword} {text}".strip()
            if image_id:
                fake_ev.image_id = image_id
            if audio_id:
                fake_ev.audio_id = audio_id

            if trigger_type == "regex":
                match = re.search(primary_keyword, text)
                if match:
                    fake_ev.regex_dict = match.groupdict()
                    fake_ev.regex_group = match.groups()
                    fake_ev.command = "|".join(g if g is not None else "" for g in match.groups())
                else:
                    fake_ev.regex_dict = {}
                    fake_ev.regex_group = ()
                    fake_ev.command = text

            try:
                out = await run_trigger_via_mockbot(real_bot, fake_ev, func, run_ctx.deps.extra)
            except Exception as e:
                logger.exception(t("log.ai.trigger_ai_primary_keyword_fail", primary_keyword=primary_keyword, e=e))
                return f"❌ 执行命令 [{primary_keyword}] 时发生错误: {e}。请尝试其他方式或检查输入参数。"
            if out:
                return out
            return f"命令 [{primary_keyword}] 已执行，但没有产出可发送的文本或图片。不要对用户说已经发出。"

        async def _call() -> str:
            return await _call_with_ctx(ctx)

        norm_text = text.strip()
        args_key = (
            json.dumps((norm_text, image_id, audio_id), ensure_ascii=False)
            if (norm_text or image_id or audio_id)
            else ""
        )
        res = await execute_guarded_tool(
            tool_func_name,
            ctx,
            _call,
            call_with_ctx=_call_with_ctx,
            check_func=_check_sv_access,
            timeout=timeout,
            track_health=True,
            raw_passthrough=True,
            background=background,
            background_grace=background_grace,
            background_label=primary_keyword,
            background_args_key=args_key,
        )
        return res if isinstance(res, str) else str(res)

    # 手动设置元数据，使 PydanticAI 能正确解析
    _ai_tool_wrapper.__name__ = tool_func_name
    _ai_tool_wrapper.__qualname__ = func.__qualname__
    _ai_tool_wrapper.__module__ = func.__module__
    _ai_tool_wrapper.__doc__ = to_ai_doc

    # 构造 __annotations__ 和 __signature__
    _ai_tool_wrapper.__annotations__ = {
        "ctx": RunContext[ToolContext],
        "text": str,
        "image_id": str,
        "audio_id": str,
        "return": str,
    }

    text_default = "" if trigger_type == "fullmatch" else inspect.Parameter.empty
    new_params = [
        inspect.Parameter(
            "ctx",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=RunContext[ToolContext],
        ),
        inspect.Parameter(
            "text",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=str,
            default=text_default,
        ),
        inspect.Parameter(
            "image_id",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=str,
            default="",
        ),
        inspect.Parameter(
            "audio_id",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=str,
            default="",
        ),
    ]
    # FunctionType 静态无 __signature__ 字段; 改 setattr 动态写入, pyright 不再标红
    # （与 register.py 的 @ai_tools 装饰器同款写法保持一致）。
    setattr(
        _ai_tool_wrapper,
        "__signature__",
        inspect.Signature(
            parameters=new_params,
            return_annotation=str,
        ),
    )

    # 注册到 PydanticAI Tool
    tool_obj = Tool(_ai_tool_wrapper, takes_ctx=True)
    plugin_name = _get_plugin_name_from_module(func.__module__)
    from gsuid_core.ai_core.buildin_tools.find_tools_rank import covers_from_trigger_keywords

    merged_covers = covers_from_trigger_keywords(keyword, covers)

    tool_base = ToolBase(
        name=tool_func_name,
        description=to_ai_doc,
        plugin=plugin_name,
        tool=tool_obj,
        category="by_trigger",
        covers=merged_covers,
        aliases=aliases,
        check_func=_check_sv_access,
        code_callable=not background,
        check_in_wrapper=True,
    )

    if "by_trigger" not in _TOOL_REGISTRY:
        _TOOL_REGISTRY["by_trigger"] = {}

    _TOOL_REGISTRY["by_trigger"][tool_func_name] = tool_base
    from gsuid_core.logger import hl_plugin

    logger.debug(
        t(
            "log.ai.trigger_ai_func_name_primary",
            primary_keyword=primary_keyword,
            tool_func_name=tool_func_name,
            plugin_name=hl_plugin(plugin_name),
        )
    )

    # 同时注册到 MCP 触发器注册表，供 MCP Server 模块使用
    _MCP_TRIGGER_REGISTRY[tool_func_name] = {
        "func": func,
        "keyword": keyword,
        "to_ai_doc": to_ai_doc,
        "sv": sv,
        "trigger_type": trigger_type,
        "plugin_name": plugin_name,
        "primary_keyword": primary_keyword,
        "covers": list(merged_covers),
        "aliases": list(aliases or []),
    }
