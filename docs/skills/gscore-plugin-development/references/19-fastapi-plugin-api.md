# 十九、为插件添加 FastAPI 后端接口

> 本章教你如何让插件**复用框架的 FastAPI 实例**，把自己的 HTTP 接口挂到 WebConsole 同源后端，
> 自动获得认证 / 鉴权 / 路由前缀等基础设施，无需自己起一个独立的 Web 服务。

## 19.1 框架的 FastAPI 实例在哪里

GsCore 的 WebConsole 后端是**一个共享的 `fastapi.FastAPI` 实例**，由 [`gsuid_core/app_life.py:48`](../../../gsuid_core/app_life.py:48) 创建并通过 `@asynccontextmanager` 包裹 lifespan：

```python
# gsuid_core/app_life.py:48
app = FastAPI(lifespan=lifespan)
```

**对外导出路径**：

```
gsuid_core.app_life.app                       ← 真正创建 FastAPI 的位置
    └─ gsuid_core.webconsole.app_app.app      ← webconsole 子包统一再导出
            └─ 所有 webconsole/*_api.py 都 `from gsuid_core.webconsole.app_app import app`
```

> **约定**：**任何 webconsole / 插件 API 文件都从 [`gsuid_core.webconsole.app_app`](../../../gsuid_core/webconsole/app_app.py:1) import `app`**——这样无论未来 `app_life.py` 是否重命名实现细节，业务代码都不用改。

## 19.2 最简示例：3 行代码加一个 GET 接口

在插件目录（例如 `MyPlugin/MyPlugin/myplugin_api.py`）里：

```python
from fastapi import Request
from gsuid_core.webconsole.app_app import app

@app.get("/api/myplugin/hello")
async def hello(request: Request) -> dict:
    return {"status": 0, "msg": "ok", "data": "hello from MyPlugin"}
```

> ⚠️ **关键**：文件名 / 函数名 / 路径前缀都**必须**与 `Plugins` 实例的 `name` 挂钩，否则重载时框架不知道你这个文件属于哪个插件。
> 推荐命名：`{prefix}_api.py` 或直接放在 `MyPlugin/myplugin_api/__init__.py`。

文件创建后，框架下次扫描/重载时就会**自动 import 它**，`@app.get(...)` 装饰器立即生效。

## 19.3 加鉴权（推荐）：复用 `require_auth`

WebConsole 已有一套 **Bearer Token 鉴权**（`Authorization: Bearer <token>` Header 或 `?token=` query 参数），所有官方 API 都走 `require_auth` 这个 FastAPI Dependency。

```python
from typing import Dict
from fastapi import Request, Depends
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth  # ← 复用

@app.get("/api/myplugin/secret")
async def secret(
    request: Request,
    _user: Dict = Depends(require_auth),     # ← 注入鉴权依赖
):
    return {"status": 0, "msg": "ok", "data": {"user": _user}}
```

`require_auth` 来源于 [`gsuid_core/webconsole/web_api.py:59`](../../../gsuid_core/webconsole/web_api.py:59)：

```python
def require_auth(authorization: str | None = Header(default=None), token: str | None = None):
    """FastAPI dependency for authentication"""
    user_data = verify_token(authorization, token)
    if not user_data:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="未授权，请先登录")
    return user_data
```

> **统一响应格式**（与 webconsole 所有 API 一致）：
>
> ```json
> { "status": 0, "msg": "ok", "data": { ... } }
> ```
>
> - `status: 0` 成功，`1` 业务失败，其他值为具体错误码
> - `msg` 给前端展示的描述
> - `data` 真正的业务数据

## 19.4 自实现鉴权（不依赖框架 `require_auth`）

> **适用场景**：你的接口**只给自己 / 特定客户端**用（webhook、IoT 设备上报、第三方回调、CI 触发），
> 不想用 WebConsole 的浏览器登录态；或者**想完全掌控鉴权逻辑**（自定义 token 格式、过期策略、签名校验）。
>
> **核心思路**：**写一个自己的 FastAPI Dependency**，签名和 `require_auth` 一样是个普通函数，
> 通过 `Depends(...)` 挂到任意接口上即可——**完全可以不引入** `require_auth` / `verify_token`。
>
> FastAPI 文档原文："A dependency is a function that can take all the same parameters that a path operation function can take."，所以写法和 `require_auth` 完全等价，只是**你**是作者。

### 19.4.1 方案 A：API Key（最简单）

在请求头里塞一个固定字符串（如 `X-Api-Key`），比对通过即放行。
适合**内网 / 单机 / 个人使用**的端点。

