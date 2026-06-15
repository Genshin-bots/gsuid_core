# 插件开发代理（plugin_developer_agent）能否正确加载 gscore-plugin-development 指南 —— 审计与修复报告

> 日期：2026-06-15 · 范围：`profiles.py` 画像注册 + 插件开发端到端流程 + `read_plugin_dev_guide` 指南加载链路
> 结论先行：**SKILL 结构从「单文件」重构为「索引页 + references/ 子文档」后，`read_plugin_dev_guide` 工具失效**——它只读 `SKILL.md`，而正文已全部搬到 `references/*.md`，导致按章节关键词查正文**全部命中失败**。本报告已定位根因并**应用修复**，附实测验证。

---

## 一、TL;DR

| 项 | 结论 |
|---|---|
| 画像能否被路由命中 | ✅ 能。`profiles.py` 中 `plugin_developer_agent` 注册完好，关键词 `插件/写插件/plugin` 兜底命中正常。 |
| 工具能否装配到画像 | ✅ 能。`tool_names` 显式列出全部 6 个 `plugin_dev` 工具，`runner._resolve_tools` 按名装配，不依赖向量检索。 |
| 能否脚手架/写码/自检/安装/热加载/自测 | ✅ 能。`scaffold_plugin` / `validate_plugin` / `copy_to_plugin_dir` / `load_plugin_into_core` / `test_plugin_command` 链路与 SKILL 重构无关，未受影响。 |
| **能否查阅权威开发指南** | ❌ **不能（修复前）**。`read_plugin_dev_guide` 只读 `SKILL.md`，正文已迁到 `references/`，按 `触发器`/`数据库`/`to_ai` 等关键词查正文 **100% MISS**，目录也只剩 6 个元标题。 |
| 修复后 | ✅ 工具改为跨 `SKILL.md` + 全部 `references/*.md` 检索，所有章节关键词恢复命中。 |

**净影响（修复前）**：插件开发代理**仍能写出插件**——因为它的 system prompt 里内联了大量高频易错 API（字体 / 发图 / Event 属性 / Bot 方法 / 导入规范等）。但它设计上的「不确定写法时先 `read_plugin_dev_guide` 查证」这条**权威兜底通道被打断**：一旦遇到 prompt 没覆盖的写法（数据库、配置、定时订阅、FastAPI、嵌入 Provider、完整示例等），它查指南只会拿到「未找到」，只能凭记忆臆造 —— 与 prompt「禁止凭记忆瞎写 API」的初衷正相反。

---

## 二、检索到的相关文件

| 文件 | 作用 |
|---|---|
| `gsuid_core/ai_core/capability_agents/profiles.py` | 注册 6 个内置能力代理画像，含 `plugin_developer_agent`（画像 prompt + `tool_names`）。 |
| `gsuid_core/ai_core/buildin_tools/plugin_developer.py` | 插件开发 6 工具实现，含 `read_plugin_dev_guide` 与指南路径常量 `_SKILL_PATH`。 |
| `gsuid_core/ai_core/buildin_tools/file_manager.py` | `read_file_content` 等文件工具——路径被强制限制在 Artifact Workspace 内。 |
| `gsuid_core/ai_core/buildin_tools/__init__.py` | 工具 re-export 与分类说明（`plugin_dev` 分类「永不可检索」）。 |
| `docs/skills/gscore-plugin-development/SKILL.md` | **重构后的索引页**（只剩目录 + 推荐流程 + 关键概念速记）。 |
| `docs/skills/gscore-plugin-development/references/01..20-*.md` | **重构后的章节正文**（触发器 / 数据库 / 配置 / AI 集成 / 完整示例 等 20 篇）。 |

---

## 三、当前端到端流程（梳理）

1. **路由命中画像**：用户对主人格说「写个 XX 插件」→ `resolve_profile` 按 `match_keywords`（`插件`/`写插件`/`plugin`，注册顺序排最后做兜底）命中 `plugin_developer_agent`。仅主人（PM=0）可用。
2. **装配工具**：`runner._resolve_tools` 读取画像 `tool_names`，经 `get_all_tools` **按名取**（`plugin_dev` 分类被 `NON_SEARCHABLE_TOOL_CATEGORIES` 标记为永不可检索，只有这里显式引用才装配）。装配到：
   `list_directory` / `read_file_content` / `write_file_content` / `diff_file_content` / `scaffold_plugin` / `pull_installed_plugin` / `validate_plugin` / `copy_to_plugin_dir` / `load_plugin_into_core` / `test_plugin_command` / `read_plugin_dev_guide`。
