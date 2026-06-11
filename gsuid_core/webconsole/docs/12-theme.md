# 12. 主题配置 API - /api/theme

主题配置接口用于读取/保存前端主题设置（明暗、纯色/毛玻璃、主题色、毛玻璃强度、卡片透明度、背景图、图标色、主题预设、语言）。

## 12.0 字段说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `mode` | string | 是 | `dark` | 颜色模式：`light` / `dark` |
| `style` | string | 是 | `glassmorphism` | 界面风格：`solid` / `glassmorphism` |
| `color` | string | 是 | `red` | 主题色：`red` / `orchid` / `blue` / `green` / `orange` / `pink` |
| `icon_color` | string | 是 | `colored` | 图标色：`white` / `black` / `colored` |
| `background_image` | string \| null | 是 | `null` | 背景图 URL 或 dataURL，`null` 表示无背景 |
| `blur_intensity` | number | 是 | `12` | 毛玻璃模糊强度（像素，0-24） |
| `card_opacity` | number | **是（新增）** | `25` | 卡片不透明度（百分比 0-100），同时作用于纯色/毛玻璃 |
| `theme_preset` | string | 是 | `default` | 主题预设：`default` / `shadcn` |
| `language` | string | 是 | `zh-CN` | 前端语言：`zh-CN` / `en-US` / `ja-JP` |

> ⚠️ **新增字段 `card_opacity`**（number，范围 0-100，可选；缺省时回退到 25）。前端 `/api/theme/config` 持久化时已带该字段，缺它将导致前端透明度无法跨设备/会话保留。

---

## 12.1 获取主题配置

```
GET /api/theme/config
```

**认证**：需要（`Authorization` 头，参见 [01-auth.md](./01-auth.md)）

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "mode": "dark",
        "style": "glassmorphism",
        "color": "red",
        "icon_color": "colored",
        "background_image": null,
        "blur_intensity": 12,
        "card_opacity": 25,
        "theme_preset": "default",
        "language": "zh-CN"
    }
}
```

**说明**：
- 读取时若存储中没有 `card_opacity`（旧版本后端持久化的数据），返回时会自动补默认值 `25`，避免前端拿到 `undefined` 触发回退逻辑。
- 其他缺失字段同样会与默认值合并，确保响应体始终包含完整字段集。

---

## 12.2 保存主题配置

```
POST /api/theme/config
```

**认证**：需要（`Authorization` 头，参见 [01-auth.md](./01-auth.md)）

**Content-Type**：`application/json`

**请求体**：
```json
{
    "mode": "dark",
    "style": "glassmorphism",
    "color": "blue",
    "icon_color": "colored",
    "background_image": null,
    "blur_intensity": 16,
    "card_opacity": 50,
    "theme_preset": "default",
    "language": "zh-CN"
}
```

**响应**：
```json
{ "status": 0, "msg": "ok" }
```

**校验规则**：
- `blur_intensity`：整数，范围 0-24（前端会做夹紧，后端也会在 Pydantic 层校验并二次夹紧）。
- `card_opacity`：整数，范围 0-100。越界将返回 422。
- `background_image`：可为 `null`，或任意字符串（URL / dataURL）。
- 其余枚举字段未在服务端做枚举校验，由前端表单约束；如传入非法值，存储会原样保留，前端回退到默认值。

---

## 12.3 兼容性说明

- **新增字段**：仅 `card_opacity`。建议前后端协同：
  1. 读取时若存储中没有该字段，返回时补默认值 `25`，避免前端拿到 `undefined` 触发回退逻辑（已实现）。
  2. 写入时将 `card_opacity` 持久化到现有主题配置存储（与 `blur_intensity` 一同存放为 `int`）。
  3. 验证范围 0-100（前端会做夹紧，后端 Pydantic `Field(ge=0, le=100)` 也强制校验）。
- **不破坏旧客户端**：老版本前端不传 `card_opacity`，后端会在反序列化时使用默认值 `25` 并落盘。
- **CORS/认证**：与其他配置 API 一致，无特殊要求。
- **存储位置**：`gsuid_core.data_store.THEME_CONFIG_PATH` 指向的 JSON 文件（与 `core_config_api` 等共享同一数据目录）。
