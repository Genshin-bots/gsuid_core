"""v2 · Agent Mesh Kanban · 能力评估代理（capability_evaluator）。

设计稿见 docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md §4。

定位：``capability_evaluator`` 不是普通可调度业务代理，而是 ``register_kanban_task``
之前的**框架内置前置代理**：

- 输入：用户原始任务 + 当前人格名 + 能力代理注册表 + 可用工具域摘要。
- 输出：结构化 JSON ``CapabilityEvaluationResult``：``covered`` / ``missing_capabilities``
  / ``suggested_subtasks`` / ``risk_notes``。
- 执行：一次性无记忆会话，``return_mode="return"``，不写主人格 session history，
  不写 self_model。
- 失败策略：评估失败 → 拒绝拆任务，让主人格如实告诉主人"能力评估失败，暂不接管"，
  禁止主人格硬拆任务硬塞 research_agent 兜底。

近期评估结果缓存（``record_evaluation`` / ``get_recent_evaluation``）用来给
``register_kanban_task`` 做"必须先评估过且 covered=true"的校验。
"""

import re
import json
import time
from typing import Any, Dict, List, Optional
from dataclasses import field, asdict, dataclass

from gsuid_core.logger import logger
from gsuid_core.ai_core.agent_node import AgentNode, list_nodes, register_agent_node

_EVALUATOR_PROFILE_ID = "capability_evaluator"

# 评估结果在内存里缓存 N 秒；超时则要求主人格重新评估
_EVAL_TTL_SECONDS = 15 * 60
# 单 owner 最多保留的近期评估数（防内存膨胀；超过滚动淘汰）
_RECENT_EVALUATIONS_PER_OWNER = 4
# 模糊匹配阈值：register goal 与 evaluate user_goal 的"重叠系数"≥ 0.30 视为同一组任务
# 重叠系数 = |A ∩ B| / min(|A|, |B|)；选这个而非 Jaccard 是因为：register goal 通常
# 是 evaluate user_goal 的"精炼版"或反过来（一个长一个短），用 Jaccard 会被长度差
# 严重惩罚（同一组任务也只 0.15-0.25），用 overlap coefficient 不受长度影响。
#
# 阈值从 0.45 → 0.30（2026-05-24 调整）：原阈值挡住了"虚拟盘账户初始化"这种从一个
# evaluate user_goal 派生出的"子任务级精简标题"——同组任务但分词重叠仅 0.30~0.40。
# 实测会话 a5696b00 中"虚拟股票账户初始化（30万本金）"与原 evaluate user_goal
# "虚拟股票投资模拟：初始化30万虚拟账户，每日A股开盘时段..."重叠正好在 0.30 区间。
# 调到 0.30 后这类合法精简能命中；同时仍能挡住完全不同的主题（如把"画一张天气图"
# 跟"虚拟盘"匹配上的 token 重叠会远低于 0.20）。
_FUZZY_MIN_OVERLAP = 0.30

# owner_user_id -> 最近 N 份评估结果列表（按时间倒序追加，超过 _RECENT_EVALUATIONS_PER_OWNER 滚动淘汰）
_RECENT_EVALUATIONS_BY_OWNER: Dict[str, List["CapabilityEvaluationResult"]] = {}

# 中文 stopwords（高频 / 无信息含量；防"主人 / 任务 / 需要"这种词导致虚假重叠）
_FUZZY_STOPWORDS: frozenset = frozenset(
    {
        "主人",
        "用户",
        "需要",
        "任务",
        "执行",
        "进行",
        "操作",
        "请帮",
        "帮我",
        "现在",
        "今天",
        "明天",
        "随便",
        "如何",
        "什么",
        "可以",
        "应该",
        "想要",
    }
)


