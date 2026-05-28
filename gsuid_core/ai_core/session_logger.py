"""
AI Session 日志记录器

为每个 GsCoreAIAgent 实例提供独立的会话日志记录能力。
日志先在内存中缓冲，每隔10分钟自动持久化到 JSON 文件，
实例销毁/结束时也会触发最终持久化。

日志文件命名规则:
    {safe_session_id}_{session_uuid}_{create_time}.json

存储路径:
    data/ai_core/session_logs/
"""

from __future__ import annotations

import json
import time
import uuid
import asyncio
from typing import Any, Dict, List, Literal, Optional, TypedDict
from pathlib import Path
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import AI_SESSION_LOGS_PATH, AI_SUBAGENT_LOGS_PATH

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


class AISessionLogger:
    """
    AI 会话日志记录器

    每个 GsCoreAIAgent 实例对应一个 Logger，独立记录该会话的全生命周期。
    支持内存缓冲 + 定时持久化（10分钟）+ 销毁时最终持久化。

    关联 Agent 设计（预留 agent_mesh 扩展位）：
    - linked_agents 记录与本会话关联的其他 Agent 实例
    - agent_type 字段用于区分关联类型：
      * "sub_agent"   – 由本 Agent 创建的子 Agent（当前主要场景）
      * "peer_agent"  – 同级/对等 Agent（预留，用于 agent_mesh）
      * "parent_agent"– 父 Agent（预留，用于 agent_mesh）
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

        # 关联 Agent 列表（持久化 + 活跃状态）
        # 每个元素: {"agent_type": str, "session_id": str, "session_uuid": str,
        #           "persona_name": str|None, "create_by": str, "linked_at": float}
        self.linked_agents: List[Dict[str, Any]] = []

        # ── 磁盘日志回放（非 subagent） ──
        # 当主 session 被 AISessionRegistry 空闲清理后，主动消息
        # （Heartbeat / ScheduledTask）通过 persist_proactive_emission_to_disk
        # 直接向磁盘日志文件追加了 entry。用户下次搭话时 _get_or_create_ai_session
        # 创建新的 GsCoreAIAgent + AISessionLogger，若不复用已有文件，就会产生
        # 两个独立日志文件（不同 session_uuid），破坏"同一 session 所有日志在同一
        # 文件"的语义。因此对非 subagent logger，初始化时检查磁盘上是否已有同
        # session_id 的日志文件，有则回放复用。
        resumed: Optional[Dict[str, Any]] = None
        resumed_path: Optional[Path] = None
        if not is_subagent:
            resumed, resumed_path = self._find_existing_log_on_disk(session_id)

        # 在 if/else 之前统一声明实例属性（避免 basedpyright "变量声明被同名声明覆盖"）
        self.session_uuid: str = ""
        self.created_at: float = 0.0
        self.updated_at: float = 0.0
        self._last_persisted_updated_at: float = 0.0
        self._last_persisted_at: float = 0.0
        self.entries: List[Dict[str, Any]] = []
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
                    "system_prompt": system_prompt,
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
                    "system_prompt": system_prompt,
                },
            )

        # 启动定时持久化循环
        self._start_persist_loop()

    @staticmethod
    def _find_existing_log_on_disk(session_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
        """在 AI_SESSION_LOGS_PATH 中查找该 session_id 最新的日志文件。

        Returns:
            (data_dict, file_path) 二元组；找不到则返回 (None, None)。
        """
        safe_session_id = session_id.replace(":", "_").replace("/", "_")
        prefix = f"{safe_session_id}_"

        best_path: Optional[Path] = None
        best_updated_at: float = 0.0
        best_data: Optional[Dict[str, Any]] = None

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
        """添加一条日志条目到内存缓冲"""
        if self._closed:
            return
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
        """记录用户输入"""
        self._add_entry("user_input", {"content": str(user_message)})

    def log_thinking(self, content: str) -> None:
        """记录模型思考过程"""
        self._add_entry("thinking", {"content": content})

    def log_tool_call(self, tool_name: str, args: Any, tool_call_id: str) -> None:
        """记录工具调用请求"""
        self._add_entry(
            "tool_call",
            {
                "tool_name": tool_name,
                "args": str(args),
                "tool_call_id": tool_call_id,
            },
        )

    def log_tool_return(self, tool_name: str, content: Any, tool_call_id: str) -> None:
        """记录工具执行返回结果"""
        content_str: str = str(content)
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

    def log_token_usage(self, input_tokens: int, output_tokens: int, model_name: str) -> None:
        """记录 Token 使用量"""
        self._add_entry(
            "token_usage",
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_name": model_name,
            },
        )

    def log_run_start(self, user_message: Any) -> None:
        """记录一次 run 的开始"""
        self._add_entry("run_start", {"user_message": str(user_message)})

    def log_run_end(self, output: Any) -> None:
        """记录一次 run 的结束"""
        self._add_entry("run_end", {"output": str(output)})

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

    @staticmethod
    def persist_proactive_emission_to_disk(
        session_id: str,
        source: ProactiveSource,
        content: str,
        trigger_reason: str,
        generator_log_files: Optional[List[str]] = None,
    ) -> bool:
        """当主 session 不在内存注册表时，直接向磁盘日志文件追加 proactive_emission entry。

        查找该 session_id 最新的日志文件，读取 → 追加 entry + linked_agents → 写回。
        若找不到任何日志文件（用户从未与 AI 对话过），则创建一个最小化的日志文件，
        保证主动消息的日志不丢失。

        注意：本方法不做并发保护，调用方应确保不会与同一文件的其它写操作冲突。
        在实际场景中（Heartbeat 每 30 分钟一次 / ScheduledTask 按调度执行），
        冲突概率极低。

        Returns:
            是否成功写入磁盘
        """
        safe_session_id = session_id.replace(":", "_").replace("/", "_")
        prefix = f"{safe_session_id}_"

        # 1. 在 AI_SESSION_LOGS_PATH 中查找匹配的最新日志文件
        best_path: Optional[Path] = None
        best_updated_at: float = 0.0

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
                except Exception:
                    continue

        # 2. 构造要追加的 entry 和 linked_agent 记录
        now = time.time()
        payload: ProactiveEmissionPayload = {
            "source": source,
            "content": content,
            "trigger_reason": trigger_reason,
            "generator_log_files": list(generator_log_files or []),
        }
        new_entry = {
            "type": "proactive_emission",
            "timestamp": now,
            "data": dict(payload),
        }

        new_linked_agents: List[Dict[str, Any]] = []
        new_agent_linked_entries: List[Dict[str, Any]] = []
        for log_file in generator_log_files or []:
            la_record = {
                "agent_type": "proactive_generator",
                "session_id": Path(log_file).stem,
                "session_uuid": "",
                "persona_name": None,
                "create_by": f"Proactive_{source}",
                "log_file": log_file,
                "linked_at": now,
            }
            new_linked_agents.append(la_record)
            new_agent_linked_entries.append(
                {
                    "type": "agent_linked",
                    "timestamp": now,
                    "data": la_record,
                }
            )

        # 3. 读取现有数据或创建最小化日志
        if best_path is not None:
            try:
                with open(best_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.warning(f"📝 [AISessionLogger] 读取磁盘日志失败: {best_path}")
                return False

            entries: List[Dict[str, Any]] = data.get("entries", [])
            entries.append(new_entry)
            entries.extend(new_agent_linked_entries)

            linked_agents: List[Dict[str, Any]] = data.get("linked_agents", [])
            linked_agents.extend(new_linked_agents)

            data["entries"] = entries
            data["linked_agents"] = linked_agents
            data["updated_at"] = now
            data["entry_count"] = len(entries)
            data["linked_agent_count"] = len(linked_agents)
        else:
            # 用户从未与 AI 对话过，创建最小化日志文件
            session_uuid = str(uuid.uuid4())[:8]
            ts = datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_session_id}_{session_uuid}_{ts}.json"
            best_path = AI_SESSION_LOGS_PATH / filename

            session_created_entry = {
                "type": "session_created",
                "timestamp": now,
                "data": {
                    "session_id": session_id,
                    "session_uuid": session_uuid,
                    "persona_name": None,
                    "create_by": f"Proactive_{source}",
                    "system_prompt": None,
                },
            }

            all_entries = [session_created_entry, new_entry] + new_agent_linked_entries

            data = {
                "session_id": session_id,
                "session_uuid": session_uuid,
                "persona_name": None,
                "create_by": f"Proactive_{source}",
                "is_subagent": False,
                "created_at": now,
                "updated_at": now,
                "ended_at": None,
                "entry_count": len(all_entries),
                "entries": all_entries,
                "linked_agents": new_linked_agents,
                "linked_agent_count": len(new_linked_agents),
            }

        # 4. 写回磁盘
        best_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(best_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"📝 [AISessionLogger] 主动消息已持久化到磁盘: {best_path.name}")
            return True
        except Exception as e:
            logger.warning(f"📝 [AISessionLogger] 写入磁盘日志失败: {best_path}: {e}")
            return False

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

        link_record = {
            "agent_type": agent_type,
            "session_id": agent_session_id,
            "session_uuid": agent_session_uuid,
            "persona_name": persona_name,
            "create_by": create_by,
            "log_file": log_file,
            "linked_at": time.time(),
        }
        self.linked_agents.append(link_record)
        self._add_entry("agent_linked", link_record)
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

    def get_linked_agents(self, agent_type: Optional[LinkedAgentType] = None) -> List[Dict[str, Any]]:
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
        """在后台启动定时持久化任务"""
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

        data: Dict[str, Any] = self._build_data()
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        self._last_persisted_updated_at = self.updated_at
        self._last_persisted_at = time.time()

        logger.debug(f"📝 [AISessionLogger] 持久化日志: {self._file_path.name} ({len(self.entries)} 条)")

    def _build_data(self) -> Dict[str, Any]:
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
