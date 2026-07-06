# 二十一、AI 集成：在插件 repo 内管理 AI Skill（`ai_skill`）

让插件作者把**运行时 AI Skill** 随插件一起放在**自己仓库内**管理，import 时一行注册即生效——
不再需要把 skill 文件夹手动挪进 `data/ai_core/skills/` 才能被框架发现。

> **先分清两种 "skill"**（极易混淆）：
> - **本章说的「运行时 Skill」**：`SKILL.md` + 可选 `scripts/` + 资源文件组成的「带元数据的可执行操作」，
>   由 `pydantic_ai_skills.SkillsToolset` 加载，主人格 / 能力代理通过 `list_skills` / `load_skill` /
>   `run_skill_script` 主动发现并调用。**本章 `ai_skill` 注册的就是它。**
> - **开发文档 skill**：`docs/skills/<skill>/`（如本 `gscore-plugin-development`），是给 AI 看的开发指南，
>   由 `rag/skills_kb.py` 索引进知识库、供 `search_skill_docs` 检索。**与 `ai_skill` 无关。**

## 21.1 与 `@ai_tools` 的区别

| | `@ai_tools` 工具 | AI Skill（`ai_skill`） |
|---|---|---|
| 形态 | Python 函数 | Markdown 文件夹（`SKILL.md` + 可选脚本/资源） |
| 加载 | 注册到 `_TOOL_REGISTRY`，按需向量检索装配 | 由 `SkillsToolset` 发现，模型用 `list_skills` 主动发现 |
| 调用 | 模型直接调函数 | `load_skill` 读正文 → 按指引 `run_skill_script` / `read_skill_resource` |
| 适合 | 明确的原子能力（查数据、发消息） | 多步骤的领域知识 / 操作流程（含可执行脚本） |

## 21.2 目录结构约定

在插件 repo 内建一个 skill 根目录（推荐放插件包下的 `skills/`），每个 skill 一个子文件夹，
**必须**含 `SKILL.md`（带 frontmatter）：

```
MyPlugin/
  MyPlugin/
    __init__.py            # 在这里调用 ai_skill(...)
    skills/
      my-skill/
        SKILL.md           # 必须：frontmatter 含 name + description
        scripts/
          run.py           # 可选：run_skill_script 调用（命令行 --key value 传参）
        reference.md       # 可选：read_skill_resource 读取
```

`SKILL.md` 示例（frontmatter 字段遵循 agentskills.io 约定，`name` 仅小写字母/数字/连字符、≤64 字符）：

```markdown
---
name: my-skill
description: 一句话说明这个技能做什么、何时该用它（≤1024 字符）。
---

# My Skill

详细操作指引（建议 ≤500 行；过长时拆到同目录的 reference 文件里用 read_skill_resource 读）。

## 步骤
1. ...
2. 需要执行脚本时调用 run_skill_script(skill_name="my-skill", script_name="scripts/run.py", args={...})
```

## 21.3 注册（插件 `__init__.py` 顶层）

```python
from pathlib import Path
from gsuid_core.ai_core.register import ai_skill

# 注册本目录下 skills/ 内的全部 SKILL.md（import 即生效）
ai_skill(Path(__file__).parent / "skills")
```

- 一次注册**整个目录**，目录下有几个 `<skill>/SKILL.md` 就注册几个。
- 路径必须存在；不存在只记一条 warning 日志、不报错。
- `plugin` 参数可省略——框架自动从调用方模块路径推断插件名（用于 webconsole 来源标记）。
- 与 `@ai_tools` 一致：AI 总开关 `enable` 关闭时 `ai_skill` 直接早退、不注册。

注册成功后，主人格 / 能力代理（agentic + CapabilityAgent 会话）即可：

```
list_skills()                         # 看到 my-skill
load_skill("my-skill")                # 读取正文 + 资源/脚本清单
run_skill_script("my-skill", "scripts/run.py", {"query": "x"})
read_skill_resource("my-skill", "reference.md")
```

## 21.4 同名冲突与覆盖

框架按「全部插件目录 + `data/ai_core/skills/`」发现 skill，**data 目录放在末位、优先级最高**：
若用户在 `data/ai_core/skills/` 放了同名 skill，会覆盖插件默认值（符合「用户自定义 > 插件默认」），
且该 skill 在 webconsole 内被视为可编辑的 data skill。

> data 目录的 skill 除手动放置外，还可经统一安装链路装入并自动热重载：webconsole
> `POST /api/ai/skills/clone` 或主人专属 AI 工具 `install_skill`（支持 git 仓库 /
> zip、tar 直链 / SKILL.md 直链），见框架侧 `gscore-development` §7.10。

## 21.5 WebConsole 行为（只读）

插件注册的 skill 在 WebConsole AI Skills 管理页：

- **可见 / 可查看**：出现在 `GET /api/ai/skills/list` 与详情中，附带
  `source: "plugin"`、`plugin: "<插件名>"`、`editable: false`；markdown 也能正常查看。
- **只读**：在控制台 `DELETE` 或 `PUT .../markdown` 会被拒绝并提示
  「该技能由插件 X 管理，请在其仓库内修改」——请在插件仓库内改 `SKILL.md`，随插件版本走。

详见 [`gsuid_core/webconsole/docs/15-ai-skills.md`](../../../../gsuid_core/webconsole/docs/15-ai-skills.md)。

## 21.6 热重载

`reload_plugin` 重新 import 插件模块时会再次执行 `ai_skill`，框架按**绝对路径去重**、
每次从磁盘重扫，幂等生效。（已卸载插件的目录条目不会被反注册——与「reload 不反注册 `@ai_tools`」
属同类既有限制；目录在磁盘上即视为仍安装。）

## 21.7 常用 import 速查

```python
from pathlib import Path
from gsuid_core.ai_core.register import ai_skill

ai_skill(Path(__file__).parent / "skills")
```