@dataclass
class SuggestedSubtask:
    """评估代理返回的单个建议子任务。"""

    description: str
    required_capability: str
    agent_profile: str
    depends_on: List[int] = field(default_factory=list)
    params_hint: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityEvaluationResult:
    """评估代理的最终输出，落到 register_kanban_task 入参时仅校验，不再二次推理。"""

    covered: bool
    missing_capabilities: List[str] = field(default_factory=list)
    available_profiles: List[str] = field(default_factory=list)
    suggested_subtasks: List[SuggestedSubtask] = field(default_factory=list)
    risk_notes: List[str] = field(default_factory=list)
    summary: str = ""
    # 内部字段：评估发起人 + 时间
    owner_user_id: str = ""
    user_goal: str = ""
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _tokenize_for_overlap(text: str) -> set:
    """中英数字混合分词——按字符类切，中文按双字粒度。

    用于 ``get_recent_evaluation`` 的模糊匹配：让同一组任务的两种表述
    （口语版 vs 精炼版、句首词序略改等）能匹配为同一份评估结果。
    """
    raw = re.findall(r"[一-鿿]+|[A-Za-z0-9]+", text or "")
    chunks: List[str] = []
    for r in raw:
        if r and "一" <= r[0] <= "鿿":
            # 中文：双字粒度（长度<2 时整段保留）
            if len(r) < 2:
                chunks.append(r)
            else:
                chunks.extend(r[i : i + 2] for i in range(len(r) - 1))
        else:
            chunks.append(r.lower())
    return {c for c in chunks if c not in _FUZZY_STOPWORDS and len(c) >= 2}


def record_evaluation(result: CapabilityEvaluationResult) -> None:
    """落入近期评估缓存，供 ``register_kanban_task`` 校验。

    存储结构改为 per-owner 列表（最近 ``_RECENT_EVALUATIONS_PER_OWNER`` 份）
    以支持 register 时按 owner 范围内的模糊匹配——见 ``get_recent_evaluation``。
    """
    result.created_at = time.time()
    owner = result.owner_user_id
    lst = _RECENT_EVALUATIONS_BY_OWNER.setdefault(owner, [])
    lst.append(result)
    # 滚动淘汰最老的
    while len(lst) > _RECENT_EVALUATIONS_PER_OWNER:
        lst.pop(0)


def list_recent_evaluations_for_owner(
    owner_user_id: str,
) -> List["CapabilityEvaluationResult"]:
    """返回某 owner 未过期的近期评估（按时间倒序）；过期项一并清理。

    供 ``register_kanban_task`` 在拒绝时给主人格回头线索使用。
    """
    lst = _RECENT_EVALUATIONS_BY_OWNER.get(owner_user_id, [])
    now = time.time()
    valid = [r for r in lst if now - r.created_at <= _EVAL_TTL_SECONDS]
    if len(valid) != len(lst):
        _RECENT_EVALUATIONS_BY_OWNER[owner_user_id] = valid
    return list(reversed(valid))


def get_recent_evaluation(
    owner_user_id: str,
    user_goal: str,
    *,
    fuzzy_min_overlap: float = _FUZZY_MIN_OVERLAP,
) -> Optional[CapabilityEvaluationResult]:
    """取 owner 在 TTL 内最相关的评估结果，匹配粒度先严后宽：

    1. **完全相同**（前 200 字 strip 后字符串相等）→ 直接返回。
    2. **模糊匹配**：把 ``user_goal`` 与每条历史评估的 ``user_goal`` 分别 token 化后
       计算 Jaccard 重叠率，取**最大**且 ≥ ``fuzzy_min_overlap`` 的那条。
    3. 都不匹配 → 返回 None，让 ``register_kanban_task`` 拒绝并提示主人格重新评估。

    这条放宽是 2026-05-23 复盘后增加的——原实现按 (owner, goal前200字) 做精确
    缓存键，导致 evaluate 与 register 之间只要 goal 字符串句首词序略改就 100% 失配。
    """
    candidates = list_recent_evaluations_for_owner(owner_user_id)
    if not candidates:
        return None

    # 步骤 1：完全相同（前 200 字 strip）
    norm = (user_goal or "")[:200].strip()
    for r in candidates:
        if (r.user_goal or "")[:200].strip() == norm:
            return r

    # 步骤 2：模糊匹配（token 重叠率）
    own_toks = _tokenize_for_overlap(user_goal)
    if not own_toks:
        # 无可用 token（极少见，比如纯标点）→ 退化为返回最近一次
        return candidates[0]

    best: Optional["CapabilityEvaluationResult"] = None
    best_score = 0.0
    for r in candidates:
        their = _tokenize_for_overlap(r.user_goal)
        if not their:
            continue
        denom = min(len(own_toks), len(their))
        if denom == 0:
            continue
        # overlap coefficient: |A ∩ B| / min(|A|, |B|)
        score = len(own_toks & their) / denom
        if score > best_score:
            best, best_score = r, score
    if best is not None and best_score >= fuzzy_min_overlap:
        logger.debug(
            f"📋 [Kanban] evaluator 模糊匹配命中: overlap={best_score:.2f} "
            f"owner={owner_user_id} target={user_goal[:30]!r}"
        )
        return best
    return None


