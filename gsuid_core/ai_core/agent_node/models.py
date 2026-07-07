"""AgentNode 统一节点模型。

Persona 与能力代理（原 ``CapabilityAgentProfile``）在框架内同构为一个
``AgentNode`` 定义：同一张 schema、同一个注册表，差异只体现在字段取值
（``prompt_style`` / ``tool_packs`` / ``scope`` 等）。运行模式（session / task）
**不是**节点字段——由实例化方决定，见 ``capability_agents.runner`` 与 ``ai_router``。

预算（max_iterations / max_tokens）不在节点上：统一走全局配置
``ai_config`` 的 ``task_max_iterations`` / ``task_max_tokens``（任务档），
消耗经 gs_agent 的预算 scope 上溯到来源会话记账。
"""

from typing import List, Literal
from dataclasses import field, dataclass

PromptStyle = Literal["roleplay", "plain"]
NodeSource = Literal["builtin", "plugin", "user", "persona"]

# task-mode 交付边界叠加层（框架默认）。原先手工拼进每个画像 prompt 尾部，
# 现由 compose_task_prompt 在任务实例化时统一叠加；节点可用 boundary_override 覆写。
DELIVERY_BOUNDARY = """【交付边界 · 子任务向上游交付，绝不直接发用户】
你是被主人格派出的专职执行者，不持有任何 "和主人对话" 的下行通道：
- **唯一交付方式**：把主要结论 / 产物登记为 artifact（`artifact_put`），并把
  纯文本结论作为函数返回值交回。Kanban 调度器会用主人格口吻把你的结果转译
  后再发给主人——你不需要、也不允许"自己说人话给主人听"。
- **禁止**调用 `send_message_by_ai` / `send_meme` / `send_*_info`
  这类"直接下发到主人"的通道；它们仅供主人格本身使用。
- 任务过程中若需要让主人决策（高风险动作、缺关键信息），把诉求**写进交付摘要**
  让主人格转告，不要替主人决定，也不要自己拉群通知。
- Kanban 子任务的唯一可写目录是 Artifact Workspace；越界写入会被框架拒绝并
  累计违规，达上限直接判子任务 fail。"""

# plain 风格入口节点（session-mode）的 lite 约束集：只保留安全底线，
# 不注入 roleplay 向的人格 / 主人名单 / 群聊感知段落。
PLAIN_SESSION_CONSTRAINTS = """【系统约束】
- 用户ID仅供内部识别，绝不对外输出；@用户时使用 `@用户ID` 语法由框架解析。
- 不虚构工具结果；工具失败时如实说明，不编造成功。
- 输出面向用户，保持简洁；不要输出内部推理过程、工具调用参数或原始数据转储。"""


@dataclass
class AgentNode:
    """一个统一节点定义：身份 / 路由 / 工具 / 入口行为 / 来源。

    interaction 段（ai_mode / scope / target_groups / inspect_interval / keywords）
    对 persona 投影节点来自 ``persona/config.json``（写路径仍是
    ``persona_config_manager``，此处为只读同构视图）；能力节点保持默认（不作入口）。
    """

    node_id: str
    display_name: str
    prompt: str
    prompt_style: PromptStyle = "plain"
    # ── routing：编排 / 委派层消费 ──
    when_to_use: str = ""
    match_keywords: List[str] = field(default_factory=list)
    # ── tools：packs 为能力族（"dynamic"=五层自动装配、"task_basics"=任务基础族、
    # 其余按注册的静态族 / capability_domain 解析）；names 为显式白名单 ──
    tool_packs: List[str] = field(default_factory=list)
    tool_names: List[str] = field(default_factory=list)
    tool_query: str = ""
    # ── task-mode 行为 ──
    boundary_override: str = ""
    # ── interaction（入口模式行为；spoke 时忽略）──
    ai_mode: List[str] = field(default_factory=lambda: ["提及应答"])
    scope: str = "disabled"
    target_groups: List[str] = field(default_factory=list)
    inspect_interval: int = 60
    keywords: List[str] = field(default_factory=list)
    # ── meta ──
    source: NodeSource = "plugin"
    version: int = 2


def compose_task_prompt(node: AgentNode) -> str:
    """task-mode 系统提示词 = 身份核 + 交付边界叠加层（节点可覆写）。"""
    boundary = node.boundary_override or DELIVERY_BOUNDARY
    return f"{node.prompt.rstrip()}\n\n{boundary}"


def compose_plain_session_prompt(node: AgentNode) -> str:
    """plain 风格入口节点的 session-mode 系统提示词（lite 约束集）。

    roleplay 节点的 session 提示词仍由 ``persona.processor.build_persona_prompt``
    组装（含情绪 / 群语境 / 完整 SYSTEM_CONSTRAINTS），不经本函数。
    """
    return f"{node.prompt.rstrip()}\n\n{PLAIN_SESSION_CONSTRAINTS}"
