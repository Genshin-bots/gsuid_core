from typing import Any, Dict, List, Callable, TypedDict


class ToolSchema(TypedDict):
    name: str
    desc: str
    params: Dict[str, Any]
    schema: Dict[str, Any]
    func: Callable


class EntitySchema(TypedDict):
    name: str
    aliases: List[str]
    domain: str
    type: str
