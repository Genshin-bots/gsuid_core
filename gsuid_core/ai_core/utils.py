import inspect
from typing import Any, List, Callable, get_type_hints

import msgspec


class FunctionDef(msgspec.Struct):
    name: str
    description: str
    parameters: dict


class ToolDef(msgspec.Struct):
    type: str
    function: FunctionDef


def function_to_schema(func: Callable) -> dict:
    """
    使用 msgspec 高速生成函数的 JSON Schema。
    支持 typing.Annotated[type, msgspec.Meta(description="...")]
    """
    func_name = func.__name__
    doc = inspect.getdoc(func) or ""

    try:
        type_hints = get_type_hints(func, include_extras=True)
    except Exception:
        type_hints = {}

    sig = inspect.signature(func)

    annotations = {}
    defaults = {}
    required_params = []

    for name, param in sig.parameters.items():
        if name == "self":
            continue  # 忽略 self

        # 获取类型，默认为 Any
        param_type = type_hints.get(name, Any)
        annotations[name] = param_type

        # 处理默认值，决定是否必填
        if param.default is inspect.Parameter.empty:
            required_params.append(name)
        else:
            defaults[name] = param.default

    DynamicParams = type(f"{func_name}_Params", (msgspec.Struct,), {})
    DynamicParams.__annotations__ = annotations

    # 生成 Schema
    # msgspec.json.schema 会返回一个 (schema, definitions) 的元组或直接返回 dict
    try:
        schema = msgspec.json.schema(DynamicParams)
        schema["required"] = required_params

        # 如果没有参数，msgspec 可能会省略 properties
        if "properties" not in schema:
            schema["properties"] = {}

    except Exception as e:
        print(f"Schema generation failed for {func_name}: {e}")
        schema = {"type": "object", "properties": {}}

    tool = {
        "type": "function",
        "function": {
            "name": func_name,
            "description": doc,  # 函数的 docstring 作为主描述
            "parameters": schema,
        },
    }

    return tool


def generate_tools_schema(funcs: List[Callable]) -> List[dict]:
    """批量转换"""
    return [function_to_schema(f) for f in funcs]
