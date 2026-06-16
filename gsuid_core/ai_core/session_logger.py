"""
AI Session 日志记录器 —— 整个 ai_core 唯一的会话日志序列化器

为每个 ``GsCoreAIAgent`` 实例提供独立的会话日志记录能力。所有来源（用户对话 /
Heartbeat / ScheduledTask / Kanban / 工具主动发送 / 记忆·meme·评估等后台 LLM 调用）
的日志都经过本类的同一条写盘路径，保证格式统一。

详细设计：``docs/AI_SESSION_LOGGING.md`` / ``plans/ai_session_log_simplification_20260529.md``

────────────────────────────── 会话窗口规则 ──────────────────────────────
一个日志文件 = 一个 (session_id, 会话窗口)。``SESSION_WINDOW_SECONDS`` 默认 1 小时：

- **主 session（非 subagent）**：创建 logger 时查该 session_id 最新的磁盘文件，
  若其 ``updated_at`` 距今 ≤ 窗口 → 续写同一文件（"相同 session_id 写同一日志"）；
  超过窗口 → 新建文件（"会话超时 1 小时后写另一个 session_log"）。
- **subagent**：一次性、按 run 隔离，永不续写——每次独立成文件，靠父 session 的
  ``linked_agents`` 串联，而非合并进同一文件。

────────────────────────────── 文件格式契约 ──────────────────────────────
``_build_data()`` 是唯一的文件结构来源，顶层字段固定为::

    (
        session_id,
        session_uuid,
        persona_name,
        create_by,
        is_subagent,
    )
    (
        created_at,
        updated_at,
        ended_at,
        entry_count,
        entries,
    )
    linked_agents, linked_agent_count

每条 entry 固定为 ``{"type": <SESSION_ENTRY_TYPES 之一>, "timestamp": float,
"data": {...}}``；entry 类型受 ``SESSION_ENTRY_TYPES`` 白名单约束。

日志文件命名规则::

    {safe_session_id}_{session_uuid}_{create_time}.json

存储路径::

    data/ai_core/session_logs/            # 主 session
    data/ai_core/session_logs/subagents/  # subagent（含自动派生的后台调用）
    data/ai_core/session_logs/images/     # 外置的图片（日志里只存引用，见下）

────────────────────────────── 图片外置规则 ──────────────────────────────
用户消息里的 base64 图片**不内联进日志 JSON**——那会让单个日志文件膨胀到几 MB。
``log_user_input`` 会把消息字符串里的 ``data:image/...;base64,...`` 抽出来，按
**内容哈希**去重落盘到 ``session_logs/images/<hash>.<ext>``，日志里只保留
``[图片引用: images/<hash>.<ext>]`` 这样的引用。images 目录与日志文件一样随
``ScheduledCleanLogDay`` 定时清理（见 ``clean_old_session_logs``）。
"""

from __future__ import annotations

