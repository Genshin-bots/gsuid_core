"""两阶段记忆摄入提示词（Pass 1 分诊 + Pass 2 五态代数）。

静态指令走 system、变量走 user，保持稳定前缀可命中 provider 缓存。
"""

WINDOW_ROUTE_SYSTEM = """你是群聊窗口分诊器。只输出一行 JSON，不要解释、不要 markdown。

任务：把带编号的窗口对话按**话题**聚类，允许同一话题的行号不连续。
禁止把纯表情/复读/无意义口水话/网络烂梗/定型文/广告/第三方机器人或自动化脚本播报/无具体指代的互怼当成话题。

输出结构：
{"clusters":[{"lines":[0,3,6],"action":"extract|episode_only|discard","thread_ref":"T1|new|null","topic_title":"<=24字"}],"open_lines":[7]}

动作判据：
- discard：纯表情/复读、无意义寒暄或瞬时口水话、烂梗/定型文/复制段子、广告或推广口令、
  同群第三方机器人或自动化脚本的模板播报/回执/榜单/状态输出（及群友触发它们的纯指令交互）、无具体指代的互怼。
- episode_only：有正常交流语境，但没有提到任何持久个人/项目事实、人际关系、状态变更、
  本人否认或撤回，或对助手的长期偏好要求。
- extract：含明确的个人/项目属性、人际关系、事件进展、事实更新/取消/撤回、本人否认或澄清他人传闻，
  或对助手的长期偏好要求（只要出现本人否认/澄清传闻或取消/撤回旧计划，即使只有一两句也必须判为 extract）。

故事线判据（严格解耦日常抽边与建长线故事线）：
- 日常几句单次小事/单人属性/偶发吐槽：即使 action="extract"，
  thread_ref 也必须填 null、topic_title 填 ""，禁止为小事新建微故事线。
- 仅当话题明显属于跨多轮/跨时段持续演进的事件、多人互动冲突或阶段性计划时，
  命中下方卡片填 "T1"，明显开启新长线故事线才填 "new" 并给简洁 topic_title。

open_lines：窗口末尾尚未聊完、需要留到下一窗合并理解的行号，最多 8 行且必须连续在末尾。没有则 []。"""

WINDOW_ROUTE_USER = """[已有故事线]
{thread_cards}

[窗口对话]
{dialogue}

只输出 JSON："""


ALGEBRA_SYSTEM = """你是记忆五态代数抽取器。只输出一行 JSON，不要解释、不要 markdown。

从一段群聊话题中抽取：
1) entities：具名实体。每条含 n(名称)、u(user_id 或 null)、s(<=50字摘要)、\
   k(仅当该人真实发言填 speaker；仅被提及的人填 person；其余严格区分 project|item|location|org)。
2) edges：事实边。每条含：
   op: ADD | MERGE | SUPERSEDE | CONTRADICT | RETRACT
   target: MERGE/SUPERSEDE/CONTRADICT/RETRACT 时优先填预取列表中的 E 编号；
     若目标出现在本窗口前几行尚未入列表则填 null 且保持相同 slot
   source / target_name: 主语实体名 / 宾语实体名（单主体 attribute 或 milestone 无独立宾语实体时 target_name 填 null）
   kind: attribute | relation | milestone
   slot: 蛇形英文槽位键（如 car_plan / project_budget / allergy）
   fact: 完整自包含陈述句，禁止悬空谓语
   subject: 事实归属人物的 user_id（他人传闻必须填被谈论者的 user_id 或 null，禁止冒充发言人）
   speaker: 实际说出这句话的发言人的 user_id
   line: 该事实对应的窗口行号
   event_date: 事实发生日期 YYYY-MM-DD 或 null
   ttl_days: 明显属于短期临时近况（如这几天的出行、生病、临时忙碌等）填 3~7，持久事实填 null
3) aliases（可选）：对话中明确指认某群友的外号/真名。
   每条含 user_id、alias、evidence("self_claim"|"at_mention"|"corroboration")。
4) thread_summary（可选）：若本话题关联故事线，输出更新后的 <=120 字故事线滚动摘要；否则 ""。
5) preferences：仅抽针对助手未来回复方式、称呼、工具行为或输出规范的**长期**规则与纠错
   （严禁抽单次任务的局部调整、临时上下文细节或一次性提醒，禁输出 context="possession"/"location"）。
   每条含 user_id / context（"general" 或具体能力域）/ rule / polarity（"do"|"dont"）/ correction（bool）。

硬约束与五态规则：
- 严禁从标有「助手上下文(禁抽事实)」或 __assistant_ 的行抽取任何实体或事实边；助手发言仅作理解人类短接话的参照。
- 第三方自动化输出忽略：忽略同群第三方机器人或自动化脚本发出的模板化回执、播报、榜单、状态通知或娱乐指令输出，
  以及群友触发它们的纯指令交互，禁止为其建实体或抽事实边。
- ADD：新增事实；追加式 milestone 事件永远用 ADD，禁止用 SUPERSEDE 抹除历史次数。
- MERGE：与 target 语义等价，补充细节并累加佐证。
- SUPERSEDE：有具体事实支撑的客观变更取代旧值（如退定金改买别款、搬家换城市）。
- RETRACT：明确取消/撤回旧计划或旧状态且无替代新值（如不买了、项目取消、退单），target 填被撤回的 E 编号；
  若旧计划出现在本窗口前几行，target 填 null 且保持相同 slot。
- CONTRADICT：无解释硬矛盾或本人否认/澄清关于自己的传闻，双方保留并标记冲突；
  若被否认的传闻出现在本窗口前几行，同样直接输出 CONTRADICT（target 填 null 且保持相同 slot），不要退化为普通 ADD。
- 情绪气话防抖：明显属于赌咒、反讽、夸张发泄或情绪化气话的发言，禁止触发 SUPERSEDE 或 RETRACT，也不要抽为长期事实。
- 省略指代保护：口语里无定语的泛指群称呼（如「群里/那边」）禁止擅自脑补改写成当前群号。
- 口水话过滤：纯瞬时状态、寒暄、情绪感叹，或仅指向图片/上下文、脱离对话就无独立语义的空洞句子不抽边。"""

ALGEBRA_USER = """[当前话题]
{thread_card}

[已有实体/槽位预取]
{known_edges}

[窗口对话（行号已在行首）]
{dialogue}

只输出 JSON："""
