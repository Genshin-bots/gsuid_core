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
| `sidebar_layout` | string | 是 | `floating` | 侧边栏布局：`floating`（悬浮卡片）/ `docked`（贴边分栏）/ `line`（仅分割线） |
| `border_radius` | number | 是 | `8` | 全局圆角强度（px，0–32），写入 CSS `--radius` |
| `ui_scale` | number | 是 | `97` | UI 字号缩放百分比（85–120），作用于 `html` font-size |
| `shadow_intensity` | number | 是 | `55` | 阴影强度百分比（0–200，0=关闭），写入 CSS `--shadow-strength`（0–2） |
| `sidebar_default_collapsed` | boolean | 是 | `false` | 侧边栏默认是否收起为仅图标 |

> ⚠️ **新增字段 `card_opacity`**（number，范围 0-100，可选；缺省时回退到 25）。前端 `/api/theme/config` 持久化时已带该字段，缺它将导致前端透明度无法跨设备/会话保留。
>
> ⚠️ **字段 `sidebar_layout`**（string，`floating` | `docked` | `line`，缺省 `floating`）。旧配置无此字段时读取会自动补默认值。
>
> ⚠️ **新增字段 `border_radius` / `ui_scale` / `shadow_intensity` / `sidebar_default_collapsed`**：旧配置缺失时读取会补默认值（8 / 97 / 55 / false）。用户未设置过这些项时（如更新后首次启动），生效的就是这组默认值。

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
        "language": "zh-CN",
        "sidebar_layout": "floating",
        "border_radius": 8,
        "ui_scale": 97,
        "shadow_intensity": 55,
        "sidebar_default_collapsed": false
    }
}
```

**说明**：
- 读取时若存储中没有 `card_opacity`（旧版本后端持久化的数据），返回时会自动补默认值 `25`，避免前端拿到 `undefined` 触发回退逻辑。
- 读取时若存储中没有 `sidebar_layout` / `border_radius` / `ui_scale` / `sidebar_default_collapsed`，返回时会自动补默认值。
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
    "language": "zh-CN",
    "sidebar_layout": "floating",
    "border_radius": 8,
    "ui_scale": 97,
    "shadow_intensity": 55,
    "sidebar_default_collapsed": false
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

---

## 12.4 主题预设（保存 / 列表 / 应用 / 删除）

为了支持「多套主题切换」场景，新增主题预设（preset）能力。预设分两层：

| 来源 | 路径 | 写入权限 | 说明 |
|------|------|----------|------|
| **内置主题** | `gsuid_core/webconsole/themes_builtin/*.json`（包内） | 只读 | 随 gsuid_core 发布的出厂主题，前端列表中带 `is_builtin=true` |
| **用户主题** | `gsuid_core.data_store.THEME_CONFIGS_PATH/*.json`（用户数据目录） | 可写 | 用户通过 API 保存的自定义主题 |

- **文件名规则**：`{name}.json`，`name` 支持任意 Unicode 字符（含中文/日文/韩文），长度 1-64 个字符（按 Unicode 字符数计，非字节数）；禁止包含路径分隔符 `/ \`、Windows 保留字符 `< > : " | ? *`、控制字符，禁止以 `.` 开头或结尾。
- **应用语义**：`apply` 操作把预设文件的内容写入当前活动主题配置（`theme_config.json`），前端刷新即生效；内置和用户预设都支持应用。
- **保护规则**：内置主题名是保留名（reserved），不允许通过 `save` 覆盖，也不允许通过 `delete` 删除。如需基于内置主题定制，请使用新名称（如 `我的纯色质感`）保存。

### 12.4.1 获取主题预设列表

```
GET /api/theme/presets
```

**认证**：无需（公开接口，主题面板加载时调用）。

**响应**：
```json
{
  "status": 0,
  "msg": "ok",
  "data": {
    "user_presets_path": ".../data/themes",
    "builtin_presets_path": ".../gsuid_core/webconsole/themes_builtin",
    "presets": [
      {
        "name": "黑夜街道",
        "filename": "黑夜街道.json",
        "source": "builtin",
        "size_bytes": 248,
        "mtime": 1718952200.0,
        "is_active": true,
        "valid": true,
        "is_builtin": true,
        "config": {
          "mode": "dark",
          "style": "glassmorphism",
          "color": "blue",
          "icon_color": "colored",
          "background_image": "https://cdn.pixabay.com/photo/2024/05/26/15/27/anime-8788959_1280.jpg",
          "blur_intensity": 7,
          "card_opacity": 55,
          "theme_preset": "shadcn",
          "language": "zh-CN"
        }
      },
      {
        "name": "暗夜玻璃",
        "filename": "暗夜玻璃.json",
        "source": "user",
        "size_bytes": 312,
        "mtime": 1718952312.0,
        "is_active": false,
        "valid": true,
        "is_builtin": false,
        "config": {
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
      }
    ]
  }
}
```

字段说明：
- `presets[].source`：来源标记，`"builtin"` 或 `"user"`。前端应据此区分展示样式（内置主题禁用删除/编辑按钮）。
- `presets[].is_builtin`：与 `source` 同步的布尔标记，方便不支持枚举比对的前端按位使用。
- `presets[].is_active`：与当前活动主题配置（合并默认值后）逐字段相等则为 `true`。
- `presets[].valid`：预设 JSON 是否能被正确解析（解析失败时仍会展示，但 `valid=false`，前端可提示修复）。
- `presets[].config`（**新增**）：当 `valid=true` 时附带合并默认值后的完整 ThemeConfig，供前端在卡片上做背景图预览、主题色徽标等富展示；至少需要 `config.background_image` / `config.color` / `config.mode`。当 `valid=false` 时字段省略，前端走占位渐变渲染即可。
- `user_presets_path` / `builtin_presets_path`：两类预设的绝对路径，便于前端在调试面板中展示。
- 排序：内置优先（按文件名排序），其次用户预设（按文件名排序）。

---

### 12.4.2 保存主题预设

```
POST /api/theme/presets/save
```

**认证**：需要（`Authorization` 头，参见 [01-auth.md](./01-auth.md)）

**Content-Type**：`application/json`

**请求体**：
```json
{
  "name": "暗夜玻璃",
  "overwrite": false,
  "config": {
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
}
```

字段说明：
- `name`（必填）：预设名称，校验规则见上文（支持中文，例如 `暗夜玻璃`）。**不允许与内置主题重名**。
- `config`（可选）：要保存的主题配置；若省略，则保存**当前活动主题配置**（不存在时回退到默认值）。
- `overwrite`（可选，默认 `false`）：同名用户预设已存在时是否覆盖；为 `false` 时返回 `status=1`。

**响应**：
```json
{ "status": 0, "msg": "ok", "data": { "name": "暗夜玻璃", "filename": "暗夜玻璃.json", "source": "user" } }
```

错误：
- `name` 不合法 → `status=1`，msg 说明原因（包含非法字符 / 路径分隔符 / 长度越界等）。非法字符包括：路径分隔符 `/ \`、Windows 保留字符 `< > : " | ? *`、控制字符、首尾点号。
- `name` 与内置主题重名 → `status=1`，msg：`'xxx' 是内置主题名，不可被用户预设占用。请使用其他名称保存自定义版本。`
- 同名用户预设且 `overwrite=false` → `status=1`，提示需要开启覆盖。
- 写入失败 → `status=1`，附异常信息。

---

### 12.4.3 应用主题预设

```
POST /api/theme/presets/apply
```

**认证**：需要

**请求体**：
```json
{ "name": "黑夜街道" }
```

**行为**：
1. 先在 `themes_builtin/{name}.json`（内置）查找；若不存在再去 `themes/{name}.json`（用户）查找；
2. 读取到的内容与默认值合并；
3. 二次夹紧 `blur_intensity(0-24)` / `card_opacity(0-100)`；
4. 覆盖写入 `theme_config.json`（即 `/api/theme/config` 的活动配置）。

**响应**：
```json
{
  "status": 0,
  "msg": "ok",
  "data": {
    "name": "黑夜街道",
    "source": "builtin",
    "config": {
      "mode": "dark",
      "style": "glassmorphism",
      "color": "blue",
      "icon_color": "colored",
      "background_image": "https://cdn.pixabay.com/photo/2024/05/26/15/27/anime-8788959_1280.jpg",
      "blur_intensity": 7,
      "card_opacity": 55,
      "theme_preset": "shadcn",
      "language": "zh-CN"
    }
  }
}
```

`data.source` 标识本次应用读自哪一层（`"builtin"` 或 `"user"`），便于前端在调试面板中展示。

错误（统一为 HTTP 200 + `status=1` 信封）：
- `name` 不合法 → `status=1`，msg 说明原因。
- 预设文件不存在（内置和用户都查不到）→ `status=1`，msg：`主题预设 'xxx' 不存在`。
- 预设文件 JSON 损坏 → `status=1`，msg 说明读取失败。

---

### 12.4.4 删除主题预设

```
DELETE /api/theme/presets/{name}
```

**认证**：需要

**路径参数**：
- `name`：预设名称，校验规则同上。

**响应**：
```json
{ "status": 0, "msg": "ok", "data": { "name": "暗夜玻璃", "source": "user" } }
```

错误（统一为 HTTP 200 + `status=1` 信封）：
- 名称非法 → `status=1`，msg 说明原因。
- 与内置主题重名 → `status=1`，msg：`'xxx' 是内置主题，不允许删除`。
- 用户预设不存在 → `status=1`，msg：`主题预设 'xxx' 不存在`。

---

### 12.4.5 预设存储位置与安全

- **用户目录**：`gsuid_core.data_store.THEME_CONFIGS_PATH`，由 `get_res_path(...)` 自动创建。
- **内置目录**：`gsuid_core/webconsole/themes_builtin/`，随包发布，只读。修改/删除内置文件需要重新发布包，不通过 API 暴露。
- **路径穿越防护**：预设名禁止包含 `/` `\` `..`、Windows 保留字符、控制字符、首尾点号；写入前用 `Path.resolve().relative_to(base)` 二次校验。
- **不会清理活动配置**：删除用户预设只删除 `themes/{name}.json`，不会触碰 `theme_config.json`。
- **活动配置 vs 预设**：二者是「当前生效配置」与「命名预设」的关系，互不影响；`apply` 是单向覆盖（应用预设会覆盖当前活动配置，但不会反向写回预设）。即使预设位于只读的内置目录，应用依然会把内容复制一份写入用户可写的活动配置。
- **同名冲突策略**：保留名机制（reserved names）保证内置主题不会被用户 save 覆盖、不会被 delete 误删；如需基于内置主题定制，请使用新名称（如 `我的暗夜玻璃`）保存。

### 12.4.6 当前内置主题清单

当前随包发布的内置主题位于 `gsuid_core/webconsole/themes_builtin/`，由 gsuid_core 维护：

| 名称 | 风格 | 模式 | 说明 |
|------|------|------|------|
| `纯色质感` | glassmorphism | light | 浅色毛玻璃纯色背景，无背景图 |
| `随机老婆` | glassmorphism | light | 调用 `paugram` 随机壁纸接口，每天一张 |
| `清澈波纹` | glassmorphism | light | 浅色毛玻璃，Unsplash 蓝色水波纹，shadcn 预设 |
| `绫华` | glassmorphism | light | 浅色毛玻璃，orchid 主题色，神里绫华壁纸 |
| `鬼针草` | glassmorphism | light | 浅色毛玻璃，pink 主题色，近乎无模糊 |
| `初音未来` | glassmorphism | dark  | 深色毛玻璃，初音未来主题壁纸 |
| `磨砂岩石` | glassmorphism | dark  | 深色毛玻璃，Unsplash 岩石纹理，shadcn 预设 |
| `黑夜街道` | glassmorphism | dark  | 深色毛玻璃，配 Pixabay 二次元夜景 |

如需新增内置主题：在该目录下追加 `xxx.json`，文件名（不含扩展名）即视为新保留名，PR 审核后随下一版本发布。
