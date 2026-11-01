# 48. 人格市场 API - /api/persona-store

## 概述

人格市场 API 提供对远程人格目录（`persona_list.json`）的浏览、git 仓库整包导入、更新检查、同步与卸载能力。前端 `/persona-config` 的「从人格市场添加」入口基于这套接口。

**核心能力**：
- 获取远程人格市场列表（带本地导入状态 / 更新状态）
- 通过市场 ID 安装（白名单解析 link + branch）
- 通过任意 git URL 导入人格整包仓库
- 检查并同步远端更新（pull → 重新导入 → 对齐绑定与资源）
- 卸载（删除人格 + 清掉克隆仓库）

**模型要点**：
- 市场条目里的 `tags` / `alias` 只是展示元数据；真正决定可导入性的是仓库内的**固定整包结构**：`manifest.json`（`format=gsuid-persona-bundle`）+ `persona.md` + 可选资源（`avatar.png` / `image.png` / `config.json` / `persona.json` / `appearance.txt` / `voice_anchor.txt` / `audio.*`）+ `packs/<包名>/{图片,tags.json}`。
- 整包根可以在仓库根，也可以在**一级子目录**（如 `persona_sayu/persona_sayu/`），导入时自动定位。
- 克隆目录为 `data/ai_core/persona_repos/<repo名>`；导入后在人格目录写入 `persona_store.json` 来源标记（repo_url / branch / commit / packs）。
- **git 导入的人格只读**：内容、资源、persona.json、行为配置、表情包挂载一律以仓库为准，修改接口返回 status=1；仅 `scope` / `target_groups`（启用范围）允许本地修改，同步时保留。仓库拥有的表情包本身（改名 / 删除 / 成员增删）同样只读。
- 复制人格时摘除来源标记并带走表情包挂载，得到一份可自由编辑的独立副本。
- 表情包向量是派生数据：导入新记录时补写 Qdrant，失败由启动回填兜底，不随仓库搬运。

**认证方式**：所有 API 均需 `Authorization: Bearer <token>`；写操作（install / update / uninstall）需要管理员权限。

---

## 48.1 获取人格市场列表

```
GET /api/persona-store/list
```

**请求参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| tab | string | 否 | 页签：`all` / `installed` / `updates` |
| search | string | 否 | 按 ID / 描述 / 标签 / 别名模糊匹配 |
| limit | integer | 否 | 单页条数；**不传返回全量** |
| offset | integer | 否 | 偏移量，默认 0 |
| check_updates | integer | 否 | 为 1 时对已导入人格执行 git fetch 并比较 commit |

**响应**（成功）：

```json
{
  "status": 0,
  "msg": "ok",
  "data": {
    "personas": [
      {
        "id": "persona_sayu",
        "name": "早柚 AI 人格整包",
        "description": "早柚 AI 人格整包",
        "link": "https://github.com/KimigaiiWuyi/persona_sayu",
        "branch": "main",
        "avatar": "https://github.com/KimigaiiWuyi.png",
        "cover": "https://raw.githubusercontent.com/.../ICON.png",
        "type": "tip",
        "content": "普通",
        "info": "早柚 AI 人格整包",
        "installMsg": "",
        "tags": ["人设文档", "形象参考", "配置", "表情包"],
        "alias": ["persona_sayu", "早柚", "sayu"],
        "installed": true,
        "persona_name": "persona_sayu",
        "hasUpdate": false,
        "status": "installed"
      }
    ],
    "count": 1,
    "total_count": 1,
    "counts": {"all": 1, "installed": 1, "updates": 0},
    "limit": null,
    "offset": 0,
    "has_more": false
  }
}
```

## 48.2 已导入的 git 人格

```
GET /api/persona-store/installed
```

返回 `{persona_name, repo_url, repo_name, branch, commit, packs, installed_at}` 列表。

## 48.3 安装 / URL 导入

```
POST /api/persona-store/install/{persona_id}   # 白名单：从远程目录解析 link + branch
POST /api/persona-store/install-url            # body: {"url": "...", "branch": "main?"}
```

克隆到 `data/ai_core/persona_repos/<repo名>` → 定位整包根 → 按 bundle 结构导入（persona.md / 资源 / config.json / 表情包包）→ 写来源标记。仓库结构不符合整包格式时不落盘、清掉克隆。manifest 的 `persona_name` 若与现有人格同名且非本仓库导入（手工人格或其他仓库），拒绝导入并清掉克隆，避免覆盖本地数据。

## 48.4 更新检查与同步

```
POST /api/persona-store/check/{persona_name}   # → {"hasUpdate": bool}
POST /api/persona-store/update/{persona_name}
```

同步流程：`git pull` → 校验 manifest 人格名一致（改名拒绝同步）→ 重新导入 → 恢复本地 `scope`/`target_groups` → 删除仓库已移除的资源文件 → 解除已移除包的绑定、清理独占包内失效成员 → 更新来源标记 commit。

## 48.5 卸载

```
DELETE /api/persona-store/uninstall/{persona_name}
```

删除人格（含配置与绑定）并清掉对应克隆仓库。`DELETE /api/persona/{name}` 对 git 人格同样生效（内部走同一逻辑）。