3. **开发循环**：脚手架（工作区）→ 写码 → `validate_plugin` 语法自检 → `copy_to_plugin_dir`（主人审批后落 `plugins/`）→ `load_plugin_into_core` 热加载 → `test_plugin_command` 自测 → 交回主人格。
4. **查指南（关键的一环）**：画像 prompt 多处要求「写代码前若不确定，先 `read_plugin_dev_guide` 查证」「不确定写法时先 `read_plugin_dev_guide(目录→章节)`，不要凭记忆瞎写 API」。该工具是开发代理获取**权威写法**的唯一通道。

---

## 四、问题：SKILL 结构变动击穿了第 3.4 步「查指南」

### 4.1 根因

`read_plugin_dev_guide` 的指南路径是写死的**单一文件**：

```python
# gsuid_core/ai_core/buildin_tools/plugin_developer.py
_SKILL_PATH: Path = Path(__file__).resolve().parents[3] / "docs" / "skills" / "gscore-plugin-development" / "SKILL.md"
```

工具实现（修复前）只 `read_text(_SKILL_PATH)` 一个文件，再用 `_heading_levels` / `_extract_guide_section` 在**这一个文件**里抽目录、抽章节。

但 `SKILL.md` 已重构为「主入口（索引）+ `references/` 子文档（正文）」：

```
docs/skills/gscore-plugin-development/
├── SKILL.md                ← 现在只剩：文档目录索引表 + 推荐流程 + 关键概念速记 + 关联文档
└── references/
    ├── 01-plugin-basics.md         # 一、插件基础结构
    ├── 02-sv-and-triggers.md       # 二、SV 与触发器
    ├── 04-config-management.md     # 四、配置管理
    ├── 05-database.md              # 五、数据库操作
    ├── 10-ai-to-ai-and-ai-return.md# 十、AI 集成：to_ai 与 ai_return
    ├── 15-full-plugin-example.md   # 十五、完整插件示例
    └── ... 共 20 篇正文
```

`SKILL.md` 内部只剩这几个 Markdown 标题（H1/H2）：`GsCore 插件开发完整指南（核心入口）` / `文档目录索引` / `推荐开发流程（按需跳转）` / `关键概念速记（…）` / `关联文档（本 SKILL 文件夹内）` / `关联文档（同仓库其他位置）`。**所有真正的章节正文标题（触发器、数据库、配置、to_ai、完整示例…）都不在 `SKILL.md` 里了，而在 `references/*.md` 里。**

### 4.2 实测证据（修复前，复跑工具的纯函数逻辑）

- `read_plugin_dev_guide("")`（查目录）→ 只返回 6 个**元标题**（索引页自身的结构标题），看不到任何业务章节。
- 按 prompt 建议的关键词查正文，全部 MISS：

  | 查询关键词 | 修复前结果 |
  |---|---|
  | `触发器` | ❌ 未找到 |
  | `数据库操作` | ❌ 未找到 |
  | `配置管理` | ❌ 未找到 |
  | `完整插件示例` | ❌ 未找到 |
  | `AI 集成：to_ai` / `to_ai` | ❌ 未找到 |
  | `消息收发` | ❌ 未找到 |

  原因：`_extract_guide_section` 只匹配 `SKILL.md` 里的标题，而这些关键词对应的标题已搬到 `references/`。

### 4.3 为什么别的工具救不了

开发代理另有 `read_file_content` 等文件工具，理论上能读文档？**不能**。`file_manager.read_file_content` 经 `_get_safe_path` → `resolve_safe_path` 把路径**强制限制在 Artifact Workspace 内**（防路径穿越），根本到不了仓库的 `docs/skills/.../references/`。因此 `read_plugin_dev_guide` 是开发代理读指南的**唯一**通道，它一断，权威指南就彻底失联。

### 4.4 影响评估