```python
# MyPlugin/MyPlugin/myplugin_api.py
from typing import Dict, Any
from fastapi import Header, HTTPException, Depends

from gsuid_core.webconsole.app_app import app
from MyPlugin.config import MyPluginConfig   # 你插件自己的配置

_cfg = MyPluginConfig.get_config("MyPlugin").api_key  # 用户在 WebConsole 配置的密钥


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """校验 X-Api-Key，校验通过返回 key 本身（可作 caller 标识）。"""
    if not _cfg:
        raise HTTPException(status_code=500, detail="服务端未配置 API Key")
    if not x_api_key or x_api_key != _cfg:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return x_api_key


@app.post("/api/myplugin/device/report")
async def device_report(
    payload: Dict[str, Any],
    _key: str = Depends(require_api_key),
):
    # _key 已经是校验过的 key，可以继续当作 caller 标识写入日志 / 数据库
    return {"status": 0, "msg": "ok", "data": {"caller": _key}}
```

> ⚠️ **安全提醒**：API Key 明文出现在 header 里，**必须全程 HTTPS**；否则会被中间人直接抄走。
> 若你的部署是内网 / 局域网，可以接受明文；公网建议升级到方案 B（HMAC 签名）或方案 C（JWT）。

### 19.4.2 方案 B：自定义 Header + 共享密钥（HMAC 签名 + 时间窗）

适合**轻量内网**场景：客户端在 header 里塞 `X-MyPlugin-Token: sha256(secret + ts)`，服务端在 60s 时间窗内校验，能防**重放攻击**。

```python
# MyPlugin/MyPlugin/myplugin_api.py
import hmac
import hashlib
import time
from typing import Dict, Any
from fastapi import Header, HTTPException, Depends

from gsuid_core.webconsole.app_app import app
from MyPlugin.config import MyPluginConfig

_SHARED_SECRET = MyPluginConfig.get_config("MyPlugin").shared_secret
_TS_WINDOW = 60  # 秒


def _make_token(secret: str, ts: int) -> str:
    return hmac.new(secret.encode(), str(ts).encode(), hashlib.sha256).hexdigest()


async def require_hmac_token(
    x_myplugin_token: str = Header(...),
    x_myplugin_ts: int = Header(...),
) -> int:
    """校验 X-Myplugin-Token / X-Myplugin-Ts，防止重放。"""
    if not _SHARED_SECRET:
        raise HTTPException(status_code=500, detail="服务端未配置 shared_secret")
    if abs(int(time.time()) - x_myplugin_ts) > _TS_WINDOW:
        raise HTTPException(status_code=401, detail="timestamp out of window")
    expected = _make_token(_SHARED_SECRET, x_myplugin_ts)
    if not hmac.compare_digest(expected, x_myplugin_token):
        raise HTTPException(status_code=401, detail="invalid token")
    return x_myplugin_ts  # 注入给 handler，用作请求时间戳


@app.post("/api/myplugin/ingest")
async def ingest(
    payload: Dict[str, Any],
    ts: int = Depends(require_hmac_token),
):
    return {"status": 0, "msg": "ok", "data": {"ts": ts}}
```

> 客户端伪代码：
> ```python
> ts = int(time.time())
> token = hmac.new(secret, str(ts).encode(), sha256).hexdigest()
> httpx.post(url, json=payload, headers={
>     "X-Myplugin-Ts": str(ts),
>     "X-Myplugin-Token": token,
> })
> ```

### 19.4.3 方案 C：自建 JWT（不依赖第三方库）

如果不想额外装 `pyjwt`，可以用 `hashlib + hmac` 自己做 HS256 JWT。
**适用**：你自己想控制 token 载荷（claims）、想跟 WebConsole 用户体系**完全解耦**。

```python
# MyPlugin/MyPlugin/auth_jwt.py
import json
import hmac
import hashlib
import base64
import time
from typing import Any, Dict, Optional


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_jwt(payload: Dict[str, Any], secret: str, ttl: int = 3600) -> str:
    """签发 HS256 JWT，exp / iat 自动塞进 payload。"""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {**payload, "exp": int(time.time()) + ttl, "iat": int(time.time())}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64url(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def verify_jwt(token: str, secret: str) -> Optional[Dict[str, Any]]:
    """验证 HS256 JWT，返回 payload；失败返回 None。"""
    try:
        h, p, sig = token.split(".")
    except ValueError:
        return None
    expected = _b64url(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    payload = json.loads(_b64url_decode(p))
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload
```

