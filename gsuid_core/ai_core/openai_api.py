import re
import copy
import json
import random
import asyncio
import inspect
from typing import Any, Dict, List, Tuple, Union, Optional, cast
from pathlib import Path

import aiofiles
import tiktoken
from bot import Bot
from PIL import Image
from models import Event
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage, ChatCompletionMessageToolCall

from gsuid_core.logger import logger
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.ai_core.register import get_registered_tools
from gsuid_core.ai_core.ai_config import openai_config
from gsuid_core.utils.resource_manager import RM
from gsuid_core.utils.image.image_tools import image_to_base64

from .models import ToolDef

FileInput = Union[str, Path]
MessageUnion = Union[Dict, Any]


# 使用 tiktoken 进行精确的 token 估算
def estimate_tokens(msg: MessageUnion, enc: Optional[tiktoken.Encoding] = None) -> int:
    """使用 tiktoken 精确估算消息的 token 数"""
    if enc is None:
        # 默认使用 cl100k_base (gpt-4, gpt-3.5-turbo)
        enc = tiktoken.get_encoding("cl100k_base")

    total_tokens = 0
    content = msg.get("content", "")
    if isinstance(content, str):
        total_tokens = len(enc.encode(content))
    elif isinstance(content, list):
        # 对于包含图片等的复杂内容，估算一个固定值
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    total_tokens += len(enc.encode(text))
                elif item.get("type") == "image_url":
                    # 图片 token 估算 (gpt-4-vision 约为 85 + 170 * 瓦片数)
                    total_tokens += 255  # 粗略估算

    # 为每条消息增加结构化 Token 冗余（role, name 等字段约占 4-5 个 token）
    return total_tokens + 5 if total_tokens > 0 else 5