- **不是致命**：画像 prompt（`profiles.py::_PLUGIN_DEVELOPER_PROMPT`）内联了「必须遵守的 GsCore 插件规范」「高频易错 GsCore API（照抄此处写法）」两大段，覆盖了目录结构、触发器、收发、字体、Event/Bot 属性、相对导入、httpx 等最易错点。**简单插件仍可凭 prompt 写出来。**
- **但有真实退化**：prompt 反复要求「不确定就查指南」，而指南查不到 → 代理要么放弃查证、凭记忆臆造（违背 prompt 红线），要么反复传不同关键词得到「未找到」浪费迭代。涉及 **prompt 未内联的专题**（数据库 SQLModel 写法、配置 `CONFIG_DEFAULT`、`gs_subscribe` 订阅、定时任务、`register_help`/`get_new_help` 细节、FastAPI 挂接口、嵌入 Provider 注册、`MyGameUID` 完整端到端示例）时，正确率明显下降。

---

## 五、修复方案（已应用）

**思路**：保持工具对外契约不变（仍是单一 `read_plugin_dev_guide(section)`），但把检索语料从「`SKILL.md` 一个文件」扩展为「`SKILL.md` + 全部 `references/*.md`」。**逐文件**检索（不拼接成一篇），避免一篇末尾章节越界 bleed 到下一篇。

### 5.1 改动点（`gsuid_core/ai_core/buildin_tools/plugin_developer.py`）

1. 新增 `references/` 目录常量：

```python
_REFERENCES_DIR: Path = _SKILL_PATH.parent / "references"
```

2. 新增语料聚合 helper：

```python
def _guide_files() -> list[Path]:
    """指南检索语料：SKILL.md（索引）+ references/*.md（正文），按文件名稳定排序。"""
    files: list[Path] = []
    if _SKILL_PATH.exists():
        files.append(_SKILL_PATH)
    if _REFERENCES_DIR.is_dir():
        files.extend(sorted(_REFERENCES_DIR.glob("*.md")))
    return files
```

3. 重写 `read_plugin_dev_guide`：
   - **目录（`section` 空）**：跨 `SKILL.md` + 所有 `references/*.md` 汇总章节标题；只列到二级标题（章 + 节），避免 20 篇正文把目录撑爆；更深的小节仍可被关键词检索到。
   - **正文（`section` 非空）**：按文件名顺序逐篇尝试 `_extract_guide_section`，命中第一个含该关键词标题的子文档即返回其章节正文。

### 5.2 验证（修复后，同样复跑纯函数逻辑）

- `read_plugin_dev_guide("")` → 目录从 6 行元标题变为 **142 行**完整章节目录（一～二十章 + 各节，如 `2.2 触发器语义速查`、`5.7 为已定义的表添加新列` 等）。
- 章节关键词全部恢复命中：

  | 查询关键词 | 修复后命中文件 | 正文长度 |
  |---|---|---|
  | `触发器` | `02-sv-and-triggers.md` | 6804 |
  | `数据库操作` / `数据库` | `05-database.md` | 10386 |
  | `配置管理` / `配置` | `04-config-management.md` | 5023 |
  | `完整插件示例` | `15-full-plugin-example.md` | 10761 |
  | `to_ai` / `AI 集成` | `10-ai-to-ai-and-ai-return.md` | 5677 |
  | `消息收发` | `03-messaging.md` | 8053 |
  | `帮助` | `08-help-system.md` | 4493 |
  | `定时任务` | `06-scheduler-and-subscribe.md` | 4070 |
  | `FastAPI` | `19-fastapi-plugin-api.md` | 17535 |
  | `嵌入 Provider` | `20-embedding-provider-registry.md` | 5310 |
  | `能力代理` | `14-ai-capability-profile.md` | 12046 |
  | `代码规范红线` | `17-code-redlines.md` | 1642 |

- `python -m py_compile plugin_developer.py` 通过。

---

## 六、备注与后续建议

1. **关键词匹配是子串、大小写不敏感**：因 `references/` 标题里 `to_ai` 带反引号（`` `to_ai` ``），传 `AI 集成：to_ai`（无反引号）会因中间夹了反引号而 MISS；传 `to_ai` 或 `AI 集成` 都能命中。已在工具 docstring 把建议关键词改为更稳的 `触发器 / 数据库 / 配置 / to_ai / 完整插件示例`。
2. **防回归**：`read_plugin_dev_guide` 现在依赖 `references/` 子目录存在。若将来再次重构 SKILL 目录结构（改名 / 再拆层），需同步 `_guide_files()` 的发现逻辑。建议给该工具补一个轻量单测：断言「`read_plugin_dev_guide("触发器")` 非空且含 `on_command`」，把目录结构契约钉死。
3. **本次未改动画像 prompt 的整体逻辑**：`profiles.py` 中 `plugin_developer_agent` 注册、`tool_names`、工作流文案均保持原样——它们本就正确，问题只在指南加载实现一处。
4. **`data/` 兼容性**：与本问题无关，沿用现状（覆盖更新不代搬 `data/`，由插件自身负责）。