_EVALUATOR_PROMPT = """你是「能力评估代理（capability_evaluator）」。**无角色人格，
不做角色扮演，不写任何寒暄。** 你只对一件事负责：判断框架现有能力代理画像与工具
能否独立、安全地覆盖主人给出的复合任务。

【输入】
- 用户原始任务（user_goal）
- 当前主人格名（persona_name）
- 已注册能力代理画像清单 + 每个画像的 when_to_use / 工具域摘要
- 框架可用能力域列表

【判定原则】
1. **诚实 > 看起来万能**。当现有画像不能覆盖关键能力时，必须返回 covered=false 并
   列出缺什么，不要硬塞 research_agent 兜底。
2. **区分"对外副作用"与"持久化模拟"——这是最容易判错的一档**：
   - **真实外部副作用**任务（真实下单 / 转账 / 操控外部账户 / 修改医疗病历 /
     提交法律文书 / 发布到对外渠道）→ 框架未挂载专业外接工具时一律 covered=false。
   - **持久化模拟 / 内部账本类任务**——任何形如"用数据结构维护一份状态 +
     周期性更新它 + 期末做汇总"的任务都属于此类，框架已具备完整能力
     （`record_*` 通用集合 + 业务画像分析能力 + Kanban 周期触发 + 子任务
     `not_before`），**必须 covered=true**。常见形态举例（不限于）：
       - 资产模拟：虚拟盘 / 模拟交易 / 给 N 元让你管理 N 天后考察 / 模拟运营
       - 健康打卡：每日体重 / 饮食 / 训练量记录 + 周月汇总
       - 学习计划：每日单词 / 课程打卡 + 阶段考核
       - 销售 / 项目追踪：每日跟进记录 + 周报 / 月报
       - 任何"建一份状态 → 周期更新 → 最后总结"的任务
   - **此类任务必须用"一棵树包完整生命周期"模板拆**（**新版推荐**——子任务级
     `recurring_trigger`，告别旧版"三棵独立树"折中）：返回的 suggested_subtasks
     就是这棵树的全部阶段子任务，主人格用 **一次** `register_kanban_task` 创建：
       ① 一次性 **init 子任务**（`depends_on=[]`，`recurring_trigger` 留空）：只跑
          一次，用 `record_put` 建好这次任务要维护的所有 record 集合（账户 / 打卡
          日历 / 进度表等）；初始集合通常是空的或携带种子值。
       ② **周期子任务**（`depends_on=[0]`，suggested_subtasks 的对应项**必须带**
          `recurring_trigger` 字段——子任务级而非根级！）：init 完成后框架自动 arm
          挂 APScheduler，每个周期点 fire 时框架克隆出一个执行实例做一次"查当前
          状态 → 决策或采集 → 写入流水 → 必要时更新主表 → 汇报本次"。周期子任务
          自身**永远 armed 不 completed**，整棵树持续 running 直到周期过期。
       ③ 一次性 **final 子任务**（`depends_on=[]`，**严禁 depends_on 周期子任务**——
          周期永不 completed = 死锁）：带 `not_before="<结算时刻 ISO>"`，到点自动
          派出，调 `record_list` 把流水拉回来汇总并出报告。
     时间触发条件**严禁**走 add_interval_task 在主人格侧自己写循环——必须把
     `recurring_trigger` 写在 ② 那个 suggested_subtask 的字段里让 Kanban 自己
     克隆执行实例；任何"等到某个绝对时间再执行一次"语义请用 ③ 子任务的
     `not_before` 字段。
     - ①初始化状态集合：`code_agent` 或对应业务画像；
       关键工具 `record_put` 建主集合；recurring_trigger 空；not_before 空。
     - ②周期执行（每次 fire 做一次）：对应业务画像，无业务画像时用 `code_agent`；
       关键工具为业务工具 + `record_append` / `record_update`；
       recurring_trigger **必填**（cron / interval）；not_before 空。
     - ③期末汇总：`internal_reporter`；
       关键工具 `record_list` + `record_summary` + `render_markdown_to_image`；
       recurring_trigger 空；not_before **必填**（结算时刻 ISO）。

   **关于 recurring_trigger 怎么定**：必须**从用户描述的时段反推 cron**，不要
   套模板。常见时段对应模板（用户没说时段时按下表选最贴近一档，并在 risk_notes
   说明你做了哪条假设）：
     - "每个工作日上午 / 下午" → `cron:0 9-11,14-17 * * 1-5`
     - "每天早晚两次打卡" → `cron:0 8,21 * * *`
     - "每周一例会前" → `cron:0 9 * * 1`
     - "每天傍晚总结" → `cron:0 19 * * *`
     - 用户提到行业特定时段（如证券开盘、医院门诊时间、餐饮翻台时间）→
       按该行业实际时段写，如 A 股开盘可写 `cron:0,30 9-11,13-14 * * 1-5`。
   **禁止**机械套用其它领域的时段——把"每天打卡"派进股票开盘时段、或把
   "每开盘看盘"派进 8-21 点这种错配是判错的硬错误，应在 risk_notes 顶部首条
   告警并给出推荐 cron。
3. 强专业域（医疗诊断、法律意见、实盘交易）框架既无外接工具又非"虚拟/模拟"
   任务时，按 §1 返回 covered=false 并在 risk_notes 说明原因。
4. 子任务推荐应粒度合理（每个 1~3 个工具能跑完），并把可并发的标记为相互无依赖；
   有上下游的子任务显式给 depends_on（引用本数组下标）。Kanban 本身不持有定时器
   （周期触发由 `recurring_trigger` 字段 + APScheduler 桥接）——若任务带
   "明天/每天/N 小时后"等时间触发条件，请在 risk_notes 里提示主人格根据情况
   选择 `add_once_task` / `add_interval_task` 唤醒，或在 `register_kanban_task`
   时直接传 `recurring_trigger`。
5. **不要**在 suggested_subtasks 里写"先用 web_search 查一下"这种万能兜底——那是
   research_agent 自己规划的事，不是你的拆解。
6. **周期触发提示（必须）**：当用户描述包含"每天 / 每周 / 每隔 N / N 小时后 /
   每开盘日 / 持续 / 定时"等周期触发关键词，**且** suggested_subtasks ≥ 2 个时：

   - **首选**：把推荐的 cron / interval **直接写在对应"周期阶段子任务" spec 的
     `params_hint.recurring_trigger` 字段里**（同时在 risk_notes 顶部首条说明"这条
     子任务应该作为子任务级周期模板，主人格 register 时把 params_hint.recurring_trigger
     的值复制到 KanbanSubtaskSpec.recurring_trigger 字段"）——这是新版"一棵树包
     完整生命周期"模板的核心结构。
   - **不要**让主人格用根级 `recurring_trigger`（整棵树克隆）来表达"一棵树里有
     一次 init + N 次周期 + 一次 final"——根级 recurring 每次开火都会克隆整棵
     树，把 init 也跑一遍，导致 record_* 集合被反复重置。子任务级 recurring
     才能让 init 只跑一次。
   - 任何"等到某个绝对时刻再执行一次"语义请用对应子任务 `params_hint.not_before`
     字段（同时在 risk_notes 说明）。

   具体 cron 表达式按 §2 的"从用户描述反推"规则给——把推断依据简短附在 risk_notes
   同条里（如"用户提到工作日上午所以选了 `cron:0 9-11 * * 1-5`"），方便主人格事后
   核对。

   不要把"自己写时间循环判断"塞进 task_prompt——那是 scheduler 的事。

【输出格式】
**严格只输出一个 JSON 对象**——结束 `}` 之后立即停止，**绝不允许**再追加第二份
JSON / 修正版 / "另一种拆法"等任何字符。重复输出多份 JSON 会让框架解析失败，
导致主人格陷入"评估-注册"循环、最终被限流拒绝。要修订请覆盖原对象的字段，
不要追加新对象。不要前后解释，不要 markdown 围栏。结构如下：

{
  "covered": true | false,
  "missing_capabilities": ["缺什么能力1", "缺什么能力2"],
  "available_profiles": ["research_agent", "code_agent", ...],
  "suggested_subtasks": [
    {
      "description": "用一句话描述",
      "required_capability": "归属能力域 / 自然语言",
      "agent_profile": "code_agent",
      "depends_on": [0, 1],
      "params_hint": {}
    }
  ],
  "risk_notes": ["注意点1", "注意点2"],
  "summary": "对主人格的简短总结（≤80 字）"
}

字段含义：
- covered=true 表示现有画像足够；为 false 则 suggested_subtasks 可为空，
  missing_capabilities 必填。
- agent_profile 必须是 available_profiles 列表中已存在的 profile_id。
- depends_on 是本次返回的 suggested_subtasks 数组下标（从 0 开始）。
- 全部字段必填（空数组 / null 也要给）。
"""