import re
import json
import time
import uuid
import base64
import asyncio
import hashlib
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, TypedDict
from pathlib import Path
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import (
    AI_SESSION_LOGS_PATH,
    AI_SUBAGENT_LOGS_PATH,
    AI_SESSION_IMAGES_PATH,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.models import (
        SessionLogEntry,
        LinkedAgentRecord,
        SessionLogFileData,
    )

# 主动消息来源枚举，供 webconsole 与前端复用
ProactiveSource = Literal["heartbeat", "scheduled_task", "kanban", "tool"]


class ProactiveEmissionPayload(TypedDict):
    """主动消息 emission entry 的 data 段结构（详见 plans/proactive_message_session_unification_20260529.md §3.2）"""

    source: ProactiveSource
    content: str
    trigger_reason: str
    generator_log_files: List[str]


# linked_agents 中 agent_type 可选枚举值：
# - "sub_agent"            原有：由本 Agent 创建的子 Agent
# - "peer_agent"           预留：同级 / 对等 Agent
# - "parent_agent"         预留：父 Agent
# - "proactive_generator"  主动消息生成子 Agent（Heartbeat 决策 / 发言、
#                          Kanban 转译、ScheduledTask 执行体等）
LinkedAgentType = Literal["sub_agent", "peer_agent", "parent_agent", "proactive_generator"]

# 会话窗口：同一 session_id 的日志在该时间窗口内续写同一文件，
# 空闲超过窗口后下次写入滚动到新文件（详见模块 docstring "会话窗口规则"）。
SESSION_WINDOW_SECONDS: int = 3600  # 1 小时

# 全部合法 entry 类型白名单。新增 entry 类型必须在此登记，
# 否则 _add_entry 会记 warning（仍按统一结构落盘，不丢数据）。
# 这是"绝不允许不规范格式"的强制点。
SESSION_ENTRY_TYPES: frozenset[str] = frozenset(
    {
        # 生命周期
        "session_created",
        "session_resumed",
        "session_ended",
        # 单次 run
        "system_prompt",
        "run_start",
        "run_end",
        "result",
        "user_input",
        # 模型产出
        "thinking",
        "text_output",
        # 工具
        "tool_call",
        "tool_return",
        "tools_list",
        # 统计 / 节点 / 错误
        "token_usage",
        "node_transition",
        "error",
        # 关联 / 主动消息
        "agent_linked",
        "proactive_emission",
    }
)


# ── base64 图片外置（见模块 docstring "图片外置规则"）──────────────────────
# 匹配日志字符串里的 DataURI 图片：data:image/<subtype>;base64,<base64>
# base64 段用 [A-Za-z0-9+/]+={0,2}，遇到引号/空白/中括号等非 base64 字符即停止，
# 因此对 list 形式 user_message 的 repr（含 ImageUrl(url='data:image/...;base64,...')）
# 同样适用——不会越界吞掉后面的内容。
_DATAURI_IMAGE_RE = re.compile(r"data:image/([A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/]+={0,2})")

# MIME subtype → 文件扩展名（识别不出时统一用 .img）
_IMAGE_MIME_EXT: Dict[str, str] = {
    "jpeg": "jpg",
    "jpg": "jpg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
    "bmp": "bmp",
    "svg+xml": "svg",
}


def _save_base64_image(mime_subtype: str, b64_data: str) -> Optional[str]:
    """把一张 base64 图片落盘到 ``AI_SESSION_IMAGES_PATH``，返回相对引用 ``images/<hash>.<ext>``。

    按图片内容的 sha256 去重命名：同一张图只存一份。文件已存在时只刷新其 mtime，
    使"仍在被引用的图片"在 ScheduledCleanLogDay 清理时保持新鲜、不被误删。
    落盘失败（解码失败 / 写盘异常）返回 None，由调用方退化处理。
    """
    try:
        raw = base64.b64decode(b64_data, validate=False)
    except Exception:
        return None
    if not raw:
        return None

    digest = hashlib.sha256(raw).hexdigest()[:16]
    ext = _IMAGE_MIME_EXT.get(mime_subtype.lower(), "img")
    filename = f"{digest}.{ext}"
    fpath = AI_SESSION_IMAGES_PATH / filename
    try:
        AI_SESSION_IMAGES_PATH.mkdir(parents=True, exist_ok=True)
        if fpath.exists():
            # 内容相同的图片复用同一文件；刷新 mtime 让活跃引用的图片不被定时清理误删
            fpath.touch(exist_ok=True)
        else:
            with open(fpath, "wb") as f:
                f.write(raw)
    except Exception as e:
        logger.warning(f"📝 [AISessionLogger] 图片外置落盘失败，退化为截断: {e}")
        return None
    return f"images/{filename}"


def externalize_base64_images(text: str) -> str:
    """把日志字符串里的 base64 图片外置到 images 目录，替换为 ``[图片引用: images/<hash>.<ext>]``。

    这是"图片以引用形式存入日志"的唯一实现点（见模块 docstring "图片外置规则"）。
    外置失败的图片退化为 ``[图片: base64 <N> 字符, 外置失败]`` 占位，**绝不**把超长
    base64 原样写进日志（避免日志膨胀，与正常路径行为一致）。
    """

    def _repl(m: "re.Match[str]") -> str:
        mime_subtype = m.group(1)
        b64 = m.group(2)
        ref = _save_base64_image(mime_subtype, b64)
        if ref is None:
            return f"[图片: base64 {len(b64)} 字符, 外置失败]"
        return f"[图片引用: {ref}]"

    return _DATAURI_IMAGE_RE.sub(_repl, text)


def normalize_user_message_to_text(user_message: Any) -> str:
    """把用户消息归一为「保留真实换行 + base64 已外置」的纯文本，供 user_input 日志使用。

    - ``str``：原样（仅外置 base64）。
    - ``list`` / ``tuple``（pydantic_ai 的 ``list[UserContent]``，模型支持图片时
      gs_agent 传入的形态）：逐元素转文本——字符串元素保留原文（含真实换行），
      ``ImageUrl`` 等带 ``url`` 字段的内容取其 url 单独成行——再用**真实换行** ``\n``
      拼接。**绝不**用 ``str(list)``：那会用 Python repr 把字符串里的换行转义成字面
      ``\n``（前端按纯文本渲染时无法换行），并把图片包成 ``ImageUrl(url='data:...')``
      这样的噪声。
    - 其他类型：退化为 ``str()``。

    最后统一调用 ``externalize_base64_images``，确保任何形态里的 base64 图片都被落盘
    为 ``images/<hash>.<ext>`` 引用（这是"base64 不写入日志"的兜底，且不依赖 repr 格式）。
    """
    if isinstance(user_message, str):
        text = user_message
    elif isinstance(user_message, (list, tuple)):
        parts: List[str] = []
        for item in user_message:
            if isinstance(item, str):
                parts.append(item)
            else:
                # ImageUrl / BinaryContent 等：优先取 url 字段（base64 DataURI 在此），
                # 取不到再退化为 str(item)，交由 externalize_base64_images 兜底外置。
                url = getattr(item, "url", None)
                parts.append(url if isinstance(url, str) else str(item))
        text = "\n".join(parts)
    else:
        text = str(user_message)
    return externalize_base64_images(text)


class AISessionLogger:
    """
    AI 会话日志记录器 —— ai_core 唯一的会话日志序列化器

    每个 GsCoreAIAgent 实例对应一个 Logger，独立记录该会话的全生命周期。内存缓冲 +
    落盘：**主 session** 启动后台轮询（见 PERSIST_INTERVAL / IDLE_PERSIST_THRESHOLD）；
    **subagent**（含自动派生的后台调用）不轮询，跑完即 close() 或 __del__ 兜底落盘。
    文件归属遵循会话窗口规则（见模块 docstring "会话窗口规则"）。

    关联 Agent 设计（预留 agent_mesh 扩展位）：
    - linked_agents 记录与本会话关联的其他 Agent 实例
    - agent_type 字段用于区分关联类型：
      * "sub_agent"          – 由本 Agent 创建的子 Agent（当前主要场景）
      * "peer_agent"         – 同级/对等 Agent（预留，用于 agent_mesh）
      * "parent_agent"       – 父 Agent（预留，用于 agent_mesh）
      * "proactive_generator"– 主动消息生成子 agent（决策 / 转译 / 执行体）
    """

    PERSIST_INTERVAL: int = 600  # 10分钟兜底强制持久化，单位秒
    IDLE_PERSIST_THRESHOLD: int = 60  # 60 秒无新消息即落盘
    POLL_INTERVAL: int = 15  # 后台轮询周期，单位秒

    def __init__(
        self,
        session_id: str,
        system_prompt: Optional[str] = None,
        persona_name: Optional[str] = None,
        create_by: str = "LLM",
        is_subagent: bool = False,
    ):
        self.session_id: str = session_id
        self.system_prompt: Optional[str] = system_prompt
        self.persona_name: Optional[str] = persona_name
        self.create_by: str = create_by
        self.is_subagent: bool = is_subagent

        self.ended_at: Optional[float] = None
        self._persist_task: Optional[asyncio.Task] = None
        self._closed: bool = False

        # 关联 Agent 列表（持久化 + 活跃状态），元素结构见 LinkedAgentRecord
        self.linked_agents: List[LinkedAgentRecord] = []

        # ── 磁盘日志回放 / 会话窗口续写（非 subagent） ──
        # 当主 session 被 AISessionRegistry 空闲清理后，主动消息（Heartbeat /
        # ScheduledTask）通过 log_standalone_proactive 向磁盘日志文件追加了 entry。
        # 用户下次搭话时 _get_or_create_ai_session 创建新的 GsCoreAIAgent +
        # AISessionLogger，若不复用已有文件，就会产生两个独立日志文件（不同
        # session_uuid），破坏"同一 session 所有日志在同一文件"的语义。因此对非
        # subagent logger，初始化时按会话窗口（SESSION_WINDOW_SECONDS）检查磁盘上
        # 是否已有同 session_id 且未超时的日志文件，有则回放续写；超时则滚动新文件。
        resumed: Optional[SessionLogFileData] = None
        resumed_path: Optional[Path] = None
        if not is_subagent:
            resumed, resumed_path = self._find_existing_log_on_disk(session_id)

        # 在 if/else 之前统一声明实例属性（避免 basedpyright "变量声明被同名声明覆盖"）
        self.session_uuid: str = ""
        self.created_at: float = 0.0
        self.updated_at: float = 0.0
        self._last_persisted_updated_at: float = 0.0
        self._last_persisted_at: float = 0.0
        self.entries: List[SessionLogEntry] = []
        self._file_path: Path = Path("")

        if resumed is not None and resumed_path is not None:
            # 回放：复用磁盘日志的 session_uuid / created_at / entries / linked_agents / 文件路径
            self.session_uuid = resumed.get("session_uuid", str(uuid.uuid4())[:8])
            self.created_at = resumed.get("created_at", time.time())
            self.updated_at = time.time()
            self._last_persisted_updated_at = resumed.get("updated_at", 0.0)
            self._last_persisted_at = time.time()
            self.entries = resumed.get("entries", [])
            self.linked_agents = resumed.get("linked_agents", [])
            self._file_path = resumed_path
            # 记录会话恢复事件（区别于全新创建）
            self._add_entry(
                "session_resumed",
                {
                    "session_id": session_id,
                    "session_uuid": self.session_uuid,
                    "persona_name": persona_name,
                    "create_by": create_by,
                    "resumed_from_entries": len(self.entries) - 1,  # 排除本 entry 自身
                },
            )
        else:
            # 全新创建
            self.session_uuid = str(uuid.uuid4())[:8]
            self.created_at = time.time()
            self.updated_at = self.created_at
            self._last_persisted_updated_at = 0.0
            self._last_persisted_at = 0.0
            self.entries = []
            self._file_path = self._build_file_path()
            # 记录会话创建事件
            self._add_entry(
                "session_created",
                {
                    "session_id": session_id,
                    "session_uuid": self.session_uuid,
                    "persona_name": persona_name,
                    "create_by": create_by,
                },
            )

        # system_prompt 单独作为一条 system_prompt entry 记录（前端按该 entry 渲染
        # 可折叠代码块）。**不再**塞进 session_created / session_resumed 的 data——
        # 避免同一份 prompt 在一次创建里被重复记两遍。
        if self.system_prompt is not None:
            self.log_system_prompt(self.system_prompt)

        # 启动定时持久化循环
        self._start_persist_loop()

    @staticmethod
    def _find_existing_log_on_disk(session_id: str) -> tuple[Optional["SessionLogFileData"], Optional[Path]]:
        """在 AI_SESSION_LOGS_PATH 中查找该 session_id **当前会话窗口内**最新的日志文件。

        会话窗口规则（见模块 docstring）：只有 updated_at 距今 ≤ SESSION_WINDOW_SECONDS
        的文件才会被续写；更早的文件视为"上一段会话已超时关闭"，本次创建会滚动到新
        文件。这是"相同 session_id 写同一日志 / 超时 1 小时后写另一个 session_log"
        的唯一实现点。

        Returns:
            (data_dict, file_path) 二元组；找不到或最新文件已超出窗口则返回 (None, None)。
        """
        safe_session_id = session_id.replace(":", "_").replace("/", "_")
        prefix = f"{safe_session_id}_"

        best_path: Optional[Path] = None
        best_updated_at: float = 0.0
        best_data: Optional[SessionLogFileData] = None

        if AI_SESSION_LOGS_PATH.exists():
            for p in AI_SESSION_LOGS_PATH.iterdir():
                if not p.is_file() or p.suffix != ".json":
                    continue
                if not p.name.startswith(prefix):
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ua = data.get("updated_at", 0.0)
                    if ua > best_updated_at:
                        best_updated_at = ua
                        best_path = p
                        best_data = data
                except Exception:
                    continue

        # 会话窗口判断：最新文件距今已超过窗口 → 不续写，滚动新文件
        if best_updated_at > 0.0 and (time.time() - best_updated_at) > SESSION_WINDOW_SECONDS:
            return None, None

        return best_data, best_path

    def _build_file_path(self) -> Path:
        """构建日志文件路径

        SubAgent 日志独立存放于 session_logs/subagents/ 子目录，
        与主 Agent 日志物理隔离，便于管理和查询。
        """
        ts: str = datetime.fromtimestamp(self.created_at).strftime("%Y%m%d_%H%M%S")
        safe_session_id: str = self.session_id.replace(":", "_").replace("/", "_")
        filename: str = f"{safe_session_id}_{self.session_uuid}_{ts}.json"
        base_path: Path = AI_SUBAGENT_LOGS_PATH if self.is_subagent else AI_SESSION_LOGS_PATH
        return base_path / filename

    def _add_entry(self, entry_type: str, data: Dict[str, Any]) -> None:
        """添加一条日志条目到内存缓冲。

        entry_type 受 SESSION_ENTRY_TYPES 白名单约束（"绝不允许不规范格式"）：
        未登记的类型记 warning 以便开发期立即暴露，但仍按统一结构落盘——
        日志写入路径绝不因校验而抛异常 / 丢数据。
        """
        if self._closed:
            return
        if entry_type not in SESSION_ENTRY_TYPES:
            logger.warning(
                f"📝 [AISessionLogger] 未登记的 entry 类型 '{entry_type}'，"
                f"请在 SESSION_ENTRY_TYPES 中登记（session_id={self.session_id}）"
            )
        self.entries.append(
            {
                "type": entry_type,
                "timestamp": time.time(),
                "data": data,
            }
        )
        self.updated_at = time.time()

    def log_system_prompt(self, system_prompt: str) -> None:
        """记录系统提示词"""
        self._add_entry("system_prompt", {"content": system_prompt})

    def log_user_input(self, user_message: Any) -> None:
        """记录用户输入。

        用户消息里的 base64 图片会被外置到 ``session_logs/images/`` 并替换为图片引用，
        避免 base64 把日志文件撑爆（外置文件随 ScheduledCleanLogDay 清理）。

        当 user_message 是 ``list[UserContent]``（文本 + ImageUrl 混排，模型支持图片
        时 gs_agent 传入的形态）时，由 ``normalize_user_message_to_text`` 逐元素归一，
        **既保留文本里的真实换行**（不被 ``str(list)`` 的 repr 转义成字面 ``\n`` 而导致
        前端无法换行），**又确保 base64 落盘为图片引用**（不依赖 ImageUrl 的 repr 格式）。
        """
        self._add_entry("user_input", {"content": normalize_user_message_to_text(user_message)})

    def log_thinking(self, content: str) -> None:
        """记录模型思考过程"""
        self._add_entry("thinking", {"content": content})

    def log_tool_call(self, tool_name: str, args: Any, tool_call_id: str) -> None:
        """记录工具调用请求。

        工具参数里若夹带 base64 图片（少见，但如截图/渲染类工具可能出现）同样外置，
        与 user_input 保持一致——绝不把 base64 内联进日志（见模块 docstring "图片外置规则"）。
        """
        self._add_entry(
            "tool_call",
            {
                "tool_name": tool_name,
                "args": externalize_base64_images(str(args)),
                "tool_call_id": tool_call_id,
            },
        )

    def log_tool_return(self, tool_name: str, content: Any, tool_call_id: str) -> None:
        """记录工具执行返回结果。

        先外置返回内容里的 base64 图片再截断——否则一张 base64 返回图会被截成 2000 字
        乱码而非干净的图片引用（顺序很关键）。
        """
        content_str: str = externalize_base64_images(str(content))
        if len(content_str) > 2000:
            content_str = content_str[:2000] + f"...[截断, 共{len(content_str)}字符]"
        self._add_entry(
            "tool_return",
            {
                "tool_name": tool_name,
                "content": content_str,
                "tool_call_id": tool_call_id,
            },
        )

    def log_text_output(self, content: str) -> None:
        """记录模型直接输出的文本"""
        self._add_entry("text_output", {"content": content})

    def log_result(self, output: Any, tool_calls: List[str]) -> None:
        """记录单次 run 的最终结果"""
        self._add_entry(
            "result",
            {
                "output": str(output),
                "tool_calls": tool_calls,
            },
        )

    def log_error(self, error_type: str, message: str) -> None:
        """记录错误信息"""
        self._add_entry("error", {"error_type": error_type, "message": message})

    def log_token_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        model_name: str,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """记录 Token 使用量"""
        self._add_entry(
            "token_usage",
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_name": model_name,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
            },
        )

    def log_run_start(self) -> None:
        """记录一次 run 的开始（纯时间线标记）。

        用户输入由 user_input entry 记录，**不在此重复**（run_start 只是 run 的边界）。
        """
        self._add_entry("run_start", {})

    def log_run_end(self) -> None:
        """记录一次 run 的结束（纯时间线标记）。

        最终输出由 result entry 记录（output + tool_calls），**不在此重复**。
        """
        self._add_entry("run_end", {})

    def log_tools_list(self, tools: List[str]) -> None:
        """记录本次传给 AI 的工具列表（去重后）"""
        self._add_entry("tools_list", {"tools": tools})

    def log_node_transition(self, node_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        """记录 Agent 节点状态转换（如 ModelRequestNode / CallToolsNode / End）"""
        self._add_entry("node_transition", {"node_type": node_type, "details": details or {}})

    def log_proactive_emission(
        self,
        source: ProactiveSource,
        content: str,
        trigger_reason: str,
        generator_log_files: Optional[List[str]] = None,
    ) -> None:
        """记录一条主动消息发射事件（详见 §3.2）

        与普通 text_output 的区别：本 entry 表示"在 LLM 当前 run 之外注入到本
        session history 的 assistant turn"，即 Heartbeat / ScheduledTask / Kanban
        / 工具主动调用产生的输出。前端会按 source 分桶高亮显示。
        """
        payload: ProactiveEmissionPayload = {
            "source": source,
            "content": content,
            "trigger_reason": trigger_reason,
            "generator_log_files": list(generator_log_files or []),
        }
        # entry 落盘的 data 与 ProactiveEmissionPayload 同构
        self._add_entry("proactive_emission", dict(payload))

    @classmethod
    def log_standalone_proactive(
        cls,
        session_id: str,
        source: ProactiveSource,
        content: str,
        trigger_reason: str,
        generator_log_files: Optional[List[str]] = None,
    ) -> bool:
        """主 session 不在内存注册表时，把一条 proactive_emission 写进该 session 的日志。

        用一个临时 logger 复用 ``__init__`` 的"会话窗口续写 / 滚动" + 统一的
        ``_build_data`` 写盘逻辑——**格式与活跃 session 完全一致**。这取代了旧的
        手工拼文件结构的 ``persist_proactive_emission_to_disk``（"格式不统一"的根因）。

        - 非 subagent → 走会话窗口：窗口内续写既有文件，超时 / 从未对话过则新建。
        - 子 agent 生成日志通过 ``link_agent`` 串到本 session 的 ``linked_agents``。

        并发说明：与"主 session 同一时刻被创建"理论上存在写竞争，但主动消息频次低
        （Heartbeat / 定时任务按调度），冲突概率极低；这是既有口径，本次不引入也不扩大。

        Returns:
            是否成功写入磁盘（恒为 True；写盘异常由 close()/_persist_sync 内部处理）
        """
        standalone = cls(session_id=session_id, create_by=f"Proactive_{source}")
        for log_file in generator_log_files or []:
            standalone.link_agent(
                agent_session_id=Path(log_file).stem,
                agent_session_uuid="",
                agent_type="proactive_generator",
                create_by=f"Proactive_{source}",
                log_file=log_file,
            )
        standalone.log_proactive_emission(
            source=source,
            content=content,
            trigger_reason=trigger_reason,
            generator_log_files=generator_log_files,
        )
        standalone.close()
        logger.info(f"📝 [AISessionLogger] 主动消息已持久化到磁盘: {standalone._file_path.name}")
        return True

    def link_agent(
        self,
        agent_session_id: str,
        agent_session_uuid: str,
        agent_type: LinkedAgentType = "sub_agent",
        persona_name: Optional[str] = None,
        create_by: Optional[str] = None,
        log_file: Optional[str] = None,
    ) -> None:
        """
        记录关联的 Agent（如 SubAgent、PeerAgent 等）

        Args:
            agent_session_id: 被关联 Agent 的 session_id
            agent_session_uuid: 被关联 Agent 的 session_uuid
            agent_type: 关联类型，默认 "sub_agent"
                        可选: "sub_agent", "peer_agent", "parent_agent",
                              "proactive_generator"
            persona_name: 被关联 Agent 的 persona_name
            create_by: 被关联 Agent 的 create_by
            log_file: 被关联 Agent 的日志文件路径（绝对路径或相对路径）
        """
        if self._closed:
            return

        link_record: LinkedAgentRecord = {
            "agent_type": agent_type,
            "session_id": agent_session_id,
            "session_uuid": agent_session_uuid,
            "persona_name": persona_name,
            "create_by": create_by,
            "log_file": log_file,
            "linked_at": time.time(),
        }
        self.linked_agents.append(link_record)
        self._add_entry("agent_linked", dict(link_record))
        self.updated_at = time.time()
        logger.debug(
            f"📝 [AISessionLogger] 关联 Agent: {agent_type} session_id={agent_session_id}, uuid={agent_session_uuid}"
        )

    @property
    def has_unpersisted_data(self) -> bool:
        """是否存在尚未落盘的新数据。

        当 updated_at > _last_persisted_updated_at 时，说明自上次持久化以来
        有新条目写入，内存版本比磁盘版本更新；反之则说明内存与磁盘完全同步，
        数据已安全落盘。
        """
        return self.updated_at > self._last_persisted_updated_at

    def get_linked_agents(self, agent_type: Optional[LinkedAgentType] = None) -> List["LinkedAgentRecord"]:
        """
        获取关联的 Agent 列表

        Args:
            agent_type: 可选的关联类型过滤，None 则返回全部

        Returns:
            关联 Agent 记录列表
        """
        if agent_type is None:
            return list(self.linked_agents)
        return [a for a in self.linked_agents if a.get("agent_type") == agent_type]

    def _start_persist_loop(self) -> None:
        """在后台启动定时持久化任务。

        仅对**主 session（非 subagent）**启动周期轮询——它们长生命周期，需要兜底
        flush。subagent（含自动派生的后台 LLM 调用）一次性、跑完即 close()，不需要
        各自挂一个 15s 轮询任务，最终落盘由 close() / __del__ 完成。
        """
        if self.is_subagent:
            return
        try:
            loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
            self._persist_task = loop.create_task(self._persist_loop())
        except RuntimeError:
            # 当前没有运行中的事件循环，跳过定时任务（依赖最终 close() 持久化）
            pass

    async def _persist_loop(self) -> None:
        """后台循环：按 POLL_INTERVAL 轮询，满足条件时持久化。

        触发持久化的两类条件（任一满足即刻落盘）：
        1. 距离 self.updated_at 已 ≥ IDLE_PERSIST_THRESHOLD，即"会话空闲超过 1 分钟"。
        2. 距离 self._last_persisted_at 已 ≥ PERSIST_INTERVAL，即"兜底周期到了"。

        没有未落盘的新增内容时不会重写文件。
        """
        while not self._closed:
            await asyncio.sleep(self.POLL_INTERVAL)
            if self._closed:
                break
            if self.updated_at <= self._last_persisted_updated_at:
                continue
            now: float = time.time()
            idle_seconds: float = now - self.updated_at
            since_last_persist: float = now - self._last_persisted_at
            if idle_seconds >= self.IDLE_PERSIST_THRESHOLD or since_last_persist >= self.PERSIST_INTERVAL:
                self._persist_sync()

    def _persist_sync(self) -> None:
        """同步持久化当前内存中的日志到 JSON 文件。

        若 entries 为空、或自上次落盘后没有新增内容，直接返回避免无效写入。
        """
        if not self.entries:
            return
        if self.updated_at <= self._last_persisted_updated_at:
            return

        data = self._build_data()
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        self._last_persisted_updated_at = self.updated_at
        self._last_persisted_at = time.time()

        logger.debug(f"📝 [AISessionLogger] 持久化日志: {self._file_path.name} ({len(self.entries)} 条)")

    def _build_data(self) -> "SessionLogFileData":
        """构建完整的日志数据结构"""
        return {
            "session_id": self.session_id,
            "session_uuid": self.session_uuid,
            "persona_name": self.persona_name,
            "create_by": self.create_by,
            "is_subagent": self.is_subagent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "entry_count": len(self.entries),
            "entries": self.entries,
            "linked_agents": self.linked_agents,
            "linked_agent_count": len(self.linked_agents),
        }

    def close(self) -> None:
        """
        关闭 Logger，执行最终持久化

        应在 GsCoreAIAgent 实例被销毁前调用，确保所有日志落盘。
        """
        if self._closed:
            return

        self._closed = True
        self.ended_at = time.time()
        self._add_entry("session_ended", {"ended_at": self.ended_at})

        if self._persist_task is not None:
            self._persist_task.cancel()

        self._persist_sync()
        logger.info(f"📝 [AISessionLogger] 会话日志已关闭并持久化: {self._file_path.name}")

    def __del__(self) -> None:
        """析构时兜底持久化（若未显式调用 close）"""
        if not self._closed:
            self.close()


def clean_old_session_logs(days: int) -> int:
    """清理 X 天以前的 AI 会话日志文件（main + subagents 目录）及外置图片（images 目录）。

    与框架日志清理（utils/backup/backup_files.clean_log）共用同一个配置
    ``ScheduledCleanLogDay``，由 core_backup 的每日维护任务调用。

    Args:
        days: 保留天数；**为 0（或负数）时不清理**，直接返回 0。

    Returns:
        实际删除的文件数量（日志 + 图片）。

    说明：按文件 mtime 判断（与 clean_log 一致）。活跃 session 会周期性重写文件、
    mtime 一直很新，不会被误删；空闲超过 days 天的 session 早已不在内存注册表，
    其日志文件可安全清理（这也回收了自动派生 subagent 日志的磁盘占用）。外置图片
    在被引用时会刷新 mtime（见 ``_save_base64_image``），故仍在活跃日志里引用的图片
    不会被提前清掉。
    """
    if days <= 0:
        return 0

    cutoff: float = time.time() - days * 86400
    removed: int = 0

    # 1. 日志文件（main + subagents）：仅清理 .json
    for base in (AI_SESSION_LOGS_PATH, AI_SUBAGENT_LOGS_PATH):
        if not base.exists():
            continue
        for p in base.iterdir():
            if not p.is_file() or p.suffix != ".json":
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except FileNotFoundError:
                continue

    # 2. 外置图片（images 目录）：清理任意扩展名的图片文件
    if AI_SESSION_IMAGES_PATH.exists():
        for p in AI_SESSION_IMAGES_PATH.iterdir():
            if not p.is_file():
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except FileNotFoundError:
                continue

    if removed:
        logger.info(f"📝 [AISessionLogger] 已清理 {removed} 个超过 {days} 天的会话日志/图片文件")
    return removed