```python
# MyPlugin/MyPlugin/myplugin_api.py
from typing import Any, Dict
from fastapi import Header, HTTPException, Depends, Body

from gsuid_core.webconsole.app_app import app
from MyPlugin.auth_jwt import sign_jwt, verify_jwt
from MyPlugin.config import MyPluginConfig

_JWT_SECRET = MyPluginConfig.get_config("MyPlugin").jwt_secret  # 32+ 字符串


async def require_jwt(authorization: str = Header(...)) -> Dict[str, Any]:
    """校验 Authorization: Bearer <jwt>，注入 payload。"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bad auth header")
    token = authorization[7:]
    payload = verify_jwt(token, _JWT_SECRET)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return payload


# 自己的登录端点（签发 token）
@app.post("/api/myplugin/login")
async def login(data: Dict[str, str] = Body(...)):
    username = data.get("u", "")
    password = data.get("p", "")
    if not username or not password:
        return {"status": 1, "msg": "missing credentials"}
    if not _check_user_in_myplugin_db(username, password):
        return {"status": 1, "msg": "invalid credentials"}
    token = sign_jwt({"sub": username, "role": "user"}, _JWT_SECRET, ttl=3600)
    return {"status": 0, "msg": "ok", "data": {"token": token}}


# 需要鉴权的业务端点
@app.get("/api/myplugin/me")
async def me(_claims: Dict[str, Any] = Depends(require_jwt)):
    return {"status": 0, "msg": "ok", "data": {"user": _claims.get("sub")}}
```

> 💡 **如果想用 `pyjwt`**（更标准、payload 不限 HS256 也能 RS256），
> 直接在插件 `pyproject.toml` 加 `pyjwt` 依赖，逻辑和上面等价：
> ```python
> import jwt
> payload = jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])
> ```

### 19.4.4 方案 D：与 WebConsole 鉴权**共存**（同接口允许两种调用方）

很多场景既要让 **WebConsole 浏览器登录**能调，又要让**机器 / 第三方带自己的 token** 也能调。
做法：**自己写一个 dependency，先尝试 `require_auth`，失败再尝试自实现 token**。

```python
# MyPlugin/MyPlugin/myplugin_api.py
from typing import Any, Dict
from fastapi import Header, HTTPException, Depends, Request

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import verify_token
from MyPlugin.auth_jwt import verify_jwt
from MyPlugin.config import MyPluginConfig

_JWT_SECRET = MyPluginConfig.get_config("MyPlugin").jwt_secret


async def require_auth_or_jwt(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Dict[str, Any]:
    """先认 WebConsole Token，认不出来再认 MyPlugin 自签 JWT。"""
    # 1) 优先 WebConsole Bearer
    user = verify_token(authorization)
    if user:
        return {"source": "webconsole", "user": user}

    # 2) 再尝试自签 JWT（不强制 Header 必填，避免一个请求同时带两套 token）
    raw_auth = request.headers.get("Authorization") or ""
    if raw_auth.startswith("Bearer "):
        token = raw_auth[7:]
        payload = verify_jwt(token, _JWT_SECRET)
        if payload:
            return {"source": "myplugin_jwt", "claims": payload}

    raise HTTPException(status_code=401, detail="未授权")


@app.get("/api/myplugin/unified")
async def unified(_caller: Dict[str, Any] = Depends(require_auth_or_jwt)):
    return {"status": 0, "msg": "ok", "data": {"caller": _caller}}
```

> 这种"宽松放行"思路**只适合内部受信调用**——公网建议**严格**写一个 dependency，只认一种鉴权。
> 如果你只想要"WebConsole 登录" + "机器 token"两者中**任一**通过，就用这个方案。

### 19.4.5 自实现鉴权的最佳实践

1. **不要把密钥写死在代码里**——用 [`MyPluginConfig`](./references/04-config-management.md)（对应 WebConsole 配置页）让用户填。
2. **依赖函数最好是 `async`**——FastAPI 0.95+ 会检测同步依赖里的阻塞 I/O；保持 async 避免被警告。
3. **失败统一抛 `HTTPException(status_code=401, ...)`**——FastAPI 会自动序列化为 JSON，前端能直接看到。
4. **永远使用 `hmac.compare_digest` 比对**——`==` 在 Python 里**不是**常量时间，存在时序攻击。
5. **注入的返回值是给你 handler 用的**——`Depends(require_jwt)` 的返回值会作为 handler 的参数，建议命名 `_claims` / `_user` 表明"已经被校验过"。
6. **不要自己解析 `Authorization` 头的多种格式**——要么 `Bearer`，要么自创 `MyPlugin-Token`，保持单一规则。
7. **日志只记"谁调用 + 时间 + 路径"，不要记原始 token**——token 落到日志基本等于泄露。
8. **依赖要写幂等**——同一个请求中同一个 `Depends` 多次出现，FastAPI 默认只调用一次并缓存结果，所以可以放心地"叠加"依赖。