def register_capability_evaluator() -> None:
    """把 capability_evaluator 注册为内置节点。由 ``init_planning`` 调用。

    评估代理不经 runner 派活（``evaluate_capability`` 自带 create_agent，预算
    显式 8000/4），故不挂任何 tool pack。
    """
    register_agent_node(
        AgentNode(
            node_id=_EVALUATOR_PROFILE_ID,
            display_name="能力评估代理",
            prompt=_EVALUATOR_PROMPT,
            when_to_use="framework_internal_only",
            match_keywords=[],  # 不参与 resolve_node 自然语言路由
            source="builtin",
        )
    )


def _build_evaluator_context(
    user_goal: str,
    persona_name: str,
) -> str:
    """拼装喂给 evaluator 的输入文本（人格 / 画像清单 / 能力域）。"""
    profiles = [p for p in list_nodes() if p.node_id != _EVALUATOR_PROFILE_ID]
    lines = [
        f"【主人格】{persona_name or '（未知）'}",
        f"【用户任务】{user_goal}",
        "【已注册能力代理画像】",
    ]
    for p in profiles:
        tool_summary = (
            ", ".join(p.tool_names[:8]) + ("…" if len(p.tool_names) > 8 else "")
            if p.tool_names
            else "（无显式白名单，按 tool_query 动态检索）"
        )
        lines.append(f"- {p.node_id} | {p.display_name} | {p.when_to_use} | 工具: {tool_summary}")
    # 能力域：直接读 ToolBase.capability_domain（已在 models.py 定义为属性），
    # 不再用 getattr 兜底（LLM.md §1.4）
    from gsuid_core.ai_core.register import get_registered_tools

    cat_map = get_registered_tools()
    domains: Dict[str, int] = {}
    for cat, tools in cat_map.items():
        for _name, base in tools.items():
            dom = base.capability_domain or cat
            domains[dom] = domains.get(dom, 0) + 1
    lines.append("【框架可用能力域（工具数）】")
    lines.append(", ".join(f"{k}×{v}" for k, v in sorted(domains.items(), key=lambda x: -x[1])[:20]))
    return "\n".join(lines)


