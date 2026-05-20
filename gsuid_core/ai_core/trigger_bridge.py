"""触发器 → AI 工具桥接模块

提供以下能力：
1. `ai_return()` — 在触发器函数内向 AI 返回纯文本中间结果
2. `MockBot` — AI 调用时拦截 bot.send，将文本内容收集返回给 AI；图片通过 RM 注册并返回资源 ID
3. `_register_trigger_as_ai_tool()` — 将触发器函数包装为 AI 工具并注册到 _TOOL_REGISTRY
"""

import re
import inspect
import contextvars
from copy import deepcopy
from typing import Any, Dict, List, Tuple, Union, Optional

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import _TOOL_REGISTRY, ToolBase, _get_plugin_name_from_module
from gsuid_core.utils.resource_manager import RM

# ─── ContextVar ───────────────────────────────────────────────────────────────
# AI 调用时为 dict（收集 send 内容），普通用户调用时为 None
_AI_CALL_CONTEXT: contextvars.ContextVar[Optional[Dict[str, list]]] = contextvars.ContextVar(
    "_AI_CALL_CONTEXT", default=None
)

# ─── MCP Trigger Registry ────────────────────────────────────────────────────
# 存储所有带 to_ai 的触发器的原始信息，供 MCP Server 模块使用
# 格式: {tool_name: {func, keyword, to_ai_doc, sv, trigger_type}}
_MCP_TRIGGER_REGISTRY: Dict[str, Dict[str, Any]] = {}


# ─── ai_return ────────────────────────────────────────────────────────────────


def ai_return(text: str) -> None:
    """
    在触发器函数内调用，向 AI 返回纯文本中间结果。

    当触发器由真实用户触发时，此函数什么也不做（静默忽略）。
    当触发器由 AI 工具调用时，文本会被收集，最终作为工具返回值返回给 AI。

    用法示例::

        from gsuid_core.ai_core.trigger_bridge import ai_return

        @sv.on_command("个股", to_ai=\"\"\"
        查询指定股票或ETF的分时图/K线图。
        Args:
            text: 股票代码或名称，多个以空格分隔，可选前缀 '日k'/'周k'/'月k'，
                  例如 "证券ETF" 或 "日k 证券ETF 白酒ETF"
        \"\"\")
        async def send_stock_img(bot: Bot, ev: Event):
            content = ev.text.strip()
            if not content:
                ai_return("请提供股票代码，例如：证券ETF")
                return await bot.send("请后跟股票代码使用")
            ...
    """
    ctx = _AI_CALL_CONTEXT.get()
    if ctx is not None:
        ctx["texts"].append(text)


# ─── MockBot ──────────────────────────────────────────────────────────────────