## 19.5 完整示例：CRUD + 鉴权 + 路径参数

```python
# MyPlugin/MyPlugin/myplugin_api/__init__.py
from typing import Dict, List, Optional, Any
from fastapi import Request, Body, Depends
from pydantic import BaseModel

from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


# ── Request / Response 模型 ─────────────────────────────────
class MyItem(BaseModel):
    id: Optional[int] = None
    name: str
    value: int = 0


# ── 路由：列表 ──────────────────────────────────────────────
@app.get("/api/myplugin/items")
async def list_items(
    request: Request,
    q: Optional[str] = None,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    items: List[MyItem] = await fetch_items_from_db(q)
    return {"status": 0, "msg": "ok", "data": items}


# ── 路由：详情 ──────────────────────────────────────────────
@app.get("/api/myplugin/items/{item_id}")
async def get_item(
    request: Request,
    item_id: int,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    item = await fetch_item_by_id(item_id)
    if item is None:
        return {"status": 1, "msg": f"item {item_id} not found", "data": None}
    return {"status": 0, "msg": "ok", "data": item}


# ── 路由：创建 ──────────────────────────────────────────────
@app.post("/api/myplugin/items")
async def create_item(
    request: Request,
    payload: MyItem = Body(...),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    new_id = await insert_item_to_db(payload.name, payload.value)
    return {"status": 0, "msg": "created", "data": {"id": new_id}}


# ── 路由：删除（带权限检查） ──────────────────────────────
@app.delete("/api/myplugin/items/{item_id}")
async def delete_item(
    request: Request,
    item_id: int,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    if "admin" not in _user.get("roles", []):
        return {"status": 1, "msg": "需要管理员权限", "data": None}
    await delete_item_from_db(item_id)
    return {"status": 0, "msg": "deleted", "data": None}
```

前端可直接 `GET /api/myplugin/items?q=xxx`、`POST /api/myplugin/items` 等。

## 19.6 注册时机：放在插件 `__init__.py` 即可

