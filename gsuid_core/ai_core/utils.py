import inspect
from typing import Any, List, Callable, get_type_hints

import msgspec

from .models import ToolDef


def _type_to_json_schema_type(param_type: Any) -> dict:
    """将 Python 类型转换为 JSON Schema 类型定义"""
    import typing
    from typing import get_args, get_origin

    # 处理 Annotated 类型
    origin = get_origin(param_type)
    args = get_args(param_type)

    if origin is not None:
        # 处理 Annotated[type, Meta(...)]
        if hasattr(origin, "__origin__") and origin.__origin__ is typing.Annotated or origin is typing.Annotated:
            if args:
                inner_type = args[0]
                # 检查是否有 Meta 信息
                description = None
                for arg in args[1:]:
                    if hasattr(arg, "description"):
                        description = arg.description
                result = _type_to_json_schema_type(inner_type)
                if description:
                    result["description"] = description
                return result

        # 处理 Optional[T] = Union[T, None]
        if origin is typing.Union or str(origin) == "typing.Union":
            non_none_types = [arg for arg in args if arg is not type(None)]
            if len(non_none_types) == 1:
                result = _type_to_json_schema_type(non_none_types[0])
                if type(None) in args:
                    # Optional 字段
                    pass
                return result

    # 基本类型映射
    type_map = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        bytes: {"type": "string", "format": "binary"},
        list: {"type": "array"},
        dict: {"type": "object"},
        Any: {},
    }

    if param_type in type_map:
        return type_map[param_type]

    # 处理 List[T]
    if origin is list or origin is typing.List:
        if args:
            item_schema = _type_to_json_schema_type(args[0])
            return {"type": "array", "items": item_schema}
        return {"type": "array"}

    # 默认返回空对象
    return {}


def _build_simple_schema(annotations: dict, defaults: dict, required_params: list) -> dict:
    """当 msgspec 失败时，使用简单的手动 schema 构建"""
    properties = {}

    for name, param_type in annotations.items():
        schema_entry = _type_to_json_schema_type(param_type)

        # 添加默认值
        if name in defaults:
            default_val = defaults[name]
            # 处理特殊类型
            if default_val is None:
                schema_entry["default"] = None
            elif isinstance(default_val, (str, int, float, bool)):
                schema_entry["default"] = default_val

        properties[name] = schema_entry

    return {
        "type": "object",
        "properties": properties,
        "required": required_params,
    }


def function_to_schema(func: Callable) -> ToolDef:
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

    # 使用 exec 动态创建带注解的 Struct 类
    # 这样 msgspec 能正确识别 Annotated 类型和 Meta 信息
    class_body = []
    for name, param_type in annotations.items():
        if name in defaults:
            default_val = defaults[name]
            if isinstance(default_val, str):
                default_str = f"{repr(default_val)}"
            else:
                default_str = repr(default_val)
            class_body.append(f"    {name}: {name}_type = {default_str}")
        else:
            class_body.append(f"    {name}: {name}_type")

    # 准备 exec 的局部变量，包含所有类型
    exec_locals = {"Struct": msgspec.Struct}
    for name, param_type in annotations.items():
        exec_locals[f"{name}_type"] = param_type

    class_code = f"""
class {func_name}_Params(Struct):
{chr(10).join(class_body) if class_body else "    pass"}
"""

    try:
        exec(class_code, {"Struct": msgspec.Struct, **exec_locals}, exec_locals)
        DynamicParams = exec_locals[f"{func_name}_Params"]
    except Exception as e:
        print(f"Dynamic class creation failed for {func_name}: {e}")
        # 回退到简单的 schema 生成
        schema = _build_simple_schema(annotations, defaults, required_params)
        return {
            "type": "function",
            "function": {
                "name": func_name,
                "description": doc,
                "parameters": schema,
            },
        }

    # 生成 Schema
    try:
        schema = msgspec.json.schema(DynamicParams)

        # msgspec 的 schema 可能包含 $ref，我们需要展开它
        if "$ref" in schema:
            defs = schema.get("$defs", {})
            ref_key = schema["$ref"].split("/")[-1]
            if ref_key in defs:
                schema = defs[ref_key]

        # 确保有 properties 字段
        if "properties" not in schema:
            schema["properties"] = {}

        # 设置 required 字段
        schema["required"] = required_params

        # 移除 msgspec 特有的字段
        schema.pop("title", None)
        schema.pop("$defs", None)
        schema.pop("$ref", None)

    except Exception as e:
        print(f"Schema generation failed for {func_name}: {e}")
        schema = {"type": "object", "properties": {}}

    tool: ToolDef = {
        "type": "function",
        "function": {
            "name": func_name,
            "description": doc,  # 函数的 docstring 作为主描述
            "parameters": schema,
        },
    }

    return tool


def generate_tools_schema(funcs: List[Callable]) -> List[ToolDef]:
    """批量转换"""
    return [function_to_schema(f) for f in funcs]
