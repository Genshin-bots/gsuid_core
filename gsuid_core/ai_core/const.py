"""GsCoreAIAgent 相关的模块级常量（配置旋钮）。

从 ``gs_agent.py`` 抽出，集中存放「调参/白名单/重试策略」类纯数据常量，便于一处查改。
不放任何带逻辑的函数或有状态的缓存——那些仍留在各自归属模块。

``gs_agent`` 等模块按需从此处 import；历史上以 ``gs_agent.XXX`` 引用的公开常量
（如 ``STALE_CHAT_REQUEST_TTL``）在 ``gs_agent`` 内被 re-export，保持向后兼容。
"""

# L3 会话驻留：一个能力族被使用后，继续在随后多少轮里保持常驻（兜底紧邻的追问）。
_STICKY_FAMILY_TURNS = 3
# L5 上下文增强检索：把最近多少轮用户原话拼进工具向量检索 query（含本轮）。
_RECENT_TEXT_WINDOW = 3

# 渐进式工具暴露总开关：开启后非闲聊轮额外挂 find_tools + RetrievableToolset，
# 模型可推理中途按需检索并即时拿到工具；置 False 回退静态装配，闲聊轮恒不挂。
ENABLE_PROGRESSIVE_TOOLS = True
# 渐进式工具暴露的意图门：这些意图轮**不**挂 find_tools（高频/无工具需求，省一次潜在往返）。
_PROGRESSIVE_TOOLS_SKIP_INTENTS = ("闲聊",)

# 工具自动装配白名单：仅列表内 create_by 未显式传 tools 时走向量检索装配。
# CapabilityAgent 自带显式 tools 故排除（见 runner.py）。
_AGENTIC_CREATE_BY = ("SubAgent", "Chat", "Agent", "AutoPlanner")

# skills_toolset 挂载白名单：agentic + CapabilityAgent
# （需 list_skills/run_skill_script）；名单外后台调用不挂，避免白送 token。
_SKILLS_CREATE_BY = (*_AGENTIC_CREATE_BY, "CapabilityAgent")

# 框架默认的工具前摇台词（仅针对耗时工具），必须「人格中性」、任何 Persona 都能套用。
# 带角色口吻的台词由各 Persona 在 config.json 的 "pre_tool_expressions" 覆盖（空串=不前摇）。
_FRAMEWORK_PRE_TOOL_EXPRESSIONS: dict[str, str] = {
    "web_search_tool": "稍等，我查一下相关信息…",
    "search_knowledge": "让我先查一下资料…",
    "web_fetch_tool": "我打开这个链接看看…",
    "create_subagent": "这个任务我来安排处理…",
    "render_html_to_image": "稍等，正在生成图片…",
    "render_markdown_to_image": "稍等，正在生成图片…",
    "generate_image": "稍等，正在生成图片，可能需要一点时间…",
    "generate_video": "稍等，正在生成视频，这个会比较久，请耐心等待…",
    "edit_image": "稍等，正在处理图片…",
    "generate_music": "稍等，正在生成音乐…",
}

# 每次运行最多发送的前摇数量，避免刷屏
_MAX_PRE_TOOL_EXPRESSIONS_PER_RUN = 2

# 核心回复请求的瞬时失败重试（网络抖动 / 超时 / 5xx / 529 多可恢复）：至多 _MAX_RUN_ATTEMPTS
# 次、每次间隔 _RUN_RETRY_DELAY 秒；逻辑性 UsageLimitExceeded（有专属兜底总结）不在此列。
_MAX_RUN_ATTEMPTS = 3
_RUN_RETRY_DELAY = 3.0

# 永久性 4xx 客户端错误（内容审核拦截 / 请求体过大 / 参数非法等）：重试必复现，应
# fail-fast 不再重试。408（超时）/429（限流）虽是 4xx 但可重试，明确排除。
_RETRYABLE_4XX = frozenset({408, 429})

# 内容审核拦截特征词（各 provider 文案不一，取并集模糊判定）。仅用于友好文案与统计分类，
# 不影响"是否重试"的判定。
_CONTENT_REJECT_HINTS = ("sensitive", "content policy", "content_policy", "content_filter")
# 内容审核错误码（如 MiniMax 1026）。按词边界匹配，避免误命中 request-id / 时间戳里的数字。
_CONTENT_REJECT_CODES = ("1026",)

# 单轮意图-行为不一致检测关键词：thinking 里点名了某工具 / 任务编排意图
# 却没真正调用——直接顶到阈值，下一轮立刻强制提醒。提到模块级避免每轮重建。
_INTENT_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "register_kanban_task",
    "evaluate_agent_mesh_capability",
    "create_subagent",
    "复合多代理任务",
    "任务树",
    "创建任务树",
    "托管",
    "委派",
    # 「枚举时间点」思维信号：即便本轮调了 add_once_task，下一轮也强提醒改走
    # register_kanban_task 的 recurring_trigger 路径。
    "逐个时间点",
    "逐一设置",
    "每个时间点单独",
    "为每个时间点",
    "5个时间点",
    "10个时间点",
    "cron 的话需要写多个",
    "需要写多个触发器",
)

# O-A 群聊队头阻塞防护：交互式回复在 _run_lock 上排队超过此秒数（话题大概率已翻篇）
# 则丢弃本次回复，避免对早已结束的话题"过期答复"。仅作用于 create_by=="Chat" 的主对话。
STALE_CHAT_REQUEST_TTL = 8.0