框架的"嵌套加载"机制（见 [一、插件基础结构 §1.2](./references/01-plugin-basics.md#12-入口三件套)）会自动 import 内层包的全部子模块。因此**最简单**的做法是：在插件的 `__init__.py` 里加一行 `from . import myplugin_api`。

```python
# MyPlugin/MyPlugin/__init__.py
from gsuid_core.sv import Plugins

Plugins(name="MyPlugin", force_prefix=["mp"], ...)

# 触发 API 模块加载（import 副作用即注册路由）
from MyPlugin import myplugin_api  # noqa: F401
```

也可以借助 [`__full__.py`](./references/01-plugin-basics.md#12-入口三件套) 启用"扫描子目录全部导入"——框架会自动发现 `myplugin_api/` 子包并 import。

> **不要**在 `on_core_start` 之前去访问 `app.routes`——核心 `app_life` 在 `on_core_start_before` 之后才挂载到 `core.startup`，那时路由才会被 `uvicorn` 真正监听。

## 19.7 常见操作速查

| 想做的事 | 推荐写法 |
|---------|--------|
| 加 GET 接口 | `@app.get("/api/myplugin/xxx")` |
| 加 POST 接口 | `@app.post("/api/myplugin/xxx")` |
| 加 PUT / PATCH / DELETE | 同上换装饰器名 |
| 接收 JSON body | `payload: SomeModel = Body(...)` |
| 接收 query 参数 | `arg: str = Query(...)` 或直接当函数参数 |
| 接收路径参数 | `@app.get("/api/.../{item_id}")` + 函数签名 `item_id: int` |
| 鉴权（复用框架） | `_user: Dict = Depends(require_auth)` |
| 鉴权（自实现 API Key） | `_key: str = Depends(require_api_key)` |
| 鉴权（自实现 HMAC）   | `_ts: int = Depends(require_hmac_token)` |
| 鉴权（自实现 JWT）    | `_claims: Dict = Depends(require_jwt)` |
| 鉴权（多源共存）      | 自写一个 `Depends(...)`，先 `verify_token` 再自校（见 §19.4.4） |
| 鉴权 + 角色 | `Depends(require_auth)` + 自己检查 `_user["roles"]` |
| 上传文件 | `file: UploadFile = File(...)` |
| 异步后台任务 | `bg: BackgroundTasks, ...; bg.add_task(coro, ...)` |
| 流式响应 | 返回 `StreamingResponse` / `Response` |

## 19.8 路由前缀与命名规范（强烈建议）

WebConsole 已有 **40+ 个官方 API**，全部以 `/api/<domain>/...` 为前缀（如 `/api/plugins/...`、`/api/auth/...`）。

**插件自定义 API 的推荐前缀**：

```
/api/<插件名（小写）>/...
```

例如 MyPlugin 的 API 全部以 `/api/myplugin/...` 开头，避免与官方接口冲突。

## 19.9 注意事项 / 反模式

1. **不要**在自己的插件里 `import uvicorn` / `app.run()` 启新进程——会与框架的端口冲突。
2. **不要**新建 `FastAPI()` 实例——框架只有一个共享 app，再 new 一个会监听不到。
3. **不要**直接操作 `app.routes` 做增删——热重载时框架会重新扫你的模块。
4. **不要**在 API 处理函数里 `await asyncio.sleep(...)` 长时间阻塞——会卡住整个 web 事件循环；超过 5s 的任务请用 `BackgroundTasks` 或丢到 [六、定时任务与订阅](./references/06-scheduler-and-subscribe.md) 的 `scheduler.add_job`。
5. **鉴权默认不强制**：`@app.get(...)` 不加 `Depends(...)` 就是公开接口；**敏感操作必须加**。如果想完全摆脱框架的 `require_auth`，参考 §19.4 自实现鉴权。
6. **不要**把插件的数据库 / 配置路径硬编码——用 [`十六、常用工具模块速查 §16.1 get_res_path`](./references/16-common-utilities.md#161-资源存储get_res_path)。
7. **统一异常返回**：API 内部 `try/except` 后请返回 `{"status": 1, "msg": str(e), "data": None}`，让前端按 status 统一处理。
8. **避免阻塞型同步库**——和 [十七、代码规范红线 §17.1](./references/17-code-redlines.md#171-禁止事项) 一致：禁止 `requests` / 同步 `open()`，全用 `httpx.AsyncClient` / `aiofiles`。

## 19.10 端到端流程图

```
插件目录
└─ MyPlugin/MyPlugin/
   ├─ __init__.py             ← Plugins(name=...) + from . import myplugin_api
   └─ myplugin_api/
      └─ __init__.py          ← 定义 @app.get / @app.post（import 时即注册路由）

启动时序
   1. core 启动 → gsuid_core.app_life.app 创建
   2. 框架扫描 plugins/MyPlugin/，发现 __nest__.py
   3. 框架 import MyPlugin.MyPlugin → 执行 __init__.py
   4. Plugins(...) 实例化 → 内层 __init__.py 触发 myplugin_api 子模块 import
   5. myplugin_api 内的 @app.get / @app.post 把路由挂到共享 app
   6. core 启动 uvicorn → 监听 :8765 (默认) → 路由生效
   7. 前端 / 第三方 HTTP 客户端可访问 https://host:8765/api/myplugin/xxx
```

## 19.11 关联参考

- 启动 / 关闭钩子时序：[七、生命周期钩子 §7.1](./references/07-lifecycle-hooks.md#71-钩子总览)
- 数据库模型 + Web 控制台注册：[五、数据库操作 §5.5](./references/05-database.md#55-把数据库表注册到-web-控制台)
- 通用工具 `get_res_path` / `error_reply`：[十六、常用工具模块速查](./references/16-common-utilities.md)
- 异步规范 / 红线：[十七、代码规范红线 §17.1](./references/17-code-redlines.md#171-禁止事项)
- 框架 FastAPI 实例源码：[`gsuid_core/app_life.py:48`](../../../gsuid_core/app_life.py:48) · [`gsuid_core/webconsole/app_app.py`](../../../gsuid_core/webconsole/app_app.py:1) · [`gsuid_core/webconsole/web_api.py:59`](../../../gsuid_core/webconsole/web_api.py:59)
- 官方 40+ API 模块（参考风格）：`gsuid_core/webconsole/{plugins_api,auth_api,scheduler_api,...}.py`
- 自实现鉴权基础组件：`hashlib` / `hmac` / `secrets`（标准库，无需装包）
- 可选：第三方 JWT 库 [`pyjwt`](https://pyjwt.readthedocs.io/)（需在 `pyproject.toml` 加依赖）
- FastAPI 官方 Dependency 教程：<https://fastapi.tiangolo.com/tutorial/dependencies/>
