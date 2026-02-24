import io
import json
import base64
import random
import asyncio
import inspect
import mimetypes
from typing import List, Tuple, Union, Optional, cast
from pathlib import Path

import aiofiles
from bot import Bot
from PIL import Image
from models import Event
from openai import AsyncOpenAI
from ai_core.models import ToolDef
from openai.types.chat import ChatCompletion, ChatCompletionMessage, ChatCompletionMessageToolCall

from gsuid_core.logger import logger
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.ai_core.register import get_registered_tools
from gsuid_core.ai_core.ai_config import openai_config
from gsuid_core.utils.resource_manager import RM

# 定义类型别名，方便阅读
ImageInput = Union[str, Path, bytes, io.BytesIO, Image.Image]
FileInput = Union[str, Path]


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
        self.history = []
        if system_prompt:
            self.history.append({"role": "system", "content": system_prompt})

    async def _process_image(self, image: ImageInput) -> str:
        """
        内部函数：将各种类型的图片输入转换为 Base64 字符串
        """
        img_byte_arr = io.BytesIO()
        mime_type = "image/png"  # 默认

        try:
            # 1. 处理 PIL Image 对象
            if isinstance(image, Image.Image):
                # 转换 RGBA 到 RGB (如果存为 JPEG)
                if image.mode in ("RGBA", "P"):
                    image = image.convert("RGB")
                image.save(img_byte_arr, format="PNG")

            # 2. 处理 路径 (str 或 Path)
            elif isinstance(image, (str, Path)):
                path = Path(image)
                if not path.exists():
                    raise FileNotFoundError(f"Image not found: {path}")
                mime_type = mimetypes.guess_type(path)[0] or "image/png"
                async with aiofiles.open(path, "rb") as f:
                    img_byte_arr.write(await f.read())

            # 3. 处理 Bytes / BytesIO
            elif isinstance(image, (bytes, io.BytesIO)):
                data = image if isinstance(image, bytes) else image.getvalue()
                img_byte_arr.write(data)
                # 尝试用 PIL 打开一下以确认格式（可选，为了稳健性）
                try:
                    Image.open(io.BytesIO(data)).verify()
                except Exception:
                    pass

            else:
                raise ValueError(f"Unsupported image type: {type(image)}")

            base64_data = base64.b64encode(img_byte_arr.getvalue()).decode("utf-8")
            return f"data:{mime_type};base64,{base64_data}"

        except Exception as e:
            logger.error(f"Error processing image: {e}")
            return ""

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
        images: Optional[Union[ImageInput, List[ImageInput]]] = None,
        files: Optional[Union[FileInput, List[FileInput]]] = None,
        tools: Optional[List[ToolDef]] = None,
        json_mode: bool = False,
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
    ) -> List[Message]:
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

        if images:
            if not isinstance(images, list):
                images = [images]

            for img_input in images:
                base64_url = await self._process_image(img_input)

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

        # 2. 更新用户历史
        self.history.append({"role": "user", "content": content_payload})

        # 3. 准备请求参数
        request_kwargs = {
            "model": self.model,
            "messages": self.history,
        }

        if json_mode:
            request_kwargs["response_format"] = {"type": "json_object"}

        if tools:
            request_kwargs["tools"] = tools
            request_kwargs["tool_choice"] = "auto"

        tools_reply: List[Message] = []

        while True:
            response: ChatCompletion = await self.client.chat.completions.create(**request_kwargs)
            message: ChatCompletionMessage = response.choices[0].message

            self.history.append(message)

            logger.trace(f"🧠 [AI][OpenAI] 模型回复: {message}")

            # --- 分支 1: 模型请求调用工具 ---
            if message.tool_calls:
                tool_calls_list = cast(
                    List[ChatCompletionMessageToolCall],
                    message.tool_calls,
                )

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
                            # 1. 解析参数
                            func_args = json.loads(args_str)
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
                                    self.history.append(
                                        {
                                            "tool_call_id": call_id,
                                            "role": "tool",
                                            "name": func_name,
                                            "content": function_response,
                                        }
                                    )
                                    continue

                            # 5. 执行函数 (兼容 async 和 sync)
                            if asyncio.iscoroutinefunction(func_obj):
                                result = await func_obj(**func_args)
                            else:
                                result = func_obj(**func_args)

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

                    # 将工具结果作为 tool message 存入历史
                    self.history.append(
                        {"tool_call_id": call_id, "role": "tool", "name": func_name, "content": function_response}
                    )

                request_kwargs["messages"] = self.history
                continue  # 继续下一轮循环，让 AI 读取工具结果并生成最终回复

            else:
                content = message.content

                if not content:
                    raise ValueError("Empty content from model.")

                if json_mode:
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        # 容错：如果 JSON 解析失败，返回原始文本或报错
                        logger.error(f"JSON 解析失败: {content}")
                        return [MessageSegment.text("JSON 解析失败")]

                tools_reply.append(MessageSegment.text(content))
                return tools_reply

    def reset_session(self, system_prompt: Optional[str] = None):
        """重置会话，可选择性更新 system prompt"""
        self.history = []
        if system_prompt:
            self.history.append({"role": "system", "content": system_prompt})


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