---

## 七、附：受影响代码位置速查

- 画像注册：`gsuid_core/ai_core/capability_agents/profiles.py` → `register_builtin_profiles()` 内 `plugin_developer_agent` 段。
- 指南路径常量 + `references` 常量：`gsuid_core/ai_core/buildin_tools/plugin_developer.py` 顶部 `_SKILL_PATH` / `_REFERENCES_DIR`。
- 指南检索实现：同文件 `_guide_files()` / `read_plugin_dev_guide()` / `_extract_guide_section()` / `_heading_levels()`。
- 文件工具沙盒边界：`gsuid_core/ai_core/buildin_tools/file_manager.py` → `_get_safe_path()`。
- 指南内容：`docs/skills/gscore-plugin-development/SKILL.md` + `references/01..20-*.md`。

---

## 八、二次迭代：启动挂载知识库 + 混合检索（已实现）

### 8.1 动机

第五节的修复让 `read_plugin_dev_guide` 重新能用，但它仍是**子串标题匹配**：关键词必须命中
Markdown 标题（如 `AI 集成：to_ai` 因标题里 `to_ai` 带反引号而 MISS）。命中与否对措辞敏感，
体验"不太稳定"。知识库侧已有 **dense + BM25 稀疏混合检索（Qdrant 原生 RRF）**，对专名 / 术语 /
编号召回更稳、精度更高。故把指南挂载进知识库，让开发代理改用语义检索查指南。

### 8.2 设计

| 关注点 | 方案 |
|---|---|
| **挂载** | 启动期 `rag.startup.init_all()` 末尾调用 `sync_plugin_dev_guide()`，把 `references/*.md` 每篇作为一个文档 `add_knowledge_document(doc_id="pdev::<stem>", items=..., replace=True)` 导入（→ SQL 真值源 + dense+BM25 向量）。 |
| **分片粒度** | **按 Markdown 小节（H2）切分**——一小节一片，检索更聚焦。代码围栏感知：`_iter_h2_sections` 跳过围栏内的 `#`；超长小节用 `_atomic_segments`+`_pack_segments` **保代码块完整地**细切，仅单个原子块超 2200 字才硬切。实测 20 篇 → 221 片，最长 2040（完整代码块），围栏被切断的片 6/221。分片策略版本 `_CHUNKER_VERSION` 折进内容哈希，改切分方式会自动重切重嵌。 |
| **隔离命名空间** | 全部分片写在保留命名空间 `plugin="__plugin_dev_guide__"` + `source="builtin_doc"`。`sync_knowledge` 只清 `source="plugin"`、`reconcile_manual_knowledge` 只管 `source="manual"`，互不干扰。 |
| **防污染日常 RAG** | `query_knowledge` 新增 `exclude_plugins`（`must_not` 下推 Qdrant）。**所有聊天侧入口都排除该命名空间**：① `search_knowledge` 工具；② `mode_classifier` 的"问答"意图试探检索。这样普通聊天 / 主人格保底 RAG 永远不会捞出插件开发文档。 |
| **专用检索工具** | 新增 PM=0 工具 `search_plugin_dev_guide(query)`（`category="plugin_dev"`，永不可检索、仅本画像装配），内部 `query_knowledge(plugin_filter=["__plugin_dev_guide__"])`。 |
| **保留兜底** | `read_plugin_dev_guide`（第五节已修）**保留**：用于 RAG 关闭 / Qdrant 不可用时的兜底，以及"按章名读整章完整上下文"。检索工具无命中时也提示回退到它。 |
| **幂等** | 按每篇文件内容哈希（写进分片 tags 的 `_srchash:`）跳过未变化文档，避免每次启动重复嵌入数百分片；命名空间在向量库被清空时强制重嵌自愈；维度迁移由 `init_knowledge_collection` 的全量 payload 备份重嵌统一覆盖。章节文件删除 / 改名时清理对应陈旧 doc。 |