# 匹配 markdown 代码围栏：```json ... ``` 或 ``` ... ``` （含可选语言标识）
_FENCED_JSON_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _extract_first_json_object(text: str) -> Optional[str]:
    """从任意文本里捞出第一个看起来像 JSON 对象的子串。

    扫描策略（从严到宽，按出现频率排序）：
    1. 优先匹配 markdown 代码围栏（` ```json {...} ``` `）—— 这是 LLM 最常用的
       包装方式；
    2. 退而求其次：找文本里第一个 ``{``，从该位置截到末尾交给 raw_decode 自己
       判断对象边界——任何前置文本（"**标题**"、"以下是评估结果："等）都被跳过。

    返回截到的子串原文（不剥多余字符）；找不到时返回 None。
    """
    if not text:
        return None
    m = _FENCED_JSON_RE.search(text)
    if m is not None:
        return m.group(1)
    idx = text.find("{")
    if idx >= 0:
        return text[idx:]
    return None


def _parse_evaluator_output(
    raw: str,
    owner_user_id: str,
    user_goal: str,
) -> CapabilityEvaluationResult:
    """解析 evaluator 返回的 JSON。失败时返回 covered=false + risk_notes 标注。

    **容错点（实测会话 17ed4f85 / e05e495b 暴露）**：

    1. 某些模型偶尔会在 JSON 前面加 markdown 标题（如 ``**Capability Evaluation
       — Virtual Stock Trading Simulation**\\n\\n```json\\n{...}```）——纯 strip
       看不到结构，``json.loads`` 直接报 ``Expecting value: line 1 column 1
       (char 0)``。``_extract_first_json_object`` 通用兜底：先抓 ```json 围栏，
       否则从第一个 ``{`` 截起。
    2. 同一份 JSON 输出两次（字段顺序换、内容略变）——``json.loads`` 抛
       ``Extra data``；``json.JSONDecoder.raw_decode`` 解析到第一个对象结束就停。

    两条兜底叠加，覆盖绝大多数 LLM 输出"格式漂移"。
    """
    text = (raw or "").strip()
    candidate = _extract_first_json_object(text)
    if candidate is None:
        return CapabilityEvaluationResult(
            covered=False,
            risk_notes=[
                f"评估代理返回内容里找不到 JSON 对象（既无 ```json 围栏也无 `{{`）。原始输出片段：{text[:200]!r}"
            ],
            summary="评估失败（输出无法解析）",
            owner_user_id=owner_user_id,
            user_goal=user_goal,
        )
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        # 多 JSON 拼接 / 后置闲话兜底：raw_decode 取第一个完整对象，剩下的丢弃。
        try:
            decoder = json.JSONDecoder()
            data, end_idx = decoder.raw_decode(candidate)
            logger.warning(
                f"📋 [Kanban] evaluator 输出有冗余字符，已取首个 JSON 对象"
                f"（{end_idx}/{len(candidate)} bytes）；原错误：{e}"
            )
        except (ValueError, json.JSONDecodeError) as e2:
            return CapabilityEvaluationResult(
                covered=False,
                risk_notes=[f"评估代理返回非 JSON 输出，已拒绝：{e2}。原始片段：{candidate[:200]!r}"],
                summary="评估失败（输出无法解析）",
                owner_user_id=owner_user_id,
                user_goal=user_goal,
            )

    subtasks: List[SuggestedSubtask] = []
    for item in data.get("suggested_subtasks") or []:
        if not isinstance(item, dict):
            continue
        subtasks.append(
            SuggestedSubtask(
                description=str(item.get("description", "")).strip(),
                required_capability=str(item.get("required_capability", "")).strip(),
                agent_profile=str(item.get("agent_profile", "")).strip(),
                depends_on=[int(x) for x in (item.get("depends_on") or []) if isinstance(x, int)],
                params_hint=item.get("params_hint") or {},
            )
        )
    return CapabilityEvaluationResult(
        covered=bool(data.get("covered", False)),
        missing_capabilities=[str(x) for x in (data.get("missing_capabilities") or [])],
        available_profiles=[str(x) for x in (data.get("available_profiles") or [])],
        suggested_subtasks=subtasks,
        risk_notes=[str(x) for x in (data.get("risk_notes") or [])],
        summary=str(data.get("summary", "")).strip(),
        owner_user_id=owner_user_id,
        user_goal=user_goal,
    )


