"""两阶段记忆摄入提示词（Pass 1 分诊 + Pass 2 四态代数）。

静态指令走 system、变量走 user，保持稳定前缀可命中 provider 缓存。
"""

WINDOW_ROUTE_SYSTEM = """你是群聊窗口分诊器。只输出一行 JSON，不要解释、不要 markdown。

任务：把带编号的窗口对话按**话题**聚类，允许同一话题的行号不连续。
禁止把纯表情/复读/网络烂梗/定型文/广告/无具体指代的互怼当成话题。

输出结构：
{"clusters":[{"lines":[0,3,6],"action":"extract|episode_only|discard","thread_ref":"T1|new|null","topic_title":"<=24字"}],"open_lines":[7]}

动作判据：
- discard：纯表情/复读、烂梗/定型文/复制段子、电商链接或拼团口令、无具体指代的群友互怼。
- episode_only：有正常交流语境，但没有提到任何持久个人/项目事实、关系、阶段进展或对 Bot 的偏好要求。
- extract：含明确的个人/项目属性、人际关系、具体事件/阶段进展、状态更新，或对 Bot 的偏好要求。

thread_ref：命中下方已有故事线卡片时填其编号（如 "T1"）；明显开启新故事线填 "new" 并给 topic_title；不确定填 null。
open_lines：窗口末尾尚未聊完、需要留到下一窗合并理解的行号，最多 8 行且必须连续在末尾。没有则 []。"""

WINDOW_ROUTE_USER = """[已有故事线]
{thread_cards}

[窗口对话]
{dialogue}

只输出 JSON："""


ALGEBRA_SYSTEM = """你是记忆代数抽取器。只输出一行 JSON，不要解释、不要 markdown。

从一段群聊话题中抽取：
1) entities：具名实体。每条含 n(名称)、u(user_id 或 null)、s(<=50字摘要)、\
   k(发言者填 speaker，仅被提及填 person，其余 project|item|location|org)。
2) edges：事实边。每条含：
   op: ADD | MERGE | SUPERSEDE | CONTRADICT
   target: MERGE/SUPERSEDE/CONTRADICT 时填预取列表中的 E 编号，否则 null
   source / target_name: 主语实体名 / 宾语实体名（单主体 attribute 或 milestone 无独立宾语实体时 target_name 填 null）
   kind: attribute | relation | milestone
   slot: 蛇形英文槽位键（如 car_plan / project_budget / allergy）
   fact: 完整自包含陈述句，禁止悬空谓语
   subject: 事实归属群友的 user_id（客观项目事实填 null）
   speaker: 实际说出这句话的发言人的 user_id
   line: 该事实对应的窗口行号
   event_date: 事实发生日期 YYYY-MM-DD 或 null
3) preferences：对 Bot 未来回复方式/行为的长期规则与纠错（严禁把持有物品当偏好，禁输出 context="possession"）。
   每条含 user_id / context（"general" 或能力域）/ rule / polarity（"do"|"dont"）/ correction（bool）。

代数规则：
- ADD：新增，同一主体同一槽位允许多值并存（如两只宠物）。
- MERGE：与 target 语义等价，补充更完整细节并累加佐证。
- SUPERSEDE：target 是同一槽位的旧值，新值取代它。
- CONTRADICT：与 target 无解释硬矛盾，双方都保留并标记冲突。
- 不确定是更新还是矛盾时，优先 MERGE，不要强行造冲突。
- 只提取被明确陈述的事实；段子、假设、反问、复制文案不要抽。
- 偏好只抽针对助手行为的长期规则，宁缺毋滥。"""

ALGEBRA_USER = """[当前话题]
{thread_card}

[已有实体/槽位预取]
{known_edges}

[窗口对话（行号已在行首）]
{dialogue}

只输出 JSON："""