class AsyncOpenAISession:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = "",
        system_prompt: Optional[str] = None,
    ):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        # 基础人设（所有模式下通用的部分）
        self.base_persona = system_prompt or "你是一个智能助手。"
        # 历史记录只保存 user 和 assistant 的对话，不保存 system 消息
        self.history = []
        # 配置：最大工具调用迭代次数，防止无限循环
        self.max_tool_iterations = 5
        # 配置：最大历史 token 数量，按 token 裁剪（粗略估算，每个中文字符约2-3个token）
        self.max_history_tokens = 6000
        # 配置：OpenAI API 最大输出 token 数
        self.max_tokens = 1800
        # 当前历史记录的 token 数，避免每轮全量重新计算
        self.current_token_count = 0
        # 初始化 tiktoken encoder
        try:
            self.tokenizer = tiktoken.encoding_for_model(model)
        except KeyError:
            # 如果模型未知，使用默认的 cl100k_base
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def _safe_truncate_history(self) -> None:
        """
        安全截断历史记录，避免切断工具链。
        策略：
        1. 成对删除消息（确保不删除单个消息导致角色错乱）
        2. 如果遇到 tool 消息，必须连带删除对应的 assistant tool_call 消息
        3. 保留至少一个完整的对话轮次
        优化：使用 self.current_token_count 避免每轮全量重新计算
        """
        # 如果还没有计算过当前 token 数量，先计算一次
        if self.current_token_count == 0:
            self.current_token_count = sum(estimate_tokens(msg, self.tokenizer) for msg in self.history)

        while True:
            if self.current_token_count <= self.max_history_tokens or len(self.history) <= 2:
                break

            # 查找可以安全删除的最前面的消息组
            # 策略：找到最早的 user 消息，然后删除 user + 后续的 assistant（包括 tool chain）
            removed_count = 0
            i = 0
            while i < len(self.history) and removed_count == 0:
                msg = self.history[i]
                if isinstance(msg, dict) and msg.get("role") == "user":
                    # 找到了最早的 user 消息，现在需要删除这个 user 以及后续关联的消息
                    # 删除 user
                    removed_msg = self.history.pop(i)
                    removed_count += 1
                    self.current_token_count -= estimate_tokens(removed_msg, self.tokenizer)

                    # 继续删除后续关联的 assistant 和 tool 消息
                    # 直到遇到下一个 user 或 history 结束
                    while i < len(self.history):
                        next_msg = self.history[i]
                        if isinstance(next_msg, dict) and next_msg.get("role") == "user":
                            # 遇到下一个 user，停止删除
                            break

                        # 只要不是 user，统统删掉（包括 assistant, tool，以及残留的其他内容）
                        removed_msg = self.history.pop(i)
                        removed_count += 1
                        self.current_token_count -= estimate_tokens(removed_msg, self.tokenizer)
                        # 此处不需要 i += 1，因为 pop 后元素自动前移
                    break
                i += 1

            if removed_count > 0:
                logger.debug(f"🧠 [AI][OpenAI] 历史消息 token 数超限，安全删除 {removed_count} 条消息")
            else:
                # 无法安全删除，直接删除最早的一条（保底策略）
                removed_msg = self.history.pop(0)
                self.current_token_count -= estimate_tokens(removed_msg, self.tokenizer)
                logger.warning(
                    f"🧠 [AI][OpenAI] 历史消息 token 数超限，强制删除最早消息: {removed_msg.get('role', 'unknown')}"
                )

    async def _process_file(self, file_path: FileInput) -> str:
        """
        内部函数：读取文本文件内容。
        注意：对于非 Vision 模型的非图片文件，通常是将内容作为 Context 放入 Prompt。
        """
        path = Path(file_path)
        if not path.exists():
            return f"[System Error: File {path.name} not found]"

        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                content = await f.read()
            return f"\n--- File Content: {path.name} ---\n{content}\n----------------\n"
        except UnicodeDecodeError:
            return f"[System Error: File {path.name} is binary or not UTF-8 text, cannot read directly.]"

    async def chat(
        self,
        text: str = "",
        image_ids: Optional[Union[str, List[str]]] = None,
        files: Optional[Union[FileInput, List[FileInput]]] = None,
        tools: Optional[List[ToolDef]] = None,
        json_mode: bool = False,
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        temp_system: Optional[str] = None,
        user_context: Optional[str] = None,
    ) -> Union[List[Message], dict]:
        # 1. 准备消息内容列表 (content)
        content_payload = []

        if files:
            if not isinstance(files, list):
                files = [files]
            for f in files:
                # 假设 _process_file 返回的是文本字符串
                file_text = await self._process_file(f)
                text += file_text

        if text:
            if ev:
                for i in ev.image_id_list:
                    text += f"\n--- Upload Image ID: {i} ---\n"

            content_payload.append({"type": "text", "text": text})

        if image_ids:
            images = [await RM.get(_id) for _id in image_ids]

            for img_input in images:
                base64_url = image_to_base64(img_input)

                if base64_url:
                    content_payload.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": base64_url,
                                # "detail": "auto" # 可选：控制图片解析精度
                            },
                        }
                    )

        # --- D. 处理 JSON 提示 ---
        # 如果开启 JSON 模式，为了防止报错，确保 Prompt 里包含 "JSON"
        if json_mode:
            # 检查当前 payload 里是否有文本提示
            has_json_in_text = False
            for item in content_payload:
                if item["type"] == "text" and "json" in item["text"].lower():
                    has_json_in_text = True
                    break

            if not has_json_in_text:
                content_payload.append({"type": "text", "text": "\n(Please strictly respond in JSON format)"})

        # --- 空内容检查 ---
        if not content_payload:
            raise ValueError("[AI] Empty input (no text or images provided).")

        # 2. 动态组装最终发送给 API 的消息列表
        # 组装最终的 System Prompt = 基础人设 + 当前意图专属规则
        # 注意：RAG 参考资料应该通过 user_context 参数放在用户消息中，而不是 system prompt
        final_system_content = self.base_persona
        if temp_system:
            final_system_content = f"{self.base_persona}\n\n{temp_system}"

        # 构建历史记录专用的 User 消息（干净的，无 RAG 资料）
        history_user_msg = {"role": "user", "content": copy.deepcopy(content_payload)}

        # 构建发给 API 的 User 消息（包含本轮 RAG 资料）
        api_user_msg_content = copy.deepcopy(content_payload)
        if user_context:
            # 找到 text 节点并注入 RAG
            for item in api_user_msg_content:
                if item["type"] == "text":
                    item["text"] = f"{user_context}\n\n--- 用户问题 ---\n{item['text']}"
                    break

        current_api_user_msg = {"role": "user", "content": api_user_msg_content}

        api_messages = [{"role": "system", "content": final_system_content}]
        # 加上历史记忆（让大模型知道上文）
        api_messages.extend(self.history)
        # 加上用户这一轮的最新问题（带 RAG 的版本）
        api_messages.append(current_api_user_msg)

        tools_reply: List[Message] = []

        # 临时的使用中的消息列表（用于工具调用的上下文）
        working_messages: List[MessageUnion] = api_messages.copy()

        # 记录初始长度（此时包含了 current_api_user_msg）
        initial_working_length = len(working_messages)

        # 工具调用迭代计数器，防止无限循环
        iteration_count = 0
        # JSON 解析错误次数计数器，防止无限自我修复循环
        json_error_count = 0

        while True:
            # 检查最大迭代次数
            iteration_count += 1
            if iteration_count > self.max_tool_iterations:
                logger.warning(f"🧠 [AI][OpenAI] 工具调用次数超过最大限制 {self.max_tool_iterations}，终止循环")
                raise RuntimeError(f"Tool call exceeded max depth of {self.max_tool_iterations}")

            # 3. 在循环内重建 request_kwargs，避免状态污染
            # 确保发送给 API 的消息不包含非法字段（如 _turn_id）
            sanitized_messages = []
            for msg in working_messages:
                if isinstance(msg, dict):
                    # 移除所有 OpenAI API 不允许的字段
                    sanitized_msg = msg.copy()
                    # 只保留 OpenAI API 允许的字段
                    allowed_fields = {"role", "content", "name", "tool_calls", "tool_call_id"}
                    for key in list(sanitized_msg.keys()):
                        if key not in allowed_fields:
                            del sanitized_msg[key]
                    sanitized_messages.append(sanitized_msg)
                else:
                    # 如果不是字典，直接添加
                    sanitized_messages.append(msg)

            request_kwargs = {
                "model": self.model,
                "messages": sanitized_messages,
                "max_tokens": self.max_tokens,
            }

            if json_mode:
                request_kwargs["response_format"] = {"type": "json_object"}

            if tools:
                request_kwargs["tools"] = tools
                request_kwargs["tool_choice"] = "auto"

            response: ChatCompletion = await self.client.chat.completions.create(**request_kwargs)
            message: ChatCompletionMessage = response.choices[0].message

            working_messages.append(message.model_dump(exclude_none=True))

            logger.trace(f"🧠 [AI][OpenAI] 模型回复: {message}")

            # --- 分支 1: 模型请求调用工具 ---
            if message.tool_calls:
                tool_calls_list = cast(
                    List[ChatCompletionMessageToolCall],
                    message.tool_calls,
                )

                logger.trace(f"🧠 [AI][OpenAI] 模型请求调用工具: {tool_calls_list}")

                for tool_call in tool_calls_list:
                    func_name = tool_call.function.name
                    args_str = tool_call.function.arguments
                    call_id = tool_call.id

                    logger.debug(f"🧠 [AI][OpenAI] ID {call_id} 调用工具: {func_name}, 参数: {args_str}")

                    function_response = "Error: Function not found"

                    tools_list = get_registered_tools()

                    if func_name in tools_list:
                        try:
                            tool_def = tools_list[func_name]
                            # 1. 解析参数（清理可能的 markdown 标记）
                            clean_args_str = re.sub(r"^```(?:json)?\n?|```$", "", args_str.strip(), flags=re.MULTILINE)
                            try:
                                func_args = json.loads(clean_args_str)
                            except json.JSONDecodeError as e:
                                json_error_count += 1
                                # 巧妙利用 Tool Response 让 AI 知道自己错了并重试
                                function_response = (
                                    f"JSON解析错误: {str(e)}。请不要输出任何markdown标记，仅输出纯JSON对象。"
                                )
                                working_messages.append(
                                    {
                                        "tool_call_id": call_id,
                                        "role": "tool",
                                        "name": func_name,
                                        "content": function_response,
                                    }
                                )
                                # JSON 解析错误超过 2 次，停止重试
                                if json_error_count >= 2:
                                    logger.warning("🧠 [AI][OpenAI] JSON 解析错误次数超限，停止工具调用")
                                    continue
                                continue
                            # 2. 查找函数
                            func_obj = tool_def["func"]

                            logger.debug(f"🧠 [AI][OpenAI] ID {call_id} 即将执行工具: {func_name}, 参数: {func_args}")

                            # 3. 检查确认函数（如果存在）
                            check_func = tool_def.get("check_func")
                            check_kwargs = tool_def.get("check_kwargs", {})

                            logger.debug(
                                f"🧠 [AI][OpenAI] ID {call_id} 检查工具前置条件: {check_func}, 参数: {check_kwargs}"
                            )

                            if check_func is not None and bot is not None and ev is not None:
                                # 检查 check_func 的签名，根据参数名和类型注解注入依赖
                                sig = inspect.signature(check_func)
                                check_args = {}

                                for param_name, param in sig.parameters.items():
                                    # 根据参数名注入
                                    if param_name == "bot":
                                        check_args[param_name] = bot
                                    elif param_name == "ev" or param_name == "event":
                                        check_args[param_name] = ev
                                    # 根据类型注解注入
                                    elif param.annotation != inspect.Parameter.empty:
                                        # 获取类型注解的字符串表示
                                        ann = param.annotation
                                        # 处理 Optional[Type] 或 Union[Type, None]
                                        origin = getattr(ann, "__origin__", None)
                                        if origin is not None:
                                            # 获取 Optional 内部的真实类型
                                            args = getattr(ann, "__args__", ())
                                            if args and len(args) > 0:
                                                ann = args[0]

                                        ann_str = str(ann)
                                        if "Bot" in ann_str:
                                            check_args[param_name] = bot
                                        elif "Event" in ann_str:
                                            check_args[param_name] = ev

                                check_args.update(check_kwargs)

                                # 执行确认函数
                                if asyncio.iscoroutinefunction(check_func):
                                    check_passed: Union[bool, Tuple[bool, str]] = await check_func(**check_args)
                                else:
                                    check_passed = check_func(**check_args)

                                logger.debug(f"🧠 [AI][OpenAI] ID {call_id} 检查结果: {check_passed}")

                                if isinstance(check_passed, tuple):
                                    check_passed, reason = check_passed
                                    await bot.send(reason)
                                else:
                                    check_passed = bool(check_passed)
                                    reason = "错误: 权限检查未通过"

                                if not check_passed:
                                    function_response = f"{reason}"
                                    # 跳过函数执行，继续下一个工具调用
                                    working_messages.append(
                                        {
                                            "tool_call_id": call_id,
                                            "role": "tool",
                                            "name": func_name,
                                            "content": function_response,
                                        }
                                    )
                                    continue

                            # 5. 执行函数 (兼容 async 和 sync) - 添加依赖注入
                            inject_args = func_args.copy()
                            sig = inspect.signature(func_obj)
                            for param_name, param in sig.parameters.items():
                                if param_name not in inject_args:  # 不覆盖大模型传的参数
                                    if param_name in ("bot",):
                                        inject_args[param_name] = bot
                                    elif param_name in ("ev", "event"):
                                        inject_args[param_name] = ev

                            if asyncio.iscoroutinefunction(func_obj):
                                result = await func_obj(**inject_args)
                            else:
                                result = func_obj(**inject_args)

                            # 6. 序列化结果
                            if isinstance(result, Message):
                                function_response = "生成内容成功, 已经发送了相关消息！"
                                tools_reply.append(result)
                            elif isinstance(result, str):
                                function_response = result
                                tools_reply.append(MessageSegment.text(function_response))
                            elif isinstance(result, dict):
                                function_response = json.dumps(result, ensure_ascii=False)
                            elif isinstance(result, bytes):
                                function_response = f"生成了某项资源, 资源ID: {RM.register(result)}"
                                tools_reply.append(MessageSegment.image(result))
                            elif isinstance(result, list):
                                function_response = json.dumps(result, ensure_ascii=False)
                            elif isinstance(result, Image.Image):
                                function_response = f"生成了一张图片, 图片ID: {RM.register(result)}"
                                tools_reply.append(MessageSegment.image(result))
                            else:
                                function_response = str(result)

                        except Exception as e:
                            function_response = f"Error executing {func_name}: {str(e)}"

                    # 将工具结果作为 tool message 存入临时消息列表（不存入永久历史）
                    working_messages.append(
                        {"tool_call_id": call_id, "role": "tool", "name": func_name, "content": function_response}
                    )

                request_kwargs["messages"] = working_messages
                continue  # 继续下一轮循环，让 AI 读取工具结果并生成最终回复

            else:
                content = message.content or ""

                # 4. 保存历史记录 - 将真实的（无RAG）User 消息与本轮 AI 回复合并
                new_history_messages = [history_user_msg] + working_messages[initial_working_length:]

                # 对 user 消息进行图片降维存储（防止图片把 Token 撑爆）
                for msg in new_history_messages:
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        msg_content = msg.get("content")
                        if isinstance(msg_content, list):
                            # 对 content_payload 进行降维
                            history_payload = []
                            for item in msg_content:
                                if isinstance(item, dict):
                                    if item.get("type") == "text":
                                        history_payload.append(item)
                                    elif item.get("type") == "image_url":
                                        history_payload.append({"type": "text", "text": "[用户上传了一张图片]"})
                            msg["content"] = history_payload

                # 将本轮新增的消息追加到 self.history 中，并更新 token 计数
                for msg in new_history_messages:
                    self.history.append(msg)
                    self.current_token_count += estimate_tokens(msg, self.tokenizer)

                logger.debug(f"🧠 [AI][OpenAI] 历史记录已更新，新增 {len(new_history_messages)} 条消息")

                # 2. 修改 history 裁剪逻辑，安全截断避免切断工具链
                self._safe_truncate_history()

                # --- 返回结果处理 ---
                if json_mode:
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        logger.error(f"JSON 解析失败: {content}")
                        return {"error": "JSON解析失败", "raw": content}

                if content:
                    tools_reply.append(MessageSegment.text(content))

                if not tools_reply:
                    # 如果工具没产生可见输出，大模型也没说话的保底措施
                    return [MessageSegment.text("执行完毕。")]

                return tools_reply

    def reset_session(self, system_prompt: Optional[str] = None):
        """重置会话，可选择性更新基础人设"""
        self.history = []
        self.current_token_count = 0
        # 重新初始化 tokenizer，防止缓存问题
        try:
            self.tokenizer = tiktoken.encoding_for_model(self.model)
        except KeyError:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
        if system_prompt:
            self.base_persona = system_prompt


# 工厂函数，对外提供简单的入口
def create_ai_session(
    system_prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> AsyncOpenAISession:
    api_keys: List[str] = openai_config.get_config("api_key").data
    if not api_keys or len(api_keys[0]) <= 6:
        raise ValueError("未配置OpenAI API key 或 配置错误, 请检查配置文件")
    api_key = random.choice(api_keys)

    if model is None:
        model = openai_config.get_config("model").data
        if not model:
            raise ValueError("未配置OpenAI model 或 配置错误, 请检查配置文件")

    return AsyncOpenAISession(
        api_key=api_key,
        system_prompt=system_prompt,
        model=model,
        base_url=openai_config.get_config("base_url").data,
    )
