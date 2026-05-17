# 31. 版本信息 API - /api/version

本页接口均需要通过 WebConsole 鉴权。

## 31.1 获取框架版本与后端环境信息

```
GET /api/version
```

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "version": "0.10.4",
        "commit": "a1b2c3d",
        "python": {
            "version": "3.11.9",
            "implementation": "CPython",
            "compiler": "MSC v.1938 64 bit (AMD64)"
        },
        "platform": {
            "system": "Windows",
            "release": "11",
            "machine": "AMD64",
            "processor": "Intel64 Family 6 Model 154 Stepping 3, GenuineIntel"
        },
        "pid": 12345,
        "executable": "C:\\Python311\\python.exe",
        "dependencies": {
            "fastapi": "0.115.0",
            "uvicorn": "0.30.0",
            "pydantic": "2.12.2",
            "sqlalchemy": "2.0.35"
        }
    }
}
```

**字段说明**：

| 字段 | 类型 | 描述 |
|------|------|------|
| version | string | 框架版本号 |
| commit | string | 当前 git commit hash（7位短格式） |
| python | object | Python 运行时信息 |
| python.version | string | Python 版本号，如 `3.11.9` |
| python.implementation | string | Python 实现，如 `CPython`、`PyPy` |
| python.compiler | string | Python 编译器信息 |
| platform | object | 操作系统与硬件平台信息 |
| platform.system | string | 操作系统名称，如 `Windows`、`Linux`、`Darwin` |
| platform.release | string | 操作系统版本号 |
| platform.machine | string | 机器架构，如 `AMD64`、`x86_64`、`aarch64` |
| platform.processor | string | 处理器信息 |
| pid | integer | 当前后端进程 PID |
| executable | string | Python 解释器可执行文件路径 |
| dependencies | object | 关键依赖库版本 |
| dependencies.fastapi | string | FastAPI 版本 |
| dependencies.uvicorn | string | uvicorn 版本 |
| dependencies.pydantic | string | Pydantic 版本 |
| dependencies.sqlalchemy | string | SQLAlchemy 版本 |

## 31.2 获取当前 active_bot 列表与数量

```
GET /api/version/bots
```

用于获取当前 `gss.active_bot` 中保留的 Bot 实例列表、名称和数量。注意：`active_bot` 会保留短时间断线等待重连的 Bot 实例，因此 `connected` 字段用于表示该 Bot 当前是否仍有活跃 WebSocket 连接。

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "count": 2,
        "names": ["onebot", "qqgroup"],
        "bots": [
            {
                "name": "onebot",
                "ws_bot_id": "onebot",
                "bot_id": "onebot",
                "connected": true
            },
            {
                "name": "qqgroup",
                "ws_bot_id": "qqgroup",
                "bot_id": "qqgroup",
                "connected": false
            }
        ]
    }
}
```

**字段说明**：

| 字段 | 类型 | 描述 |
|------|------|------|
| count | integer | 当前 `gss.active_bot` 中 Bot 实例数量 |
| names | string[] | 当前 Bot 名称列表，即 `gss.active_bot` 的 key 列表 |
| bots | object[] | 当前 Bot 详情列表 |
| bots[].name | string | Bot 名称，等同于 `ws_bot_id` |
| bots[].ws_bot_id | string | WebSocket 连接维度的 Bot ID，即 `gss.active_bot` 的 key |
| bots[].bot_id | string | Bot 实例内部记录的 Bot ID |
| bots[].connected | boolean | 是否存在活跃 WebSocket 连接 |

## 31.3 获取当前 active_bot 数量

```
GET /api/version/bots/count
```

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "count": 2
    }
}
```

**字段说明**：

| 字段 | 类型 | 描述 |
|------|------|------|
| count | integer | 当前 `gss.active_bot` 中 Bot 实例数量 |

## 31.4 获取当前 active_bot 名称列表

```
GET /api/version/bots/names
```

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "names": ["onebot", "qqgroup"]
    }
}
```

**字段说明**：

| 字段 | 类型 | 描述 |
|------|------|------|
| names | string[] | 当前 Bot 名称列表，即 `gss.active_bot` 的 key 列表 |
