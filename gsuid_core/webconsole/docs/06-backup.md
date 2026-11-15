# 6. 备份 API - /api/backup

## 6.1 获取备份文件列表
```
GET /api/backup/files
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "fileName": "backup_20240101.zip",
            "downloadUrl": "/api/backup/download?file_id=backup_20240101.zip",
            "deleteUrl": "/api/backup/backup_20240101.zip",
            "size": 1234567,
            "created": "2024-01-01T00:00:00"
        }
    ]
}
```

---

## 6.2 创建备份
```
POST /api/backup/create
```

---

## 6.3 下载备份文件
```
GET /api/backup/download?file_id=xxx
```

---

## 6.4 删除备份文件
```
DELETE /api/backup/{file_id}
```

---

## 6.5 获取备份配置
```
GET /api/backup/config
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "backup_time": "02:00",
        "backup_dir": ["config", "data"],
        "backup_method": ["local"],
        "webdav_url": "",
        "webdav_username": "",
        "webdav_password": ""
    }
}
```

---

## 6.6 保存备份配置
```
POST /api/backup/config
```

**请求体**：
```json
{
    "backup_time": "03:00",
    "backup_dir_added": ["plugin-res"],
    "backup_dir_removed": ["data"],
    "backup_method": ["local", "webdav"],
    "webdav_url": "https://...",
    "webdav_username": "user",
    "webdav_password": "pass"
}
```

- `backup_dir` 三选一（可都省略）：`backup_dir`（旧式整表覆盖）、`backup_dir_added` +
  `backup_dir_removed`（增量，推荐——并发下不会整表覆盖别人的改动；两族字段同给时增量优先）。
- 全部路径归一为相对 `data/` 的 posix 路径存储；越界路径返回 `status: 1`。
- `backup_dir` 允许为空列表（清空勾选）；增量字段允许两侧都为空。

**响应**：
```json
{
    "status": 0,
    "msg": "备份配置已保存",
    "data": {"backup_dir": ["config", "plugin-res"]}
}
```
`data.backup_dir` 是保存后的最终相对路径列表；旧版本 Core 响应没有 `data`，
客户端可据此回退到整表 `backup_dir` 提交。

---

## 6.7 获取备份文件树（分页）
```
GET /api/backup/file-tree
```

列出 `data/` 下**某一目录的直接子项**，供备份勾选。不再一次性返回整棵树。

**Query 参数**：
- `path`: 相对 `data/` 的目录，空 = 根
- `sort`: `size`（默认，按体积降序）、`count`（按文件数降序）或 `hint`（按备份建议优先级：用户数据 → 配置 → 含建议的目录 → 无建议 → 可重新下载，同级再按大小降序、名称）
- `offset`: 默认 0
- `limit`: 默认 100，**上限 100**（继续拉下一页请加大 `offset`，不要一次要全量）

缓存 / 临时目录不会出现：`IMAGE_TEMP`、`DATA_CACHE_PATH`、`data_cache`、`GsCore_BACKUP_PATH`、`dist`、`__pycache__`、`node_modules`、`.` 开头。

扫盘在线程池，不堵 Core 主循环。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "path": "plugin-res",
        "name": "plugin-res",
        "type": "directory",
        "size_bytes": 123456,
        "file_count": 120,
        "child_total": 120,
        "offset": 0,
        "limit": 100,
        "truncated": true,
        "omitted_count": 20,
        "sort": "size",
        "children": [
            {
                "id": "plugin-res/cache-000.bin",
                "name": "cache-000.bin",
                "type": "file",
                "path": "plugin-res/cache-000.bin",
                "size_bytes": 122880,
                "file_count": 1,
                "has_children": false,
                "hint": null,
                "hint_count": 0
            }
        ]
    }
}
```

非法路径 `{status: 1}`。前端「还有 N 项」用 `offset = 已加载条数` 再请求同一 `path`。

**节点新增字段**（旧版本 Core 不返回，前端按 `null` / `0` 兜底）：
- `hint`：该节点精确命中备份建议时为 `{kind, plugin, desc, default_selected}`，否则 `null`。`kind ∈ {"user_data", "config", "regenerable"}`
- `hint_count`：该目录下严格子孙中登记的建议数，文件恒为 `0`

---

## 6.8 获取备份建议路径
```
GET /api/backup/hints
```

插件用 `gsuid_core.utils.backup.hints.register_backup_path` 登记的建议列表。请求会先应用一次默认勾选（`user_data` 与核心配置自动加入 `backup_dir`，只加一次，用户取消后不回填）。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "path": "GsData.db",
            "plugin": "GsCore",
            "kind": "user_data",
            "desc": "主数据库：UID 绑定、Cookie、订阅等",
            "default_selected": true,
            "exists": true,
            "selected": true,
            "size_bytes": 8388608,
            "file_count": 1
        }
    ]
}
```