### 8.3 改动文件

| 文件 | 改动 |
|---|---|
| `gsuid_core/ai_core/rag/plugin_dev_guide.py` | **新增**。命名空间常量 + `sync_plugin_dev_guide()`（幂等挂载）+ `search_plugin_dev_guide_chunks()`。 |
| `gsuid_core/ai_core/rag/knowledge.py` | `query_knowledge` 增 `exclude_plugins`（`must_not` 过滤）。 |
| `gsuid_core/ai_core/rag/startup.py` | `init_all()` 末尾调用 `sync_plugin_dev_guide()`。 |
| `gsuid_core/ai_core/buildin_tools/rag_search.py` | `search_knowledge` 排除指南命名空间。 |
| `gsuid_core/ai_core/classifier/mode_classifier.py` | 问答意图试探检索排除指南命名空间。 |
| `gsuid_core/ai_core/buildin_tools/plugin_developer.py` | **新增** `search_plugin_dev_guide` 工具；`read_plugin_dev_guide` 降级为兜底（docstring 说明分工）。 |
| `gsuid_core/ai_core/buildin_tools/__init__.py` | re-export 新工具 + 文档分类小节同步。 |
| `gsuid_core/ai_core/capability_agents/profiles.py` | `plugin_developer_agent.tool_names` 加 `search_plugin_dev_guide`；prompt 把"查指南"首选改为 `search_plugin_dev_guide`、`read_plugin_dev_guide` 作兜底。 |

### 8.4 验证

- 全部改动文件 `py_compile` 通过。
- 载入完整工具注册表后：`plugin_dev` 分类含 `search_plugin_dev_guide` 与 `read_plugin_dev_guide`；`query_knowledge` 签名含 `exclude_plugins`。
- 新模块独立导入 OK；`_reference_files()` 发现 20 篇章节；`parents[3]` 正确解析到仓库根；标题 / 哈希提取正确。
- 运行期端到端（真正写入向量、检索召回）需启动带 Qdrant + Embedding 的实例验证，建议首启后在 WebConsole 观察日志 `🧠 [PluginDevGuide] 指南挂载完成` 并让插件开发代理实跑一次 `search_plugin_dev_guide`。

### 8.5 备注 / 后续

1. **分片粒度**：按 Markdown 小节切（见 8.2），代码块尽量整块保留。少数超长小节/超大代码块仍会被细切，dense 对超过本地 `bge-small-zh` 512 token 的片有截断，但 BM25 稀疏侧索引全文、RRF 融合可兜住专名/术语/函数名的精确召回；"要某一整章完整上下文"时用 `read_plugin_dev_guide` 读整章——两条通道互补。
2. **WebConsole 知识库列表**：`source_filter="all"` 时会看到 `source="builtin_doc"` 的指南分片（管理员可见，属预期透明）；`manual` 视图不受影响。如不希望管理员列表出现，可在 `get_manual_knowledge_list` / `search_manual_knowledge` 比照加 `must_not`。
3. **首启成本**：首次（或指南变更 / 切分策略版本变更后）需嵌入约 220 个分片，是一次性的；之后按内容哈希跳过。

> 注：第八节描述的是只挂载 `gscore-plugin-development` 一份的初版；**已被第九节通用化方案取代**——
> 模块/命名空间/工具名/排除机制均以第九节为准（`skills_kb.py` / `source="skill_doc"` / `search_skill_docs` / `exclude_sources`）。

---

## 九、三次迭代：通用化为「启动挂载 docs/skills 全部内容」（已实现）

### 9.1 动机

第八节只挂载了插件开发一份指南。但 `docs/skills/` 下还有多份**开发向**文档（`gscore-ai-core-api`、
`gscore-adapter-development`、`gscore-deploy`…），都应同样可被检索且同样不该污染日常聊天。把"挂载"
通用化为**自动发现并挂载 `docs/skills/` 下每一个 skill**，新增 skill 目录无需改代码即自动纳入。

### 9.2 设计（取代第八节的插件专用实现）

