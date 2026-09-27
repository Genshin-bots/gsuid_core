"""人格「启用工具」作用域：决定哪些插件的工具能进入向量检索池。

与 ``capability_agents`` 同构的 ``*`` / ``!id`` 名单语法，落在 persona config.json 的
``enabled_tools`` 键。默认 ``["*"]`` = 全选，即所有插件工具都进检索池。

三层工具来源互不重叠，本模块是它们的唯一判定口：

- **常驻直装**（``MAIN_AGENT_CORE_TOOLS`` + persona ``tool_names``）：不经检索，
  每轮必在。框架能力，不受 ``enabled_tools`` 收放——否则人格连消息都发不出去。
- **向量检索**：受 ``enabled_tools`` 管辖，插件被排除则其工具召不回来。
- **能力族静态挂载**：人格侧已固定 ``dynamic``（五层自动装配恒开，不再开放配置），
  仅能力代理节点仍可显式声明静态族；同样不受 ``enabled_tools`` 收放。

``core`` / 空插件名是框架自身（``send_message_by_ai`` 等），永远视为启用。
"""

from typing import Dict, List, Tuple, TypeVar, Optional, Protocol, Sequence, FrozenSet, TypedDict

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# 视为「框架自身」的插件名：不受 enabled_tools 收放
NEUTRAL_PLUGINS: FrozenSet[str] = frozenset({"", "core", "unknown", "gsuid_core"})

ENABLED_TOOLS_KEY = "enabled_tools"


class _Named(Protocol):
    """只要求有 ``name`` 的工具对象（``Tool`` 与测试替身都满足）。"""

    name: str


_NamedT = TypeVar("_NamedT", bound=_Named)


class ToolCatalogEntry(TypedDict):
    """webconsole 工具目录的单项。"""

    name: str
    description: str
    category: str
    capability_domain: str
    always_mounted: bool


def parse_enabled_tools_spec(raw: object) -> Tuple[bool, FrozenSet[str], FrozenSet[str]]:
    """解析 ``enabled_tools``：``(allow_all, allow, deny)``。

    ``*`` / ``all`` = 全选；``!plugin`` = 排除；无星号则只启用列出的插件。
    空列表 = 一个都不启用。缺省 / 非列表按全选处理（向后兼容旧 config.json）。
    """
    if raw is None:
        return True, frozenset(), frozenset()
    if not isinstance(raw, list):
        return True, frozenset(), frozenset()

    allow_all = False
    allow: set[str] = set()
    deny: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        if name in ("*", "all"):
            allow_all = True
        elif name.startswith("!") and len(name) > 1:
            deny.add(name[1:].strip())
        else:
            allow.add(name)
    if allow_all:
        return True, frozenset(), frozenset(deny)
    if not allow and not deny:
        # 空列表 = 显式「都不启用」，与缺省的「全选」必须区分开
        return False, frozenset(), frozenset()
    return False, frozenset(allow), frozenset(deny)


class ToolScope:
    """一次装配用的工具作用域快照。

    装配链路（检索 / find_tools / 动态暴露）全程只读这一个对象，避免每处
    重读 config.json，也保证同一轮内判定口径一致。
    """

    __slots__ = ("allow_all", "allow", "deny")

    def __init__(self, allow_all: bool, allow: FrozenSet[str], deny: FrozenSet[str]) -> None:
        self.allow_all = allow_all
        self.allow = allow
        self.deny = deny

    @classmethod
    def from_spec(cls, raw: object) -> "ToolScope":
        allow_all, allow, deny = parse_enabled_tools_spec(raw)
        return cls(allow_all, allow, deny)

    @property
    def is_open(self) -> bool:
        """全选且无排除：装配链路可直接短路，不做任何逐工具判定。"""
        return self.allow_all and not self.deny

    def plugin_enabled(self, plugin: str) -> bool:
        if plugin in NEUTRAL_PLUGINS:
            return True
        if self.allow_all:
            return plugin not in self.deny
        return plugin in self.allow and plugin not in self.deny

    def tool_enabled(self, tool_name: str) -> bool:
        """按注册表反查插件后判定；未注册工具一律放行（不在任何人的管辖范围内）。"""
        if self.is_open:
            return True
        from gsuid_core.ai_core.register import find_tool_base

        tb = find_tool_base(tool_name)
        if tb is None:
            return True
        return self.plugin_enabled(tb.plugin)

    def filter_names(self, names: Sequence[str]) -> List[str]:
        if self.is_open:
            return list(names)
        return [n for n in names if self.tool_enabled(n)]

    def filter_tools(self, tools: Sequence[_NamedT]) -> List[_NamedT]:
        """按工具对象过滤。``Tool`` 不带 plugin 字段，统一按名回查注册表判定。"""
        if self.is_open:
            return list(tools)
        return [tool for tool in tools if self.tool_enabled(tool.name)]


def get_tool_scope(persona_name: Optional[str]) -> ToolScope:
    """读 persona 的 ``enabled_tools`` 并快照。无人格 / 读不到 = 全开。"""
    if not persona_name:
        return ToolScope(True, frozenset(), frozenset())
    try:
        from gsuid_core.ai_core.persona.config import persona_config_manager

        cfg = persona_config_manager.get_config(persona_name)
        return ToolScope.from_spec(cfg.get_config(ENABLED_TOOLS_KEY).data)
    except Exception as e:
        # 配置缺失 / 磁盘异常：退全开，绝不因作用域解析失败让人格没有工具
        logger.debug(t("log.agent.tool_scope_resolve_failed_open", p0=persona_name, e=e))
        return ToolScope(True, frozenset(), frozenset())


def list_known_plugins() -> List[str]:
    """当前注册表里出现过的插件名（去重排序），供 webconsole 渲染多选。"""
    from gsuid_core.ai_core.register import get_all_tools

    names = {tb.plugin for tb in get_all_tools().values() if tb.plugin}
    return sorted(names)


def tool_catalog() -> Dict[str, List[ToolCatalogEntry]]:
    """工具目录：按插件分组，供 webconsole 的工具选择器渲染。

    每项含 ``name`` / ``description`` / ``category`` / ``capability_domain`` /
    ``always_mounted``。``always_mounted`` 标出常驻直装工具——它们不经向量检索，
    前端要能明确告诉用户「这些默认就有，不占检索名额」。
    """
    from gsuid_core.ai_core.register import get_all_tools
    from gsuid_core.ai_core.interaction_scaffold import MAIN_AGENT_CORE_TOOLS

    always = set(MAIN_AGENT_CORE_TOOLS)
    by_plugin: Dict[str, List[ToolCatalogEntry]] = {}
    for name, tb in sorted(get_all_tools().items()):
        by_plugin.setdefault(tb.plugin or "core", []).append(
            ToolCatalogEntry(
                name=name,
                description=tb.description,
                category=tb.category,
                capability_domain=tb.capability_domain or "",
                always_mounted=name in always,
            )
        )
    return by_plugin
