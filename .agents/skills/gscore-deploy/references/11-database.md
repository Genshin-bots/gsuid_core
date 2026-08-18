# 十一、数据库配置

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十、链接 Bot 适配器清单](./10-bots.md) · **下一章**：[十二、Docker 部署](./12-docker.md)

GsCore 默认用 **SQLite**，路径 `data/GsData.db`。需要多实例共享 / 大数据量 /
远程访问时切换 **MySQL** 或 **PostgreSQL**。

源码：[`gsuid_core/utils/database/base_models.py`](../../../gsuid_core/utils/database/base_models.py)
+ [`gsuid_core/utils/plugins_config/database_config.py`](../../../gsuid_core/utils/plugins_config/database_config.py)

> 用户文档：https://docs.sayu-bot.com/Advance/Database.html

## 11.1 支持的数据库

| 类型 | 状态 | 默认 | 说明 |
|------|------|------|------|
| **SQLite** | ✅ 完整 | 是 | 零配置，单文件 `data/GsData.db` |
| **MySQL / MariaDB** | ✅ 实验性 | 否 | 需要 `aiomysql` 或 `asyncmy` 驱动 |
| **PostgreSQL** | ⚠️ 代码已有 | 否 | 用户文档标注**暂不支持** |
| **自定义 URL** | ✅ | 否 | 完全自定义 SQLAlchemy URL |

## 11.2 SQLite（默认）

无需任何配置，首次启动自动在 `data/GsData.db` 建表。

**优势**：

- 零运维
- 单文件备份 / 迁移简单（直接 `cp`）
- 嵌入式部署友好

**限制**：

- 单写者（SQLite 默认串行化写）
- 不支持多实例
- 大表（百万行）性能下降

> **生产环境单实例 / 中小规模用 SQLite 完全 OK**。

## 11.3 切到 MySQL

### 11.3.1 准备 MySQL 服务

```sh
# Docker 跑 MySQL（仅作示例）
docker run -d --name mysql \
  -e MYSQL_ROOT_PASSWORD=yourpassword \
  -e MYSQL_DATABASE=GsData \
  -p 3306:3306 \
  mysql:8

# 或用现成的 MySQL / MariaDB
```

创建数据库 `GsData`（名字任意）：

```sql
CREATE DATABASE GsData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

### 11.3.2 装驱动

```sh
# aiomysql（推荐，PyMySQL 系）
uv pip install aiomysql

# 或 asyncmy（C 语言实现，更快）
uv pip install asyncmy
```

### 11.3.3 改 Core 配置

最方便：WebConsole → 插件管理 → 修改插件设定 → **GsCore 数据库配置**

或手动编辑 `core_config.json`：

```json
{
  "db_type": "MySql",
  "db_driver": "aiomysql",
  "db_host": "127.0.0.1",
  "db_port": "3306",
  "db_user": "root",
  "db_password": "yourpassword",
  "db_name": "GsData",
  "db_pool_size": 5,
  "db_echo": false,
  "db_pool_recycle": 1500,
  "db_custom_url": ""
}
```

字段说明见 [`database_config.py`](../../../gsuid_core/utils/plugins_config/database_config.py)：

| 字段 | 取值 |
|------|------|
| `db_type` | `SQLite` / `MySql` / `PostgreSQL` / `自定义` |
| `db_driver` | MySQL：`aiomysql` / `asyncmy` |
| `db_host` | 数据库主机 |
| `db_port` | 数据库端口（MySQL 3306 / PostgreSQL 5432） |
| `db_user` / `db_password` | 凭据 |
| `db_name` | 数据库名 |
| `db_pool_size` | 连接池大小（默认 5） |
| `db_echo` | 是否打印 SQL（debug 用） |
| `db_pool_recycle` | 连接回收秒数（默认 1500） |
| `db_custom_url` | 自定义 URL（`db_type=自定义` 时用） |

### 11.3.4 重启 Core

```sh
core重启
```

启动日志应能看到：

```
📀 [数据库] 开始初始化...
...
✅ 数据库表已就绪
```

### 11.3.5 验证

WebConsole → 数据表管理 → 应能看到 `web_user` 等表已建好。

## 11.4 PostgreSQL（实验性）

代码已实现（`base_models.py:89-93`），但用户文档标注**暂不支持**。如有需要：

```json
{
  "db_type": "PostgreSQL",
  "db_driver": "asyncpg",
  "db_host": "127.0.0.1",
  "db_port": "5432",
  "db_user": "postgres",
  "db_password": "yourpassword",
  "db_name": "gsdata"
}
```

装驱动：`uv pip install asyncpg`。**生产慎用**，等官方正式支持。

## 11.5 自定义 URL

`db_type=自定义` 时使用 `db_custom_url`：

```json
{
  "db_type": "自定义",
  "db_custom_url": "sqlite+aiosqlite:///E://GenshinUID//gsdata.db"
}
```

完整 SQLAlchemy URL 语法。

## 11.6 数据库迁移（SQLite → MySQL）

没有内置一键迁移工具，需要手动：

```sh
# 1. 停 Core
core关闭Core