`path` 是相对 `data/` 的 posix 路径；`selected` 表示当前 `backup_dir` 是否已覆盖该建议。

## 6.9 备份清单（gscore_backup_manifest.json）

`0.12.1` 起的备份包是流式写出的 zip（先写 `<file_id>-<date>.zip.part`，成功后换名）。
zip 根目录最后写入 `gscore_backup_manifest.json`（`ZIP_STORED`，不解压即可读）：

```json
{
    "format": "gscore-backup",
    "format_version": 1,
    "created_at": "2026-11-16T00:55:00+08:00",
    "core_version": "0.12.1",
    "platform": "win32",
    "file_id": "NowFile-2026-11-16",
    "roots": ["GsData.db", "GenshinUID/players"],
    "hints": [{"path": "GenshinUID/players", "plugin": "GenshinUID", "kind": "user_data"}],
    "file_count": 1234,
    "total_bytes": 56789
}
```

`roots` 只含实际打包到的选择项（相对 `data/` 的 posix）。没有清单的 zip 按旧版备份处理。

`backup_dir` 新写入一律存**相对 `data/` 的 posix 路径**；旧值里的本机绝对路径、
外系统绝对路径（含盘符 / 反斜杠）仍能解析——后者取最后一个 `data` 段之后的部分。

## 6.10 还原预检
```
POST /api/backup/restore/preview        {"file_id": "NowFile-2026-11-16.zip"}
POST /api/backup/restore/preview-entries {"entries": [...], "manifest": {...} | null}
```

`preview` 校验 `backup_path` 内的 zip；`preview-entries` 收客户端本地读出的中央目录
（每条 `{path, size, is_dir, is_symlink, encrypted}`，上限 500k，manifest 不合法当无清单）。

**响应 `data`（`RestorePreview`）**：

| 字段 | 说明 |
|------|------|
| `manifest` | 清单对象或 `null` |
| `warnings` | 提示列表 |
| `total_files` / `total_bytes` / `new_files` | 可还原统计 |
| `conflict_count` / `conflict_same_size` | 冲突数 / 其中大小相同的数量 |
| `conflicts` | `{path, backup_size, local_size}`，最多 200 条 |
| `conflict_roots` | `{root, count}` 按第一段聚合，全量 |
| `rejected` / `rejected_count` | `{path, reason}`，最多 200 条 |
| `type_conflicts` / `type_conflict_count` | 类型冲突路径，最多 200 条 |
| `pending_db` | 含活 SQLite 主库，需重启生效 |
| `free_bytes` / `enough_space` | 磁盘余量 |
| `digest` | sha256(按序冲突路径 `\n` 连接)，apply 时回传 |
| `can_restore` | 无 rejected / type_conflict 且空间够 |

任一被拒绝条目（绝对路径、`..`、非法字符 / 保留名、symlink、加密、大小写重名、
落在备份目录内等）都会阻止还原；`gscore_backup_manifest.json` 与 `GsData.db-wal/-shm`
不还原（不计拒绝）。

## 6.11 分块上传

```
POST /api/backup/upload/init                  {"file_name": "a.zip", "size": 123}
PUT  /api/backup/upload/{upload_id}?offset=N  body = 原始字节
POST /api/backup/upload/{upload_id}/complete
```

- `init` 校验空间并清理 24h 以上的 `.uploads/*.part`，返回 `{upload_id, chunk_size}`（8 MiB）。
- `PUT` 的 `offset` 必须等于服务器已收字节；等于 `已收 - 本块长度` 视为重试直接成功；
  单块 ≤ chunk_size，超过声明总量拒绝。响应 `data.received` 为当前已收字节。
- `complete` 校验大小一致且能被 zipfile 打开，落盘为
  `backup_path/upload-<安全化stem>-<date>.zip`（重名加序号），返回 `{file_id}`。

## 6.12 执行还原
```
POST /api/backup/restore/apply    {"file_id": "...", "mode": "overwrite" | "skip", "digest": "..."}
```

在后台线程跑（与备份互斥）。`overwrite` 前先把将被覆盖的本地文件打进
`backup_path/pre-restore-<YYYYmmdd-HHMMSS>-<date>.zip`；`digest` 与当前冲突列表不一致时拒绝。
活 SQLite 主库只写 `GsData.db.restore-pending`，重启时自动换入（旧库归档到
`pre-restore-db-<ts>/`）。响应 `data`：`{written, overwritten, skipped, pending_db, pre_restore_file}`。
