"""Entity & Edge 提取提示词

用于从对话文本中提取实体和关系的 LLM 提示词模板。
"""

ENTITY_EXTRACTION_PROMPT = """你是一个信息提取专家，处理来自即时通讯群组的对话记录。

当前群组标识：{scope_key}

<对话内容>
{dialogue_content}
</对话内容>

请执行两项任务：

**任务1：提取实体（Entity）**
实体是对话中出现的具体的人、地点、话题、事件或概念。
- 对话中每一位发言者（[user_id]: 格式）必须作为实体提取，
  name 直接使用 user_id 的原始值（如对话中出现 [444835641]，name 就是 "444835641"），
  tag 包含 "Speaker"

**输出格式示例**：
{{
  "entities": [
    {{"name": "444835641", "summary": "群成员", "tag": ["Speaker"], "is_speaker": true, "user_id": "444835641"}},
    ...
  ]
}}

- 每个实体：name（实体名）、summary（简短描述，不超过50字）、tag（类型标签列表，每项最多3词，最多5项）

**任务2：提取关系（Edge）**
关系是实体之间可验证的事实，格式为完整陈述句。
- 每个关系：source（主语实体名）、target（宾语实体名）、fact（完整事实描述）

**Scope 判断规则（关键）**：
- 若某个 Entity 或 Edge 描述的是发言者的通用个人属性（职业、长期爱好、家庭等），
  与当前群组上下文无关，请在该 Entity 或 Edge 的 JSON 中额外添加 "scope_hint": "user_global" 和 "user_id": "<发言者user_id>"
- 其他情况不添加 scope_hint（默认为群组级）

**输出格式（纯 JSON，不含任何额外文字）**：
{{
  "entities": [
    {{"name": "...", "summary": "...", "tag": ["...", "..."]}},
    {{"name": "user_12345", "summary": "群成员", "tag": ["Speaker"], "is_speaker": true, "user_id": "12345"}},
    {{"name": "户外运动", "summary": "Alice 的长期爱好", "tag": ["Activity", "Hobby"], "scope_hint": "user_global", "user_id": "12345"}}
  ],
  "edges": [
    {{"source": "user_12345", "target": "户外运动", "fact": "Alice 喜欢户外运动", "scope_hint": "user_global", "user_id": "12345"}},
    {{"source": "user_12345", "target": "今日活动", "fact": "Alice 在本群提议周末露营"}}
  ]
}}
"""  # noqa: E501
