"""AI 预算限制管理器。

职责：判定一条消息是否超额（`check`/`evaluate`）、记一笔用量（`record_usage`）、
手动放行某 scope（`reset_scope`），以及供 WebConsole 查询的各类状态计算。

设计要点：
- 规则 / 白名单做 30s 内存 TTL 缓存 + 显式 `invalidate()`（API 写后调用），让拦截热
  路径几乎不查这两张表；用量求和仍实时查账本（带索引）。
- 多条匹配规则同时生效，任一窗口超限即拦截（返回首个超限窗口详情）。
- 窗口支持 rolling（最近 N）与 fixed（对齐零点/周一/epoch 块）两种模式，逐规则可配。
"""

import time
import asyncio
from typing import Any, Dict, List, Tuple, Optional
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import field, asdict, dataclass

from gsuid_core.aps import scheduler
from gsuid_core.i18n import t
from gsuid_core.logger import logger

from .config import budget_config, compute_billable_tokens
from .models import WINDOW_KEYS, AIBudgetRule, AIBudgetWhitelist, AIBudgetUsageRecord

# 规则/白名单缓存有效期（秒）。它们仅经 API 变更，命中后 invalidate 立即失效
_CACHE_TTL = 30.0
# 账本保留天数（最长只需周窗），prune 任务据此清理
_USAGE_RETENTION_DAYS = 8
# 窗口固定时长（秒）
_DAY_SECONDS = 86400
_WEEK_SECONDS = 7 * 86400
# 窗口中文名（提示文案/展示用）
_WINDOW_LABELS = {"short": "短时", "day": "天", "week": "周"}


@dataclass
class WindowStatus:
    """某规则在某窗口的实时状态。"""

    window: str  # short|day|week
    window_seconds: int
    limit: int
    used: int
    remaining: int
    over: bool
    reset_at: Optional[int]  # 该窗口预计可恢复的时间戳（fixed 精确, rolling 估算）


@dataclass
class RuleStatus:
    """某条规则的实时状态（含各窗口明细）。"""

    rule_id: int
    rule_name: str
    scope_type: str
    scope_label: str
    period_mode: str
    blocked: bool
    windows: List[WindowStatus] = field(default_factory=list)


@dataclass
class BudgetDecision:
    """一次预算判定结果。"""

    allowed: bool
    enabled: bool
    exempt: bool
    exempt_reason: str  # master|whitelist|""
    rule_statuses: List[RuleStatus] = field(default_factory=list)
    block_rule_id: Optional[int] = None
    block_scope_label: str = ""
    block_window: Optional[WindowStatus] = None
    message: str = ""  # 面向用户的提示（check 填充）
    notify: bool = False  # 本次是否应实际发送提示（叠加冷却后）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _UsageRow:
    """内存账本里的一条用量流水（与持久化表 AIBudgetUsageRecord 同形）。

    只存原始 token，计费量按当前 count_mode 在求和时现算——故改 count_mode 立即对历史
    窗口生效，无需回填。`persisted` 标记是否已落库，flush 据此只写增量。
    """

    group_id: str
    user_id: str
    bot_id: str
    session_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    exempt: bool
    created_at: int
    persisted: bool = False


class _SafeFormat(dict):
    """str.format_map 用：缺失占位符返回空串而非抛 KeyError。"""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return ""


