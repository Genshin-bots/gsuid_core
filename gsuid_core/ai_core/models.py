from typing import TYPE_CHECKING, Dict, List, Optional, TypedDict
from dataclasses import dataclass

from gsuid_core.bot import Bot
from gsuid_core.models import Event

if TYPE_CHECKING:
    from pydantic_ai.tools import Tool


@dataclass
class ToolContext:
    """工具执行上下文"""

    bot: Optional[Bot] = None
    ev: Optional[Event] = None


class KnowledgeBase(TypedDict):
    """知识点基类"""

    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # 知识来源: "plugin" 表示来自插件注册, "manual" 表示手动添加


class KnowledgePoint(KnowledgeBase):
    """知识点类型"""

    _hash: str


class ManualKnowledgeBase(TypedDict):
    """手动添加的知识库类型 - 不会在启动时自动同步"""

    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: str  # 固定为 "manual"


class ImageEntity(TypedDict):
    """图片实体类型 - 用于RAG图片检索

    插件作者可以通过 ai_image() 注册图片，
    系统会根据描述文本(tags + content)进行向量化存储，
    支持通过语义搜索找到匹配的图片路径。
    """

    id: str  # 唯一标识符
    plugin: str  # 插件名称
    path: str  # 图片文件路径
    tags: List[str]  # 图片标签，用于描述图片内容
    content: str  # 详细描述文本
    source: str  # 来源: "plugin" 表示来自插件注册
    _hash: Optional[str]  # 内容哈希，用于增量更新检测


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


class ToolBase:
    """RAG工具类型 - 包含工具对象和元数据"""

    name: str
    description: str
    plugin: str  # 插件名称，core表示核心模块
    tool: "Tool[ToolContext]"

    def __init__(self, name: str, description: str, plugin: str, tool: "Tool[ToolContext]"):
        self.name = name
        self.description = description
        self.plugin = plugin
        self.tool = tool
