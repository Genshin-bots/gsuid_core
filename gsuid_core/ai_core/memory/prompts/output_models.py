"""记忆系统 LLM 结构化输出模型

利用 pydantic_ai 的 output_type 特性，强制 LLM 返回符合 Pydantic 模型的结构化 JSON，
替代手动 re 解析 + json.loads 的脆弱模式。
"""

from typing import Optional

from pydantic import Field, BaseModel

# ====== Entity 提取输出模型 ======


class ExtractedEntity(BaseModel):
    """LLM 提取的单个 Entity"""

    name: str = Field(description="实体名称")
    summary: str = Field(default="", description="实体摘要，不超过50字")
    tag: list[str] = Field(default_factory=list, description="标签列表，最多5个")
    is_speaker: bool = Field(default=False, description="是否为发言者实体")
    user_id: Optional[str] = Field(default=None, description="发言者的 user_id")
    scope_hint: Optional[str] = Field(default=None, description="Scope 提示，如 user_global")


class ExtractedEdge(BaseModel):
    """LLM 提取的单个 Edge"""

    source: str = Field(description="主语实体名")
    target: str = Field(description="宾语实体名")
    fact: str = Field(description="完整事实描述句")
    scope_hint: Optional[str] = Field(default=None, description="Scope 提示")
    user_id: Optional[str] = Field(default=None, description="关联的 user_id")


class ExtractionOutput(BaseModel):
    """Entity & Edge 提取的完整输出"""

    entities: list[ExtractedEntity] = Field(default_factory=list, description="提取的实体列表")
    edges: list[ExtractedEdge] = Field(default_factory=list, description="提取的关系列表")


# ====== Category 分类输出模型 ======


class CategoryAssignment(BaseModel):
    """单个分类分配结果"""

    category: str = Field(description="类目名称")
    summary: str = Field(default="", description="类目摘要，不超过80字")
    members: list[str] = Field(description="属于此类的节点名称列表")


class CategorizationOutput(BaseModel):
    """分类输出的完整结果"""

    assignments: list[CategoryAssignment] = Field(description="分类分配列表")


# ====== System-2 节点选择输出模型 ======


class NodeSelection(BaseModel):
    """单个节点选择结果"""

    uuid: str = Field(description="节点的 UUID")
    get_all_children: bool = Field(default=False, description="是否获取所有子节点（快捷方式）")


class NodeSelectionOutput(BaseModel):
    """节点选择的完整结果"""

    selections: list[NodeSelection] = Field(default_factory=list, description="选择的节点列表")
