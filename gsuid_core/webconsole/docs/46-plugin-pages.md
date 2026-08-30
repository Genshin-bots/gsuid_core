# 46. 插件页面 - /plugin-pages 与 /api/plugin-pages

插件通过 `register_plugin_page` 登记前端静态页；Core 统一挂载，Hub `/plugins` 读取列表里的 `pages` 字段显示跳转按钮。

## 46.1 列出已挂载页面

```
GET /api/plugin-pages
```

需登录。响应 `data` 为数组：

```json
[
  {
    "id": "console",
    "plugin": "ZZZeroUID",
    "plugin_id": "zzzerouid",
    "path": "/plugin-pages/zzzerouid/console/",
    "title": { "zh-CN": "抽卡与角色管理", "en-US": "Gacha & Agents", "ja-JP": "ガチャとエージェント" },
    "description": { "zh-CN": "...", "en-US": "...", "ja-JP": "..." },
    "confirm_message": { "zh-CN": "...", "en-US": "...", "ja-JP": "..." },
    "icon": "layout-dashboard"
  }
]
```

同一字段也出现在 `GET /api/plugins/list` 与 `GET /api/plugins/{name}` 的 `pages` 数组中，Hub 主路径只读列表接口。

## 46.2 静态页

```
GET /plugin-pages/{plugin_id}/{page_id}/
GET /plugin-pages/{plugin_id}/{page_id}/{file}
GET /plugin-pages/_sdk/gshub-plugin.js   ← Hub public/gshub-plugin.js，经 pnpm build 进 dist
```

静态资源本身不强制登录（与 `/app` 前端包相同）；**数据接口必须** `/api/...` + `require_auth`。只允许常见 Web 后缀，路径走 `safe_join`。

Hub iframe 推荐带 query：`locale` / `theme` / `style` / `token`。

切主题时 Hub 向 iframe `postMessage`：

```json
{ "type": "gshub:theme", "mode": "dark", "style": "glassmorphism", "iconColor": "colored", "vars": { "--background": "240 5% 10%" } }
```

插件页 SDK 收到后更新 `html.dark` 与 CSS 变量，不整页刷新。页面可回发 `{ "type": "gshub:theme-request" }` 要一份当前主题。

## 46.3 插件作者

见插件开发 SKILL [§22](../../../.agents/skills/gscore-plugin-development/references/22-plugin-pages.md)。
