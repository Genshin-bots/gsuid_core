from typing import Any, Dict, List, Tuple, Union, Callable, Optional, TypedDict


class FunctionDef(TypedDict):
    name: str
    description: str
    parameters: dict


class ToolDef(TypedDict):
    type: str
    function: FunctionDef


class ToolSchema(TypedDict):
    name: str
    desc: str
    params: Dict[str, Any]
    schema: ToolDef
    func: Callable
    check_func: Optional[Callable[..., Union[Tuple[bool, str], bool]]]
    check_kwargs: Dict[str, Any]


class EntitySchema(TypedDict):
    name: str
    aliases: List[str]
    domain: str
    type: str