class MockBot:
    """
    AI 调用触发器时使用的代理 Bot。

    - **文本/消息 (str)**: 拦截并存入上下文，最终作为工具返回值返回给 AI（不发送给用户）
    - **图片/资源 (bytes, Message(type="image"))**: 暂存到上下文，不传给 AI 也不发送给用户；
      AI 收到文本描述后决定是否调用 ``send_trigger_images`` 真正发送
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
        recall: int = 0,
        wait_send_result: bool = False,
    ) -> None:
        """拦截 send：文本存入上下文返回给 AI，图片通过 RM 注册并返回资源 ID。"""
        ctx = object.__getattribute__(self, "_ctx")
        if isinstance(message, bytes):
            # bytes 通常是图片数据，注册到 RM 并记录资源 ID
            resource_id = RM.register(message)
            ctx["image_ids"].append(resource_id)
        elif isinstance(message, str):
            if _is_image_string(message):
                # base64://... 或 data:image/... 字符串，注册到 RM 并记录资源 ID
                resource_id = RM.register(message)
                ctx["image_ids"].append(resource_id)
            else:
                ctx["bot_messages"].append(message)
        else:
            # Message / List[Message] — 提取图片数据注册到 RM
            image_data = _extract_image_data(message)
            if image_data is not None:
                resource_id = RM.register(image_data)
                ctx["image_ids"].append(resource_id)
            else:
                # 纯文字 Message，转为字符串存入返回值
                ctx["bot_messages"].append(_message_to_text(message))

    async def reply(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        recall: int = 0,
        wait_send_result: bool = False,
    ) -> None:
        """拦截 reply，行为与 send 相同。"""
        await self.send(message, at_sender, recall, wait_send_result)

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
        stripped.startswith("base64://")
        or stripped.startswith("data:image/")
        or stripped.startswith("http://")
        or stripped.startswith("https://")
    )


def _extract_image_data(message: Any) -> Union[str, bytes, None]:
    """从消息对象中提取图片数据，用于 RM.register()。返回 None 表示非图片消息。"""
    if isinstance(message, bytes):
        return message
    if isinstance(message, str):
        if _is_image_string(message):
            return message
        return None
    if isinstance(message, Message):
        if message.type == "image" and message.data is not None:
            data = message.data
            if isinstance(data, (str, bytes)):
                return data
        return None
    if isinstance(message, (list, tuple)):
        for m in message:
            result = _extract_image_data(m)
            if result is not None:
                return result
    return None


def _message_contains_image(message: Any) -> bool:
    """检查消息对象是否包含图片段（image segment）。"""
    return _extract_image_data(message) is not None


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


# ─── _register_trigger_as_ai_tool ─────────────────────────────────────────────


def _register_trigger_as_ai_tool(
    func: Any,
    keyword: Union[str, Tuple[str, ...]],
    to_ai_doc: str,
    sv: Any,
    trigger_type: str,
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

    async def _ai_tool_wrapper(ctx: RunContext[ToolContext], text: str) -> str:
        real_bot = ctx.deps.bot
        ev = ctx.deps.ev
        assert real_bot is not None, "触发器 AI 工具调用时 bot 不能为 None"
        assert ev is not None, "触发器 AI 工具调用时 ev 不能为 None"

        # 权限检查：AI 调用时也需要遵守与用户直接触发相同的权限限制
        # user_pm: 0=master, 1=superuser, 2=群主/管理员, 3=普通用户
        # pm: 要求的最低权限等级，数值越小权限越高
        # 注意：运行时读取 sv/plugins 的当前状态，而非注册时快照
        if not sv.plugins.enabled:
            return "❌ 该插件已禁用。"
        if not sv.enabled:
            return "❌ 该功能已禁用。"
        if ev.user_pm > sv.plugins.pm:
            return f"❌ 权限不足：该插件需要权限等级 {sv.plugins.pm}，当前用户权限等级为 {ev.user_pm}。"
        if ev.user_pm > sv.pm:
            return f"❌ 权限不足：该功能需要权限等级 {sv.pm}，当前用户权限等级为 {ev.user_pm}。"

        # 模拟 ev：深拷贝 event 后修改 text 和 command
        fake_ev = deepcopy(ev)
        fake_ev.text = text
        fake_ev.command = primary_keyword
        # raw_text 也要对齐，保证 trigger 内部逻辑一致
        fake_ev.raw_text = f"{primary_keyword} {text}".strip()

        # 如果触发器类型是 regex，需要模拟 regex 匹配
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

        # 准备收集上下文
        call_ctx: Dict[str, Any] = {
            "texts": [],  # ai_return() 写入的文字
            "image_ids": [],  # bot.send 拦截到的图片，通过 RM 注册后的资源 ID
            "bot_messages": [],  # bot.send(str/Message(text)) 拦截到的文字
        }

        token = _AI_CALL_CONTEXT.set(call_ctx)
        mock_bot = MockBot(real_bot, call_ctx)

        try:
            await func(mock_bot, fake_ev)
        except Exception as e:
            logger.exception(f"🧠 [Trigger→AI] 触发器 [{primary_keyword}] 执行异常: {e}")
            return f"❌ 执行命令 [{primary_keyword}] 时发生错误: {e}。请尝试其他方式或检查输入参数。"
        finally:
            _AI_CALL_CONTEXT.reset(token)

        # 组装返回值（只包含纯文本 + 资源 ID，图片数据绝不进入返回值）
        parts: List[str] = []
        parts.extend(call_ctx["texts"])
        parts.extend(call_ctx["bot_messages"])

        if call_ctx["image_ids"]:
            image_count = len(call_ctx["image_ids"])
            id_list = ", ".join(call_ctx["image_ids"])
            parts.append(
                f"[已生成 {image_count} 张图片，资源ID: {id_list}。"
                f"请调用 send_message_by_ai 工具传入 image_id 将图片发送给用户，"
                f"或根据用户意图决定是否发送。]"
            )

        if parts:
            return "\n".join(parts)

        # 如果触发器没有调用 ai_return() 也没有 send 内容，返回通用成功提示
        return f"✅ 命令 [{primary_keyword}] 已执行，结果已发送给用户。"

    # 手动设置元数据，使 PydanticAI 能正确解析
    _ai_tool_wrapper.__name__ = tool_func_name
    _ai_tool_wrapper.__qualname__ = func.__qualname__
    _ai_tool_wrapper.__module__ = func.__module__
    _ai_tool_wrapper.__doc__ = to_ai_doc

    # 构造 __annotations__ 和 __signature__
    _ai_tool_wrapper.__annotations__ = {
        "ctx": RunContext[ToolContext],
        "text": str,
        "return": str,
    }

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
        ),
    ]
    _ai_tool_wrapper.__signature__ = inspect.Signature(
        parameters=new_params,
        return_annotation=str,
    )

    # 注册到 PydanticAI Tool
    tool_obj = Tool(_ai_tool_wrapper, takes_ctx=True)
    plugin_name = _get_plugin_name_from_module(func.__module__)

    tool_base = ToolBase(
        name=tool_func_name,
        description=to_ai_doc,
        plugin=plugin_name,
        tool=tool_obj,
    )

    if "by_trigger" not in _TOOL_REGISTRY:
        _TOOL_REGISTRY["by_trigger"] = {}

    _TOOL_REGISTRY["by_trigger"][tool_func_name] = tool_base
    logger.info(
        f"🧠 [Trigger→AI] 触发器 [{primary_keyword}] 的函数 [{tool_func_name}] "
        f"已注册为 AI 工具 (分类: by_trigger, 插件: {plugin_name})"
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
    }
