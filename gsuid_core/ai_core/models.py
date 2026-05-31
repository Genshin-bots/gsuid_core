from typing import TYPE_CHECKING, Any, Dict, List, Optional, TypedDict, NotRequired
from dataclasses import field, dataclass

from gsuid_core.bot import Bot
from gsuid_core.models import Event

if TYPE_CHECKING:
    from pydantic_ai.tools import Tool


@dataclass
class ToolContext:
    """工具执行上下文

    ``parent_session_id``：当前 Agent 的 ``session_id``。工具内可借此找到调用
    自己的那一个 ``GsCoreAIAgent``，再调 ``append_proactive_assistant_turn``
    把"工具主动发出去的话"同步到该主 session 的 pydantic_ai 历史与 logger。
    见 plans/proactive_message_session_unification_20260529.md §8.1
    "send_message_by_ai 主 session 同步" 一节。
    """

    bot: Optional[Bot] = None
    ev: Optional[Event] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    parent_session_id: Optional[str] = None


class KnowledgeBase(TypedDict):
    """知识点基类"""

    id: str
    plugin: str
    title: str
    content: str
    tags: List[str]
    source: NotRequired[str]  # 知识来源: 由框架自动设置, "plugin" 或 "manual"


class KnowledgePoint(KnowledgeBase):
    """知识点类型"""

    _hash: NotRequired[str]  # 内容哈希: 由框架自动计算, 无需手动提供


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


# ─────────────────────────────────────────────
# AI 会话日志序列化结构
#
# 这三个 TypedDict 是 ``AISessionLogger`` 落盘格式的唯一类型来源
# （对应 session_logger.py 的 ``_add_entry`` / ``link_agent`` / ``_build_data``）。
# webconsole 的日志 API 读取磁盘 JSON 与内存 logger 时复用它们，
# 使全部字段可追踪到实时类型，无需 getattr / dict.get 兜底。
# ─────────────────────────────────────────────


class SessionLogEntry(TypedDict):
    """单条会话日志条目（``AISessionLogger._add_entry`` 落盘结构）"""

    type: str  # 取值受 SESSION_ENTRY_TYPES 白名单约束
    timestamp: float
    data: Dict[str, Any]  # 各 entry 类型特定的 payload


class LinkedAgentRecord(TypedDict):
    """关联 Agent 记录（``AISessionLogger.link_agent`` 落盘结构）"""

    agent_type: str  # sub_agent / peer_agent / parent_agent / proactive_generator
    session_id: str
    session_uuid: str
    persona_name: Optional[str]
    create_by: Optional[str]
    log_file: Optional[str]
    linked_at: float


class SessionLogFileData(TypedDict):
    """AI 会话日志文件的磁盘 JSON 结构（``AISessionLogger._build_data`` 唯一来源）"""

    session_id: str
    session_uuid: str
    persona_name: Optional[str]
    create_by: str
    is_subagent: bool
    created_at: float
    updated_at: float
    ended_at: Optional[float]
    entry_count: int
    entries: List[SessionLogEntry]
    linked_agents: List[LinkedAgentRecord]
    linked_agent_count: int


class ToolBase:
    """RAG工具类型 - 包含工具对象和元数据"""

    name: str
    description: str
    plugin: str  # 插件名称，core表示核心模块
    tool: "Tool[ToolContext]"
    check_func: Any  # 可选的权限检查函数
    context_tags: List[str]  # 语境标签，用于语境工具池自动加载
    capability_domain: Optional[str]  # C3-d 能力域，用于聚合成自然语言能力清单

    def __init__(
        self,
        name: str,
        description: str,
        plugin: str,
        tool: "Tool[ToolContext]",
        check_func: Any = None,
        context_tags: Optional[List[str]] = None,
        capability_domain: Optional[str] = None,
    ):
        self.name = name
        self.description = description
        self.plugin = plugin
        self.tool = tool
        self.check_func = check_func
        self.context_tags = context_tags or []
        self.capability_domain = capability_domain