class BudgetManager:
    """预算限制管理器单例。"""

    _instance: Optional["BudgetManager"] = None

    def __new__(cls) -> "BudgetManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self._rules_cache: List[AIBudgetRule] = []
        self._whitelist_cache: List[AIBudgetWhitelist] = []
        self._cache_ts: float = 0.0
        # session_id -> 上次超额提示时间戳（冷却防刷屏）
        self._notify_ts: Dict[str, float] = {}
        # 用量内存账本——真值源：记账只 append（零 DB 竞争），闸门/看板一律读它、不查库。
        # 启动由 load_from_db 回载，之后 flush 增量落库（详见两者 docstring）。
        self._usage: List[_UsageRow] = []
        # 标记内存是否已完成首次回载：未载完不许 flush，防止空内存把历史"增量"误判覆盖。
        self._loaded: bool = False
        # 串行化 flush / reset 对内存账本 + DB 的读写，杜绝并发重复落库（详见 flush docstring）。
        self._persist_lock: asyncio.Lock = asyncio.Lock()

    # ==================== 缓存 ====================

    def invalidate(self) -> None:
        """使规则/白名单缓存立即失效（API 增删改后调用）。"""
        self._cache_ts = 0.0

    async def _get_cached(self) -> Tuple[List[AIBudgetRule], List[AIBudgetWhitelist]]:
        now = time.time()
        if now - self._cache_ts < _CACHE_TTL and self._cache_ts > 0:
            return self._rules_cache, self._whitelist_cache
        rules = await AIBudgetRule.get_all_rules()
        wl = await AIBudgetWhitelist.get_all_entries()
        self._rules_cache = [r for r in rules if r.enabled]
        self._whitelist_cache = [w for w in wl if w.enabled]
        self._cache_ts = now
        return self._rules_cache, self._whitelist_cache

    # ==================== 窗口数学 ====================

    @staticmethod
    def _window_seconds(window: str, short_window_hours: int) -> int:
        if window == "short":
            return max(1, short_window_hours) * 3600
        if window == "day":
            return _DAY_SECONDS
        return _WEEK_SECONDS

    def _window_start(
        self, window: str, period_mode: str, short_window_hours: int, now: int
    ) -> Tuple[int, Optional[int]]:
        """返回 (窗口起点时间戳, 固定窗口的 reset_at 或 None)。"""
        secs = self._window_seconds(window, short_window_hours)
        if period_mode != "fixed":
            return now - secs, None
        if window == "short":
            start = (now // secs) * secs  # epoch 对齐的 N 小时块
            return start, start + secs
        dt = datetime.fromtimestamp(now)
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if window == "day":
            start = int(midnight.timestamp())
            return start, start + _DAY_SECONDS
        monday = midnight - timedelta(days=dt.weekday())
        start = int(monday.timestamp())
        return start, start + _WEEK_SECONDS

    # ==================== scope / 规则过滤 ====================

    @staticmethod
    def scope_label(scope_type: str, scope_id: str, member_id: str) -> str:
        if scope_type == "global":
            return "全局"
        if scope_type == "group":
            return f"群 {scope_id}"
        if scope_type == "member":
            return f"群 {scope_id} 成员 {member_id}"
        return f"私聊 {scope_id}"

    @staticmethod
    def _rule_filter(rule: AIBudgetRule) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """规则的用量求和过滤器：(group_id, user_id, bot_id)；None=不过滤。"""
        bid = rule.bot_id or None
        if rule.scope_type == "group":
            return rule.scope_id, None, bid
        if rule.scope_type == "member":
            return rule.scope_id, rule.member_id, bid
        if rule.scope_type == "user":
            return "", rule.scope_id, bid
        return None, None, bid  # global

    @staticmethod
    def _scope_filter(
        scope_type: str, scope_id: str, member_id: str, bot_id: str
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """从原始 scope 描述构造用量过滤器（reset / 状态查询用）。"""
        bid = bot_id or None
        if scope_type == "group":
            return scope_id, None, bid
        if scope_type == "member":
            return scope_id, member_id, bid
        if scope_type == "user":
            return "", scope_id, bid
        return None, None, bid

    @staticmethod
    def _scope_representative(scope_type: str, scope_id: str, member_id: str) -> Tuple[str, str]:
        """从 scope 描述构造代表性 (group_id, user_id)，用于复用 _rule_matches。"""
        if scope_type == "group":
            return scope_id, ""
        if scope_type == "member":
            return scope_id, member_id
        if scope_type == "user":
            return "", scope_id
        return "", ""

    async def _scope_short_window_hours(self, scope_type: str, scope_id: str, member_id: str, bot_id: str) -> int:
        """该 scope 适用规则中最大的短窗口小时数（reset 短窗时据此足量清理）。

        取最大值：清得宽只会让放行更彻底，且已按 gid/uid/bid 过滤不会误伤别的
        scope；无匹配规则时回退默认 5h。
        """
        rep_group, rep_user = self._scope_representative(scope_type, scope_id, member_id)
        rules, _wl = await self._get_cached()
        hours = [
            r.short_window_hours
            for r in rules
            if r.limit_short > 0 and self._rule_matches(r, rep_group, rep_user, bot_id)
        ]
        return max(hours) if hours else 5

    @staticmethod
    def _rule_matches(rule: AIBudgetRule, group_id: str, user_id: str, bot_id: str) -> bool:
        """该规则是否作用于这条 (group_id,user_id,bot_id) 消息。"""
        if rule.bot_id and rule.bot_id != bot_id:
            return False
        if rule.scope_type == "global":
            return True
        if rule.scope_type == "group":
            return bool(group_id) and rule.scope_id == group_id
        if rule.scope_type == "member":
            return bool(group_id) and rule.scope_id == group_id and rule.member_id == user_id
        if rule.scope_type == "user":
            return group_id == "" and rule.scope_id == user_id
        return False

    # ==================== 状态计算 ====================

    async def _rule_status(self, rule: AIBudgetRule, now: int, include_exempt: bool, with_reset: bool) -> RuleStatus:
        """按规则自身过滤器计算各窗口用量与是否超限（全程读内存账本，不查库）。"""
        gid, uid, bid = self._rule_filter(rule)
        mode = str(budget_config.get_config("count_mode").data)
        windows: List[WindowStatus] = []
        blocked = False
        for w in WINDOW_KEYS:
            limit = rule.limit_for(w)
            if limit <= 0:
                continue
            since, fixed_reset = self._window_start(w, rule.period_mode, rule.short_window_hours, now)
            used = self._sum_usage(since, gid, uid, bid, include_exempt, mode)
            over = used >= limit
            reset_at = fixed_reset
            if reset_at is None and (over or with_reset):
                earliest = self._earliest_ts(since, gid, uid, bid, include_exempt)
                if earliest is not None:
                    reset_at = earliest + self._window_seconds(w, rule.short_window_hours)
            windows.append(
                WindowStatus(
                    window=w,
                    window_seconds=self._window_seconds(w, rule.short_window_hours),
                    limit=limit,
                    used=used,
                    remaining=max(0, limit - used),
                    over=over,
                    reset_at=reset_at,
                )
            )
            blocked = blocked or over
        return RuleStatus(
            rule_id=int(rule.id),
            rule_name=rule.name,
            scope_type=rule.scope_type,
            scope_label=self.scope_label(rule.scope_type, rule.scope_id, rule.member_id),
            period_mode=rule.period_mode,
            blocked=blocked,
            windows=windows,
        )

    async def rule_live_status(self, rule: AIBudgetRule, with_reset: bool = True) -> RuleStatus:
        """单条规则的实时用量状态（供 API 列表/详情/看板复用）。"""
        include_exempt = bool(budget_config.get_config("count_exempt_usage").data)
        return await self._rule_status(rule, int(time.time()), include_exempt, with_reset)

    async def _exempt_status(self, user_id: str, group_id: str, bot_id: str) -> Tuple[bool, str]:
        """判定该用户在该会话是否豁免，返回 (是否豁免, 原因)。"""
        if bool(budget_config.get_config("exempt_masters").data) and _is_master(user_id):
            return True, "master"
        _rules, whitelist = await self._get_cached()
        for w in whitelist:
            if w.bot_id and w.bot_id != bot_id:
                continue
            if w.user_id != user_id:
                continue
            # group_id 空=全局(含私聊)豁免；否则仅该群内豁免
            if w.group_id == "" or w.group_id == group_id:
                return True, "whitelist"
        return False, ""

    async def evaluate(
        self,
        group_id: str,
        user_id: str,
        bot_id: str,
        with_reset: bool = False,
        force_evaluate: bool = False,
    ) -> BudgetDecision:
        """核心判定。

        `force_evaluate=True` 时即使预算未启用/用户豁免也照常计算规则明细（供只读查询
        展示用量与上限）；闸门（`check`）走默认值，禁用/豁免时零查询早退。
        """
        enabled = bool(budget_config.get_config("enable").data)
        exempt, reason = await self._exempt_status(user_id, group_id, bot_id)

        # 闸门快路径：禁用或豁免直接放行，不查账本
        active = enabled and not exempt
        if not active and not force_evaluate:
            return BudgetDecision(allowed=True, enabled=enabled, exempt=exempt, exempt_reason=reason)

        rules, _wl = await self._get_cached()
        matched = [r for r in rules if self._rule_matches(r, group_id, user_id, bot_id)]
        matched.sort(key=lambda r: (-r.priority, int(r.id or 0)))

        include_exempt = bool(budget_config.get_config("count_exempt_usage").data)
        now = int(time.time())
        statuses: List[RuleStatus] = []
        block_rule: Optional[AIBudgetRule] = None
        block_window: Optional[WindowStatus] = None

        for r in matched:
            status = await self._rule_status(r, now, include_exempt, with_reset)
            statuses.append(status)
            # 仅在真正生效(启用且不豁免)时把超限计为拦截; 强制评估的展示态不拦截
            if active and block_window is None:
                for ws in status.windows:
                    if ws.over:
                        block_window = ws
                        block_rule = r
                        break

        allowed = block_window is None
        return BudgetDecision(
            allowed=allowed,
            enabled=enabled,
            exempt=exempt,
            exempt_reason=reason,
            rule_statuses=statuses,
            block_rule_id=int(block_rule.id) if block_rule is not None else None,
            block_scope_label=(
                self.scope_label(block_rule.scope_type, block_rule.scope_id, block_rule.member_id)
                if block_rule is not None
                else ""
            ),
            block_window=block_window,
        )

    # ==================== 拦截 / 记账 ====================

    async def check_scope(self, group_id: str, user_id: str, bot_id: str, session_id: str) -> BudgetDecision:
        """消息处理前的预算闸门（按 scope 三元组）。

        被动交互(handle_ai)与自主入口(巡检/proactive/定时)的统一入口。
        拦截时填充 message/notify（含按 session_id 的冷却）。
        """
        decision = await self.evaluate(group_id, user_id, bot_id, with_reset=False)
        if decision.allowed:
            return decision
        decision.message = self._format_block_message(decision)
        decision.notify = self._should_notify(session_id)
        return decision

    async def record_usage_scope(
        self,
        group_id: str,
        user_id: str,
        bot_id: str,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> None:
        """按 scope 三元组把一笔用量 append 进内存账本（真值源）。

        `gs_agent` 统一入口对所有可归属 scope 的 run（交互/巡检/proactive/经 contextvar
        继承父 scope 的嵌套子 agent、后台记忆摄入）都经此记账；预算关闭时也记，便于先观察再
        开启。只写内存、不查库——闸门/看板都读内存，落库由 `flush` 定时整批完成。
        """
        if input_tokens <= 0 and output_tokens <= 0 and cache_read_tokens <= 0 and cache_write_tokens <= 0:
            return
        exempt, _reason = await self._exempt_status(user_id, group_id, bot_id)
        # await 之后无其它 await：append 相对其它协程原子，无需加锁。
        self._usage.append(
            _UsageRow(
                group_id=group_id,
                user_id=user_id,
                bot_id=bot_id,
                session_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                exempt=exempt,
                created_at=int(time.time()),
                persisted=False,
            )
        )

    # ==================== 内存账本读取 ====================

    @staticmethod
    def _row_matches(
        row: "_UsageRow",
        group_id: Optional[str],
        user_id: Optional[str],
        bot_id: Optional[str],
        include_exempt: bool,
    ) -> bool:
        """内存账本过滤：group_id/user_id 传 None=该维度不过滤、""=精确匹配空串；
        bot_id 非空才过滤平台；include_exempt=False 时剔除豁免记录。"""
        if group_id is not None and row.group_id != group_id:
            return False
        if user_id is not None and row.user_id != user_id:
            return False
        if bot_id and row.bot_id != bot_id:
            return False
        if not include_exempt and row.exempt:
            return False
        return True

    def _sum_usage(
        self,
        since: int,
        group_id: Optional[str],
        user_id: Optional[str],
        bot_id: Optional[str],
        include_exempt: bool,
        count_mode: str,
    ) -> int:
        """内存账本中匹配过滤器、created_at>=since 的计费 Token 之和（按 count_mode 现算）。"""
        total = 0
        for r in self._usage:
            if r.created_at < since or not self._row_matches(r, group_id, user_id, bot_id, include_exempt):
                continue
            total += compute_billable_tokens(
                r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, count_mode
            )
        return total

    def _earliest_ts(
        self,
        since: int,
        group_id: Optional[str],
        user_id: Optional[str],
        bot_id: Optional[str],
        include_exempt: bool,
    ) -> Optional[int]:
        """窗口内最早一条流水的时间戳（滚动窗口估算 reset_at 用）。"""
        earliest: Optional[int] = None
        for r in self._usage:
            if r.created_at < since or not self._row_matches(r, group_id, user_id, bot_id, include_exempt):
                continue
            if earliest is None or r.created_at < earliest:
                earliest = r.created_at
        return earliest

    def usage_total(
        self,
        since: int,
        group_id: Optional[str] = None,
        user_id: Optional[str] = None,
        bot_id: Optional[str] = None,
        include_exempt: bool = True,
    ) -> int:
        """某过滤器在 [since, now] 的计费 Token 之和（看板 24h 总量等只读查询用，读内存）。"""
        mode = str(budget_config.get_config("count_mode").data)
        return self._sum_usage(since, group_id, user_id, bot_id, include_exempt, mode)

    def top_consumers(
        self,
        dimension: str,
        since: int,
        limit: int = 20,
        bot_id: Optional[str] = None,
        include_exempt: bool = True,
    ) -> List[Dict[str, Any]]:
        """按维度（group/user/member）聚合 Top 消费者（读内存账本）。"""
        if dimension not in ("group", "user", "member"):
            return []
        mode = str(budget_config.get_config("count_mode").data)
        sums: Dict[Tuple[str, ...], int] = defaultdict(int)
        for r in self._usage:
            if r.created_at < since:
                continue
            if bot_id and r.bot_id != bot_id:
                continue
            if not include_exempt and r.exempt:
                continue
            if dimension in ("group", "member") and r.group_id == "":
                continue  # group/member 维度排除私聊(group_id 为空)噪声
            if dimension == "group":
                key: Tuple[str, ...] = (r.group_id,)
            elif dimension == "user":
                key = (r.user_id,)
            else:
                key = (r.group_id, r.user_id)
            sums[key] += compute_billable_tokens(
                r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, mode
            )
        ranked = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        out: List[Dict[str, Any]] = []
        for key, total in ranked:
            if dimension == "group":
                out.append({"group_id": key[0], "total_tokens": total})
            elif dimension == "user":
                out.append({"user_id": key[0], "total_tokens": total})
            else:
                out.append({"group_id": key[0], "user_id": key[1], "total_tokens": total})
        return out

    # ==================== 持久化生命周期（与统计模块共用，见 statistics.startup）====================

    async def load_from_db(self) -> None:
        """启动时把近 `_USAGE_RETENTION_DAYS` 天的流水回载入内存账本，此后只读内存。

        幂等：仅首次（`_loaded` 为假）真正回载，重复调用直接返回，避免把已在内存的行与库里
        同一批重复并入。慢的 DB 读放在锁外，仅内存并入与置位在锁内、与 flush/reset 互斥。
        """
        if self._loaded:
            return
        cutoff = int(time.time()) - _USAGE_RETENTION_DAYS * _DAY_SECONDS
        # with_session 重试耗尽返回 None（不抛）；or [] 兜住，回载失败时按空账本继续。
        rows = await AIBudgetUsageRecord.get_records_since(cutoff) or []
        loaded = [
            _UsageRow(
                group_id=r.group_id,
                user_id=r.user_id,
                bot_id=r.bot_id,
                session_id=r.session_id,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cache_read_tokens=r.cache_read_tokens,
                cache_write_tokens=r.cache_write_tokens,
                exempt=r.exempt,
                created_at=r.created_at,
                persisted=True,
            )
            for r in rows
        ]
        # 保留回载前可能已 append 的实时行（启动竞态），接在回载行之后；求和与顺序无关。
        # 锁内并入并置位，与并发 flush/reset 互斥；双检 _loaded 防并发重复回载。
        async with self._persist_lock:
            if self._loaded:
                return
            self._usage = loaded + self._usage
            self._loaded = True
        logger.info(t("💰 [Budget] 已回载用量账本 {p0} 条", p0=len(loaded)))

    async def flush(self) -> None:
        """把内存中尚未落库（persisted=False）的用量整批写库，并就地淘汰过期行。

        全程持 `_persist_lock`：持久节拍(每30min) / 关停 flush / 管理员 reset 若并发，两个
        flush 可能在各自 await bulk_add 之间快照到同一批 persisted=False 行而重复写库，加锁
        保证「快照→写库→标记 persisted」整体原子。

        未完成回载前不落库（防空内存覆盖语义）。落库失败时 persisted 保持 False，下个周期
        自动重试——绝不丢数据。闸门/看板始终读内存，不依赖本次是否成功。
        """
        if not self._loaded:
            return
        async with self._persist_lock:
            mode = str(budget_config.get_config("count_mode").data)
            dirty = [r for r in self._usage if not r.persisted]
            if dirty:
                payload = [
                    {
                        "bot_id": r.bot_id,
                        "group_id": r.group_id,
                        "user_id": r.user_id,
                        "session_id": r.session_id,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                        "cache_read_tokens": r.cache_read_tokens,
                        "cache_write_tokens": r.cache_write_tokens,
                        "total_tokens": compute_billable_tokens(
                            r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, mode
                        ),
                        "exempt": r.exempt,
                        "created_at": r.created_at,
                    }
                    for r in dirty
                ]
                ok = await AIBudgetUsageRecord.bulk_add(payload)
                if ok:
                    for r in dirty:
                        r.persisted = True
                else:
                    # with_session 重试耗尽返回 None：保留 persisted=False，下个周期重试，绝不漏记。
                    logger.warning(t("💰 [Budget] 用量落库失败，{p0} 条留待下次重试", p0=len(dirty)))
            # 内存淘汰：账本最长只需保留 retention 窗（与 DB prune 同口径）
            cutoff = int(time.time()) - _USAGE_RETENTION_DAYS * _DAY_SECONDS
            if self._usage and self._usage[0].created_at < cutoff:
                self._usage = [r for r in self._usage if r.created_at >= cutoff]

    async def reset_scope(
        self,
        scope_type: str,
        scope_id: str = "",
        member_id: str = "",
        bot_id: str = "",
        window: str = "",
    ) -> int:
        """清除某 scope 的用量（管理员手动放行）：内存账本 + DB 双删。window 留空=清全部。"""
        gid, uid, bid = self._scope_filter(scope_type, scope_id, member_id, bot_id)
        if window in WINDOW_KEYS:
            short_hours = await self._scope_short_window_hours(scope_type, scope_id, member_id, bot_id)
            secs = self._window_seconds(window, short_hours)
            since = int(time.time()) - secs
        else:
            since = 0
        # 与 flush 同锁，避免并发 flush 把本次要删的行又写回库（内存清了、库里却复活）。
        async with self._persist_lock:
            # 内存：移除匹配行（include_exempt=True——手动放行应连豁免记录一并清掉）
            before = len(self._usage)
            self._usage = [
                r for r in self._usage if not (r.created_at >= since and self._row_matches(r, gid, uid, bid, True))
            ]
            removed = before - len(self._usage)
            # DB：同步删除，避免重启回载又把它们带回来
            try:
                await AIBudgetUsageRecord.delete_scope_usage(since, gid, uid, bid)
            except Exception as e:  # noqa: BLE001
                logger.warning(t("💰 [Budget] 重置时删除 DB 流水失败（内存已清，重启或回载残留）: {e}", e=e))
        return removed

    # ==================== 提示文案 / 冷却 ====================

    def _should_notify(self, session_id: str) -> bool:
        if not bool(budget_config.get_config("notify_on_block").data):
            return False
        cooldown = int(budget_config.get_config("notify_cooldown").data)
        now = time.time()
        last = self._notify_ts.get(session_id, 0.0)
        if now - last < cooldown:
            return False
        self._notify_ts[session_id] = now
        return True

    def _format_block_message(self, decision: BudgetDecision) -> str:
        template = str(budget_config.get_config("block_message").data)
        ws = decision.block_window
        window_label = _WINDOW_LABELS.get(ws.window, ws.window) if ws else ""
        if ws and ws.window == "short" and ws.window_seconds:
            window_label = f"{ws.window_seconds // 3600}小时"
        reset_str = ""
        if ws and ws.reset_at:
            reset_str = datetime.fromtimestamp(ws.reset_at).strftime("%m-%d %H:%M")
        mapping = _SafeFormat(
            scope=decision.block_scope_label,
            window=window_label,
            used=ws.used if ws else 0,
            limit=ws.limit if ws else 0,
            reset=reset_str,
        )
        return template.format_map(mapping)


def _is_master(user_id: str) -> bool:
    """是否为机器人主人（委托全框架唯一实现 ``ai_core.utils._is_master_user``）。"""
    from gsuid_core.ai_core.utils import _is_master_user

    return _is_master_user(user_id)


# 全局单例
budget_manager = BudgetManager()


# 用量落库复用统计模块的持久化生命周期（startup 调 load_from_db/flush、_persist_loop
# 周期 flush）；本 job 只保留 DB 端过期清理，内存淘汰在 flush 内顺带完成。


@scheduler.scheduled_job("cron", hour=4, minute=30)
async def _budget_prune_job() -> None:
    """每日清理 DB 中过期的用量流水（持久化后备最长只需保留周窗；内存淘汰在 flush 内完成）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return
    cutoff = int(time.time()) - _USAGE_RETENTION_DAYS * _DAY_SECONDS
    await AIBudgetUsageRecord.prune(cutoff)
