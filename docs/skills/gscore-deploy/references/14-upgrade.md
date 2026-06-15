# 十四、升级与热更新

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十三、AI 核心部署要点](./13-ai.md) · **下一章**：[十五、数据目录与路径速查](./15-data-layout.md)

GsCore 升级涉及**四个层面**：Core 自身代码、业务插件、AI 子系统、数据迁移。
本章按"何时升级 → 怎么升 → 怎么验证"组织。

## 14.1 Core 升级

### 14.1.1 升级方式

**方式 A：源码 git pull**

```sh
cd gsuid_core
git pull
uv sync               # 同步新依赖
core重启               # 或 systemctl restart gsuid_core
```

**方式 B：Docker（挂载模式）**

```sh
cd gsuid_core
git pull
docker-compose down
docker-compose up -d --build
```

**方式 C：Docker（全量模式）**

```sh
docker-compose -f docker-compose.bundle.yml pull
docker-compose -f docker-compose.bundle.yml up -d
```

### 14.1.2 升级前必读 CHANGELOG

每次升级前看 GitHub Releases / commit log 里的 **BREAKING CHANGES**。常见破坏性变更：

- 配置文件 schema 变 → Core 启动时会自动迁移，但**旧字段可能被丢**
- 数据库表结构变 → Core 启动时自动迁移（新增列 / 索引）
- API 行为变 → 上游 Bot 适配器可能也要升级

### 14.1.3 自动升级（推荐生产）

`core_config.json`：

```json
{
  "AutoUpdateCore": true,
  "AutoUpdatePlugins": true,
  "AutoRestartCore": false,
  "AutoUpdateCoreTime": ["3", "40"],
  "AutoUpdatePluginsTime": ["4", "10"],
  "AutoRestartCoreTime": ["4", "40"]
}
```

**最佳实践**：三个开关都开，配合 Docker `--restart always` 或 systemd
`Restart=always` 兜底。

> 注意：`AutoUpdate*` 只 `git pull` 代码，**不**运行 `uv sync` 或 `pip install`。
> 如果新版改了 `pyproject.toml`（加了新依赖），需要手动同步，否则 ImportError。

## 14.2 v3 → v4 数据迁移

> 用户文档：https://docs.sayu-bot.com/Advance/ExportAndImport.html

### 14.2.1 导出 v3 数据（NoneBot2 / Hoshino）

1. 升级到最新 GenshinUID v3
2. 用 master 账号在 Bot 里发 `导出v3数据`
3. 备份 `{Bot 目录}/data/GenshinUID/` 整个文件夹（HoshinoBot 是 `res/GenshinUID/`）

### 14.2.2 导入到 v4

1. 按 [二、源码部署安装](./02-install.md) 或 [十二、Docker 部署](./12-docker.md) 装好
   GsCore 和 GenshinUID v4
2. 把 v3 文件夹拷到 `{Core 目录}/data/GenshinUID/`
3. **删除**该文件夹内的 `config.json`（v4 schema 不一样）
4. 启动 Core 和 Bot
5. master 账号在 Bot 里发 `导入v3数据`
6. 重启

## 14.3 配置自动迁移

### 14.3.1 插件配置迁移（已自动）

旧版 `config.json` 里嵌一个大 `plugins` dict，新版拆成 `plugins_configs/<plugin>.json`。

**Core 启动时自动**（[`config.py:207-233`](../../../gsuid_core/config.py)）：

1. 检测 `config.json["plugins"]` 是否存在
2. 备份 `config.json` 为 `config_backup.json`
3. 每个插件的 dict 写入 `plugins_configs/<plugin>.json`
4. 移除 `config.json["plugins"]` 键
5. 写回 `config.json`

> 迁移是一次性的，**完成后 `config_backup.json` 可手动删**。

### 14.3.2 Core 配置 schema 迁移

`update_config()`（[`config.py:109-125`](../../../gsuid_core/config.py)）每次启动都会跑：

- 缺失字段 → 用 `CONFIG_DEFAULT` 补
- 缺失子字段 → 用 `CONFIG_DEFAULT[parent][child]` 补

> 这是**追加型**迁移，不会主动删除你额外加的字段。

### 14.3.3 AI 配置 schema 迁移

各 AI 配置类（`ai_config.py` / `database_config.py` / `pic_gen_config.py` 等）
的 `StringConfig` 也走类似的"缺失补默认"逻辑。**WebConsole 改完会写盘**，
下次启动读回。

## 14.4 数据库迁移

数据库表结构变更（新增列 / 索引）由 SQLModel 自动处理：

- **新增表**：Core 启动时根据 `BaseModel` 子类自动 `CREATE TABLE IF NOT EXISTS`
- **新增列**：Core 启动时根据模型定义自动 `ALTER TABLE ADD COLUMN`
- **删列**：**不会自动删**（SQLAlchemy 默认不 `DROP COLUMN`）
- **字段类型变更**：**不会自动改**

> 删列 / 改类型需要**手动 SQL**（参考 [`utils/database/startup.py`](../../../gsuid_core/utils/database/startup.py) 的 `exec_list` 用法）。

### 14.4.1 给已存在的表手动加列

如果插件定义了新列，部署者的数据库**没有这列**，可以：

```sh
# Core 启动会自动加（如果没有冲突）
# 或 WebConsole → 数据表管理 → 手动 ALTER TABLE
```

## 14.5 业务插件升级

### 14.5.1 命令行

| 命令 | 行为 |
|------|------|
| `core更新插件` | git pull 所有插件 |
| `core更新插件<name>` | 只更新一个 |
| `core强制更新插件<name>` | 丢弃本地修改 |
| `core强行强制更新插件<name>` | `git clean -xdf` 后 hard reset |
| `core刷新插件列表` | 重新拉 `plugin_list.json` |

### 14.5.2 自动升级（生产推荐）

`core_config.json`：

```json
{
  "AutoUpdatePlugins": true,
  "AutoUpdatePluginsTime": ["4", "10"],
  "AutoRestartCore": true,
  "AutoRestartCoreTime": ["4", "40"]
}
```

### 14.5.3 镜像源

详见 [九、§9.5](./09-plugins.md#95-git-镜像源切换)。

## 14.6 升级失败回滚

### 14.6.1 源码

```sh
cd gsuid_core
git log --oneline -10       # 找上一个稳定 commit
git reset --hard <commit>
uv sync
core重启
```

### 14.6.2 Docker（挂载模式）

同源码。

### 14.6.3 Docker（全量模式）

```sh
docker-compose -f docker-compose.bundle.yml pull <old_tag>
# 或
docker tag docker.cnb.cool/gscore-mirror/gsuid_core:<old> gsuid_core:rollback
docker run ... gsuid_core:rollback
```

### 14.6.4 数据回滚

```sh
# 备份先
cp data/GsData.db data/GsData.db.bak

# 出问题回滚
systemctl stop gsuid_core
cp data/GsData.db.bak data/GsData.db
systemctl start gsuid_core
```

## 14.7 升级 Checklist

- [ ] 备份 `data/`（最少 `GsData.db` + `config.json` + `core_config.json` + `plugins_configs/` + `ai_core/`）
- [ ] 读 CHANGELOG / commit log，看 BREAKING CHANGES
- [ ] 拉新代码 / 拉新镜像
- [ ] `uv sync` 同步依赖（Docker 通常自动）
- [ ] 重启 Core
- [ ] 看启动日志确认无报错
- [ ] 在 Bot 里发 `gs帮助` 确认插件加载
- [ ] 在 WebConsole 浏览数据确认迁移成功
- [ ] （如有）跑迁移脚本导入 v3 数据