| 关注点 | 通用方案 |
|---|---|
| **发现** | `skills_kb._discover_skill_docs()` 扫描 `docs/skills/*/`；每个 skill 取 `references/*.md`（无则退回 `SKILL.md`）。 |
| **挂载** | 启动期 `init_all()` 调 `sync_skill_docs()`，逐 skill、逐文件 `add_knowledge_document(doc_id="skilldoc::<skill>::<stem>", items=分小节切片, plugin="skilldoc:<skill>", source="skill_doc", replace=True)`。 |
| **隔离命名空间** | 统一 `source="skill_doc"`；每个 skill 一个命名空间 `plugin="skilldoc:<skill>"`。 |
| **防污染（按来源整类排除）** | `query_knowledge` 新增 `exclude_sources`；聊天侧两处入口（`search_knowledge` 工具 + `mode_classifier` 问答试探）改 `exclude_sources=["skill_doc"]`——**一处排除整类、对将来新增 skill 自动生效**（比第八节按单个 plugin 命名空间排除更通用）。 |
| **检索工具** | `search_plugin_dev_guide` → 改名通用化为 **`search_skill_docs(query, skill="")`**：`skill` 留空检索全部已挂载 skill，传 skill 名（如 `gscore-plugin-development`）限定到一份。`read_plugin_dev_guide` 仍作插件开发指南的确定性整章阅读/兜底。 |
| **画像** | `plugin_developer_agent.tool_names` 用 `search_skill_docs`；prompt 指示写插件时传 `skill="gscore-plugin-development"` 限定范围、避免混入其它 skill。 |
| **分片 / 幂等** | 复用第八节的 Markdown 小节级、代码块感知切分 + 内容哈希幂等，逐 skill 适用。 |

### 9.3 改动文件（相对第八节）

| 文件 | 改动 |
|---|---|
| `gsuid_core/ai_core/rag/skills_kb.py` | **新增（取代 `plugin_dev_guide.py`，后者已删除）**：发现 + `sync_skill_docs()` + `search_skill_doc_chunks(query, skills=None)` + `known_skill_names()` + 切分器。 |
| `gsuid_core/ai_core/rag/knowledge.py` | `query_knowledge` 增 `exclude_sources`（`must_not` on `source`）。 |
| `gsuid_core/ai_core/rag/startup.py` | 改调 `sync_skill_docs()`。 |
| `gsuid_core/ai_core/buildin_tools/rag_search.py` · `classifier/mode_classifier.py` | 聊天侧改 `exclude_sources=["skill_doc"]`。 |
| `gsuid_core/ai_core/buildin_tools/plugin_developer.py` | `search_plugin_dev_guide` → `search_skill_docs(query, skill="")`。 |
| `buildin_tools/__init__.py` · `capability_agents/profiles.py` | re-export / tool_names / prompt 同步为 `search_skill_docs`。 |

### 9.4 验证

- 全部改动文件 `py_compile` 通过；删除 `plugin_dev_guide.py` 后无残留引用。
- 完整工具注册表：`plugin_dev` 含 `search_skill_docs`（旧 `search_plugin_dev_guide` 已移除）；`query_knowledge` 签名含 `exclude_sources` + `exclude_plugins`。
- `_discover_skill_docs()` 自动发现 **4 个 skill**（含运行中新加的 `gscore-deploy`，零改码纳入）。
- Markdown 小节切分实测 4 skill / 56 篇文档 → **586 片**，最长 2211，围栏被切断的片 12/586（≈2%，超大代码块的极少数硬切）。
- 运行期端到端（写向量、检索召回）需带 Qdrant+Embedding 实例验证：首启看日志 `🧠 [SkillsKB] skill 文档挂载完成`，再让插件开发代理实跑 `search_skill_docs(query, skill="gscore-plugin-development")`。

### 9.5 备注

1. **检索消费面**：当前只有 `plugin_developer_agent` 装配了 `search_skill_docs`（默认会用 plugin-dev 范围）。其余 skill（adapter / ai-core-api / deploy）已挂载+隔离、随时可检索——将来新增「适配器开发代理」等画像时把 `search_skill_docs` 加进其 `tool_names` 即可复用；如需主人格在聊天里直接答开发问题，也可把它给主人格（但仍与日常 RAG 隔离，仅显式调用）。
2. **`docs/skills/` 即契约**：放进该目录的任何子目录都会被挂载进知识库（开发文档语义）。非文档目录请勿放此处。
