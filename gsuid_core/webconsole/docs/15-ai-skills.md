# 15. AI Skills API - /api/ai/skills

## 15.1 获取 AI 技能列表

```
GET /api/ai/skills/list
```

**请求头**：
```
Authorization: Bearer <token>
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "skills": [
            {
                "name": "summarize",
                "description": "Summarize URLs or files with the summarize CLI...",
                "content": "# Summarize\n\nFast CLI to summarize URLs...",
                "license": null,
                "compatibility": null,
                "uri": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize",
                "metadata": {
                    "homepage": "https://summarize.sh"
                }
            }
        ],
        "count": 1
    }
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功 |
| msg | string | 状态信息 |
| data.skills | array | 技能列表 |
| data.skills[].name | string | 技能名称 |
| data.skills[].description | string | 技能描述 |
| data.skills[].content | string | 技能内容（markdown格式） |
| data.skills[].license | string/null | 许可证信息 |
| data.skills[].compatibility | string/null | 兼容性要求 |
| data.skills[].uri | string | 技能目录路径 |
| data.skills[].metadata | object | 技能元数据 |
| data.count | integer | 技能总数 |

---

## 15.2 获取指定技能详情

```
GET /api/ai/skills/{skill_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**响应（技能存在）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "summarize",
        "description": "Summarize URLs or files with the summarize CLI...",
        "content": "# Summarize\n\nFast CLI to summarize URLs...",
        "license": null,
        "compatibility": null,
        "uri": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize",
        "metadata": {
            "homepage": "https://summarize.sh"
        },
        "resources": [
            {
                "name": "_meta.json",
                "description": null,
                "uri": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize\\_meta.json"
            }
        ],
        "scripts": []
    }
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found",
    "data": null
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| data.name | string | 技能名称 |
| data.description | string | 技能描述 |
| data.content | string | 技能内容（markdown格式） |
| data.license | string/null | 许可证信息 |
| data.compatibility | string/null | 兼容性要求 |
| data.uri | string | 技能目录路径 |
| data.metadata | object | 技能元数据 |
| data.resources | array | 技能资源列表 |
| data.resources[].name | string | 资源名称 |
| data.resources[].description | string/null | 资源描述 |
| data.resources[].uri | string | 资源路径 |
| data.scripts | array | 技能脚本列表 |
| data.scripts[].name | string | 脚本名称 |
| data.scripts[].description | string/null | 脚本描述 |
| data.scripts[].uri | string/null | 脚本路径 |

---

## 15.3 删除 AI 技能

```
DELETE /api/ai/skills/{skill_name}
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "Skill 'xxx' deleted successfully"
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found"
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |

---

## 15.4 从 Git 克隆 AI 技能

```
POST /api/ai/skills/clone
```

**请求头**：
```
Authorization: Bearer <token>
```

**请求体**：
```json
{
    "git_url": "https://github.com/user/skill-repo.git",
    "skill_name": "optional-custom-name"
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| git_url | string | 是 | Git 仓库 URL |
| skill_name | string | 否 | 自定义技能名称，不提供则使用仓库名 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "Skill 'skill-repo' cloned successfully",
    "skill_name": "skill-repo"
}
```

**错误响应（技能已存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' already exists"
}
```

**错误响应（Git 克隆失败）**：
```json
{
    "status": 1,
    "msg": "Git clone failed: error message"
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| skill_name | string | 克隆后的技能名称（仅成功时返回） |

---

## 15.5 获取 AI 技能 Markdown 内容

```
GET /api/ai/skills/{skill_name}/markdown
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "skill_name": "summarize",
        "content": "# Summarize\n\nSkill description...",
        "path": "F:\\gsuid_core\\data\\ai_core\\skills\\summarize\\SKILL.md"
    }
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found",
    "data": null
}
```

**错误响应（Markdown 文件不存在）**：
```json
{
    "status": 1,
    "msg": "Markdown file not found for skill 'xxx'",
    "data": null
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
| data.skill_name | string | 技能名称 |
| data.content | string | Markdown 文件内容 |
| data.path | string | Markdown 文件完整路径 |

---

## 15.6 更新 AI 技能 Markdown 内容

```
PUT /api/ai/skills/{skill_name}/markdown
```

**请求头**：
```
Authorization: Bearer <token>
```

**路径参数**：
| 参数 | 类型 | 说明 |
|------|------|------|
| skill_name | string | 技能名称 |

**请求体**：
```json
{
    "content": "# Updated Skill Name\n\nUpdated description..."
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| content | string | 是 | 新的 Markdown 内容 |

**响应（成功）**：
```json
{
    "status": 0,
    "msg": "Skill 'xxx' markdown updated successfully"
}
```

**错误响应（技能不存在）**：
```json
{
    "status": 1,
    "msg": "Skill 'xxx' not found"
}
```

**响应字段说明**：
| 字段 | 类型 | 说明 |
|------|------|------|
| status | integer | 状态码，0表示成功，1表示失败 |
| msg | string | 状态信息 |