# 2. 导出 SQLite
uv run python -c "
from sqlalchemy import create_engine
import sqlite3
src = sqlite3.connect('data/GsData.db')
dst = create_engine('mysql+pymysql://root:pwd@127.0.0.1/GsData')
for table in src.execute('SELECT name FROM sqlite_master WHERE type=\"table\"').fetchall():
    name = table[0]
    rows = src.execute(f'SELECT * FROM \"{name}\"').fetchall()
    if not rows:
        continue
    cols = [d[0] for d in src.execute(f'PRAGMA table_info(\"{name}\")').fetchall()]
    placeholders = ','.join(['%s'] * len(cols))
    with dst.begin() as conn:
        for row in rows:
            conn.execute(f'INSERT INTO {name} ({','.join(cols)}) VALUES ({placeholders})', row)
"

# 3. 按 §11.3 切到 MySQL
# 4. 启动 Core 验证
```

> **更稳妥的方案**：用 `mysqldump` / `pg_dump` 等工具，或者直接走 ORM 自带的迁移
> 工具（如 Alembic，但本项目暂未集成）。

## 11.7 备份

### 11.7.1 自动备份（WebConsole）

WebConsole → 备份管理 → 选路径 → 立即备份 / 定时备份

默认备份目录：`data/GsCore_BACKUP_PATH/`，文件命名 `GsData_BAK_<时间>.db`。

### 11.7.2 手动备份

```sh
# SQLite
cp data/GsData.db data/GsData.db.bak.$(date +%Y%m%d_%H%M%S)

# MySQL
mysqldump -uroot -p GsData > GsData_$(date +%Y%m%d_%H%M%S).sql
```

> 备份**前**停 Core 或确认无写入；Core 启动时表结构变更可能让旧备份不兼容。

### 11.7.3 自动备份配置

源码：[`gsuid_core/utils/plugins_config/backup_config.py`](../../../gsuid_core/utils/plugins_config/backup_config.py)

WebConsole 可设：

- 备份路径
- cron 表达式
- 最大保留份数

## 11.8 故障排查

| 现象 | 原因 / 解决 |
|------|--------------|
| 启动报 `No module named 'aiomysql'` | 没装驱动，`uv pip install aiomysql` |
| 启动报 `greenlet DLL load failed` | Windows 缺 msvc-runtime，`uv pip install greenlet msvc-runtime` |
| `Can't connect to MySQL server` | `db_host` / `db_port` 错 / 防火墙 / MySQL 没启 |
| `Access denied for user` | `db_user` / `db_password` 错 |
| `Unknown database 'GsData'` | 没建库，先 `CREATE DATABASE GsData` |
| 切库后表丢失 | Core 启动时按 SQLModel metadata 自动建表；MySQL 需库存在 |
| 中文乱码 | MySQL 连接串加 `?charset=utf8mb4`，库用 `utf8mb4_unicode_ci` |
| WebConsole 看不到表 | 切换数据库后**刷新页面**或重新登录 |
