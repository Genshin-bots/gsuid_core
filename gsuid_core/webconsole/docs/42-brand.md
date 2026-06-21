# 42. 品牌信息 API - /api/brand

品牌信息接口用于读取/管理前端品牌展示信息（标题、副标题、ICON），与 [12-theme.md](./12-theme.md) 保持一致：读取与 ICON 提供均为公开，登录页加载就需要 brand。

## 42.0 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `title` | string | 是 | `GsHub` | 品牌标题，≤ 64 字符 |
| `subtitle` | string | 是 | `早柚核心` | 品牌副标题，≤ 128 字符 |
| `icon_url` | string | 是 | `/api/brand/icon` | 品牌 ICON 访问 URL（固定不变，换图后仍是同一地址） |
| `icon_source` | string | 是 | `default` | ICON 来源标记：`user`（用户上传）/ `default`（默认打包图标） |
| `default` | object | 是 | `{ "icon": "ICON.png", "title": "GsHub", "subtitle": "早柚核心" }` | 默认品牌信息，便于前端做"恢复默认"按钮 |

> ⚠️ **前端 `<img>` 始终使用 `icon_url`**：上传新图后 `icon_url` 不变，但 `icon_source` 会从 `default` 变为 `user`，前端可据此提示"已自定义"。`GET /api/brand/icon` 会带 `Cache-Control: no-cache` 走 304 重校验，确保换图立即可见。

---

## 42.1 获取品牌信息

```
GET /api/brand
```

**认证**：无需（公开接口）。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "title": "GsHub",
        "subtitle": "早柚核心",
        "icon_url": "/api/brand/icon",
        "icon_source": "default",
        "default": {
            "icon": "ICON.png",
            "title": "GsHub",
            "subtitle": "早柚核心"
        }
    }
}
```

**说明**：
- 读取时若存储中没有 `title` / `subtitle`，会与默认值合并，确保响应体始终包含完整字段集。
- 配置文件每次实时读取，不缓存，确保 POST 后立即生效。
- 配置损坏（JSON 解析失败）不会阻断请求，回退到默认配置。

---

## 42.2 更新品牌信息（标题 / 副标题）

```
POST /api/brand
```

**认证**：需要（`Authorization` 头，参见 [01-auth.md](./01-auth.md)）

**Content-Type**：`application/json`

**请求体**：
```json
{
    "title": "GsHub Pro",
    "subtitle": "早柚核心 v2"
}
```

**校验规则**：
- `title`：字符串，0-64 字符；越界返回 422。
- `subtitle`：字符串，0-128 字符；越界返回 422。
- ICON 不在此接口处理，请走 `/api/brand/icon` 上传/删除。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "title": "GsHub Pro",
        "subtitle": "早柚核心 v2"
    }
}
```

---

## 42.3 上传品牌 ICON

```
POST /api/brand/icon
```

**认证**：需要

**Content-Type**：`multipart/form-data`

**请求字段**：
- `icon`：PNG 图片文件，≤ 2MB。

**校验规则**：
- 仅允许 PNG（`content_type=image/png` 或文件名以 `.png` 结尾作为兜底，前端可通过 `<input accept="image/png">` 提前过滤）。
- 文件大小 ≤ 2MB；超过返回 `status=1`，提示实际大小与上限。
- 文件为空返回 `status=1`，提示上传的图片为空。

**行为**：
- 保存到 `data/brand/ICON.png`，直接覆盖，下次 `GET /api/brand` 时 `icon_source` 变为 `user`。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "icon_url": "/api/brand/icon",
        "icon_source": "user",
        "size_bytes": 12345
    }
}
```

错误（统一为 HTTP 200 + `status=1` 信封）：
- 格式非 PNG → `msg: 不支持的图片格式: xxx，仅允许 PNG`。
- 空文件 → `msg: 上传的图畔为空`。
- 超过 2MB → `msg: 图片过大 (x.xxMB)，上限 2MB`。
- 读写失败 → `msg: 读取上传文件失败: <原因>` 或 `msg: 保存 ICON 失败: <原因>`。

---

## 42.4 删除品牌 ICON

```
DELETE /api/brand/icon
```

**认证**：需要

**行为**：
- 删除 `data/brand/ICON.png`，回退到默认 `CORE_PATH/ICON.png`。
- 已是默认状态（用户从未上传过）时直接返回成功，不报错。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "icon_source": "default"
    }
}
```

---

## 42.5 获取品牌 ICON

```
GET /api/brand/icon
```

**认证**：无需（公开接口，前端 `<img src>` 直接使用）。

**行为**：
1. 若 `data/brand/ICON.png` 存在：直接返回（用户上传）。
2. 否则回退到 `CORE_PATH/ICON.png`（默认打包图标，随 gsuid_core 发布，只读）。
3. 都没有则返回 `status=1`，前端可显示占位图标。

**响应头**：
- `Content-Type: image/png`
- `Cache-Control: no-cache`：浏览器按 ETag/Last-Modified 重校验，上传新图后能立即看到，未变则走 304 省流量。

---

## 42.6 兼容性 / 存储说明

- **存储位置**：
  - 品牌文本：`gsuid_core.data_store.BRAND_CONFIG_PATH` → `data/plugins_configs/brand.json`
  - 品牌 ICON：`gsuid_core.data_store.BRAND_ICON_PATH` → `data/brand/ICON.png`
  - 默认 ICON：`CORE_PATH/ICON.png`（包内，只读，随发布固定）
- **原子写入**：使用 `boltons.fileutils.atomic_save`，避免并发/断电导致配置损坏。
- **实时读取**：每次请求实时读取配置文件，不缓存，确保 POST 后立即生效。
- **接口风格**：读取与 ICON 提供均为公开，与 [12-theme.md](./12-theme.md) 保持一致。
- **上传约束**：仅 PNG、最大 2MB，与 [29-meme.md](./29-meme.md) / [01-auth.md](./01-auth.md) 现有上传限制保持一致。
- **失败兜底**：所有异常路径（读取/解析/序列化/上传/删除）均捕获后返回 `status=1` 信封，不会返回 500，确保前端总能拿到稳定结构。
