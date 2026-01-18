import io
import json
import base64
import random
import asyncio
import mimetypes
from typing import Dict, List, Union, Callable, Optional, cast
from pathlib import Path

import aiofiles
from PIL import Image
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage, ChatCompletionMessageToolCall

from gsuid_core.logger import logger
from gsuid_core.ai_core.ai_config import openai_config

# 定义类型别名，方便阅读
ImageInput = Union[str, Path, bytes, io.BytesIO, Image.Image]
FileInput = Union[str, Path]


class AsyncOpenAISession:
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        system_prompt: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        functions: Optional[Dict[str, Callable]] = None,
    ):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.history = []
        if system_prompt:
            self.history.append({"role": "system", "content": system_prompt})

        # 保存工具定义和函数映射
        self.tools = tools
        self.function_map = functions or {}

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
        json_mode: bool = False,
    ) -> Union[str, Dict]:
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
            return "Error: Empty input (no text or images provided)."

        # 2. 更新用户历史
        self.history.append({"role": "user", "content": content_payload})

        # 3. 准备请求参数
        request_kwargs = {
            "model": self.model,
            "messages": self.history,
        }

        if json_mode:
            request_kwargs["response_format"] = {"type": "json_object"}

        if self.tools:
            request_kwargs["tools"] = self.tools
            request_kwargs["tool_choice"] = "auto"

        while True:
            response: ChatCompletion = await self.client.chat.completions.create(**request_kwargs)
            message: ChatCompletionMessage = response.choices[0].message

            self.history.append(message)

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

                    function_response = "Error: Function not found"

                    if func_name in self.function_map:
                        try:
                            # 1. 解析参数
                            func_args = json.loads(args_str)
                            # 2. 查找函数
                            func_obj = self.function_map[func_name]

                            # 3. 执行函数 (兼容 async 和 sync)
                            if asyncio.iscoroutinefunction(func_obj):
                                result = await func_obj(**func_args)
                            else:
                                result = func_obj(**func_args)

                            # 4. 序列化结果
                            function_response = json.dumps(result, ensure_ascii=False)

                        except Exception as e:
                            function_response = f"Error executing {func_name}: {str(e)}"

                    # 将工具结果作为 tool message 存入历史
                    self.history.append(
                        {"tool_call_id": call_id, "role": "tool", "name": func_name, "content": function_response}
                    )

                # 重要：更新 request_kwargs 里的 messages，因为 self.history 已经变了
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
                        return content

                return content

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
    if not api_keys:
        raise ValueError("OpenAI API key is not configured.")
    api_key = random.choice(api_keys)

    if model is None:
        model = openai_config.get_config("model").data
        if not model:
            raise ValueError("OpenAI model is not configured.")

    return AsyncOpenAISession(
        api_key=api_key,
        system_prompt=system_prompt,
        model=model,
    )