# evaluator 解析失败时的自动重试次数（共最多跑 _EVAL_MAX_ATTEMPTS 次）。
# 解析失败本身是模型抖动（输出格式漂移），统一一次重试即可——主人格不该被推回
# "评估失败 → 自己硬干" 的旁路，本字段是兜底闸刀。
_EVAL_MAX_ATTEMPTS = 2


async def _run_evaluator_once(
    user_message: str,
    owner_user_id: str,
    extra_system: str = "",
) -> str:
    """单次跑一遍 evaluator agent，返回原始文本。隔离日志 / 异常处理。"""
    from gsuid_core.ai_core.gs_agent import create_agent

    system_prompt = _EVALUATOR_PROMPT + (extra_system or "")
    agent = create_agent(
        system_prompt=system_prompt,
        max_tokens=8000,
        max_iterations=4,
        create_by="CapabilityEvaluator",
        task_level="low",
        session_id=f"capeval_{owner_user_id}_{int(time.time() * 1000) % 10_000_000}",
        is_subagent=True,
    )
    try:
        raw = await agent.run(
            user_message=user_message,
            return_mode="return",
        )
        return str(raw or "")
    finally:
        if agent._session_logger is not None:
            agent._session_logger.close()


async def evaluate_capability(
    user_goal: str,
    owner_user_id: str,
    persona_name: str = "",
) -> CapabilityEvaluationResult:
    """跑一次能力评估代理，落入近期评估缓存并返回结构化结果。

    框架内部前置代理：一次性无记忆会话，不污染主人格 session history，不写
    self_model。

    **抗输出格式漂移**（实测会话 e05e495b 暴露）：模型偶尔会把 JSON 包进 markdown
    标题 / 代码围栏，或干脆截断；`_parse_evaluator_output` 已有强容错，但若仍
    解析失败，本函数会自动重试一次并在 system prompt 顶部追加更严格的"裸 JSON"
    口令——只有重试也失败才返回 covered=false。**严禁**直接放过解析失败让主人格
    走旁路（旧版行为，导致主人格在评估失败后自行 `record_put` + `add_interval_task`，
    完全绕过 Kanban / 能力代理体系）。
    """
    # 短期内重复评估，直接复用上一次结果（避免主人格刷工具刷出无意义评估）
    cached = get_recent_evaluation(owner_user_id, user_goal)
    if cached is not None:
        return cached

    context = _build_evaluator_context(user_goal, persona_name)
    last_raw = ""
    last_result: Optional[CapabilityEvaluationResult] = None
    for attempt in range(1, _EVAL_MAX_ATTEMPTS + 1):
        extra_system = ""
        user_message = context
        if attempt > 1:
            # 第二次：在 system / user 两侧都追加更严格的"裸 JSON"指令
            extra_system = (
                "\n\n【上一次输出未通过 JSON 解析（多了 markdown 围栏 / 标题 /"
                " 文字解释 / 重复对象）。本次再来一遍。无论如何**只输出一个裸 JSON"
                " 对象**——首字符是 `{`、末字符是 `}`、前后没有任何字符、没有"
                " markdown 围栏、没有标题、没有解释。"
            )
            user_message = (
                context + "\n\n⚠️ 重要：必须只输出一个裸 JSON 对象，首字符 `{`、末字符 `}`，"
                "前后无任何文字 / markdown 围栏 / 解释。"
            )
        try:
            raw = await _run_evaluator_once(user_message, owner_user_id, extra_system)
        except Exception as e:
            logger.exception(f"📋 [Kanban] 能力评估代理执行失败 attempt={attempt}: {e}")
            last_result = CapabilityEvaluationResult(
                covered=False,
                risk_notes=[f"评估代理执行抛出异常：{type(e).__name__}: {e}"],
                summary="评估失败（代理执行异常）",
                owner_user_id=owner_user_id,
                user_goal=user_goal,
            )
            continue
        last_raw = raw
        result = _parse_evaluator_output(raw, owner_user_id, user_goal)
        if result.covered or not any("无法解析" in r or "非 JSON" in r for r in result.risk_notes):
            # 解析成功（或返回 covered=false 但是模型真这么判定）→ 接受
            record_evaluation(result)
            logger.info(
                f"📋 [Kanban] 能力评估完成 owner={owner_user_id}"
                f" covered={result.covered} attempt={attempt}"
                f" subtasks={len(result.suggested_subtasks)}"
                f" missing={result.missing_capabilities}"
            )
            return result
        # 解析失败 → 留 last_result 以备重试用完后兜底
        last_result = result
        logger.warning(
            f"📋 [Kanban] 能力评估解析失败 owner={owner_user_id} attempt={attempt}"
            f"，将{'重试' if attempt < _EVAL_MAX_ATTEMPTS else '放弃'}。"
            f"原始片段：{last_raw[:200]!r}"
        )

    # 所有 attempt 都失败：返回最近一份失败结果（covered=false + 提示）
    assert last_result is not None
    record_evaluation(last_result)
    return last_result
