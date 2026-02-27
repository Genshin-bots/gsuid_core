from typing import Any, Dict, List, Tuple, Union, Callable, Optional, TypedDict

"""
if TYPE_CHECKING:
    from qdrant_client.models import ExtendedPointId
"""


class KnowledgeBase(TypedDict):
    """知识点基类"""

    id: str
    plugin: str
    type: str
    category: str
    title: str
    content: str
    tags: List[str]


class KnowledgePoint(KnowledgeBase):
    """知识点类型"""

    _hash: str


class KnowledgeHash(TypedDict):
    """知识点哈希值类型"""

    id: str
    hash: str


class ModelInfo(TypedDict):
    """模型信息类型"""

    title: str
    content: str
    tags: List[str]


class FunctionDef(TypedDict):
    name: str
    description: str
    parameters: Dict


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
