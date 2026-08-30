# 二十二、为插件挂 Web 控制台页面

> 返回 [SKILL 主入口](../SKILL.md)

插件可以把自己的前端页挂到 Core，由 Hub 的 `/plugins` 页用按钮打开。打开后侧边栏收起，右侧用 iframe 嵌入；顶栏可返回。后端接口仍走 `/api/...`。

## 22.1 统一收口

```python
from gsuid_core.webconsole.plugin_page import register_plugin_page, PluginAPI, api_ok, api_fail
```

不要再自己 `app.mount` 静态目录，也不要新起 uvicorn。页面走注册表，API 走 `PluginAPI`（或原有 `@app.get("/api/...")`）。

## 22.2 挂一个前端页

```python
from pathlib import Path
from gsuid_core.webconsole.plugin_page import register_plugin_page

register_plugin_page(
    title="抽卡与角色管理",
    static_dir=Path(__file__).parent.parent / "web",
    page_id="console",  # → /plugin-pages/<plugin_id>/console/
    description="管理抽卡记录",
    confirm_message="即将打开插件提供的页面。",
    title_i18n={
        "zh-CN": "抽卡与角色管理",
        "en-US": "Gacha & Agents",
        "ja-JP": "ガチャとエージェント",
    },
    description_i18n={"en-US": "...", "ja-JP": "..."},
    confirm_message_i18n={"en-US": "...", "ja-JP": "..."},
)
```

约定：

| 项 | 规则 |
|----|------|
| `page_id` | `[a-z][a-z0-9_-]{0,63}`，不能以下划线开头 |
| `plugin` | 默认同调用栈推断（`gsuid_core/plugins/<目录>`），建议与 `Plugins(name=...)` 一致 |
| `static_dir` | 相对调用方文件目录，或绝对路径；必须含 `index.html` |
| URL | `/plugin-pages/<plugin_id>/<page_id>/`，`plugin_id` 为插件名小写 |
| 静态后缀 | html/js/css/json/图片/字体 等，**不提供** `.py` / `.env` |
| 热重载 | 框架会先 `unregister_plugin_pages` 再重新 import |

Hub `/plugins` 在插件标题右侧显示按钮；点开先确认（可勾选「下次不再弹出」），再进入 `/#/plugin-view/<plugin_id>/<page_id>`。

## 22.3 页面 i18n

静态目录下：

```
web/
  index.html
  app.js
  app.css
  locales/
    zh-CN.json
    en-US.json
    ja-JP.json
```

三语言 **leaf key 对齐**。Hub 打开 iframe 时带 `?locale=zh-CN|en-US|ja-JP`。页面引入 SDK：

```html
<script src="/plugin-pages/_sdk/gshub-plugin.js"></script>
```

SDK **源文件在 Hub** `public/gshub-plugin.js`（`pnpm build` 原样进 `dist/`，不参与 hash）。Core 从 `webconsole/dist`（或 `data/dist`）读出，再挂到上面这个稳定 URL。改 SDK 后要 rebuild Hub 并更新 Core 的 dist。

```js
GsHubPlugin.ready.then(() => {
  document.title = GsHubPlugin.t('title');
  GsHubPlugin.api('/api/myplugin/items').then(render);
});
```

SDK 提供：`locale` / `theme` / `style` / `token` / `t(key, params)` / `api(path, opts)` / `blob(path)` / `fetch(path, opts)` / **`onTheme(cb)`**。

`title` / `description` / `confirm_message` 的 i18n 给 **Hub 按钮和确认框** 用；页面正文走 `locales/*.json`。

## 22.3.1 跟随 Hub 亮暗主题

SDK 会给 `<html>` 打上 `.dark` / `data-theme` / `data-style`，并把 Hub 的 CSS 变量（`--background`、`--foreground`、`--card`、`--border`、`--primary` 等）写到页面根节点。

- 打开 iframe 时 query 里有 `theme=light|dark`（首屏）。
- Hub 在控制台里切换亮暗 / 配色时 **不会重载 iframe**，而是 `postMessage({ type: "gshub:theme", mode, style, vars })`。
- 插件页启动后会向父页发 `gshub:theme-request`，避免错过第一帧。

页面 CSS **优先用 Hub token**，不要写死整页黑底：

```css
body {
  background: hsl(var(--background));
  color: hsl(var(--foreground));
}
.panel {
  background: hsl(var(--card));
  border: 1px solid hsl(var(--border));
}
html.dark .panel { /* 仅暗色才需要的微调 */ }
```

可选：听主题变化做图表重绘等：

```js
GsHubPlugin.onTheme(function (theme) {
  // theme.mode === 'light' | 'dark'
});
```

完整参考：ZZZeroUID `web/app.css`（`--bg` 映射 `hsl(var(--background))`）。

## 22.4 挂 `/api` 接口（推荐 PluginAPI）

```python
from gsuid_core.webconsole.plugin_page import PluginAPI, api_ok, api_fail

api = PluginAPI()  # 前缀 /api/<plugin_id>，默认 Depends(require_auth)

@api.get("/players")
async def list_players():
    return api_ok([...])

@api.delete("/players/{uid}/gacha")
async def remove_gacha(uid: str):
    return api_fail("not found")
```

规则：

1. 路径必须在 `/api/` 下（`PluginAPI(prefix=...)` 会检查）。
2. 默认要登录；公开接口用 `PluginAPI(auth=False)`。
3. 响应继续用 `{status, msg, data}`，`api_ok` / `api_fail`。
4. 仍可用原来的 `from gsuid_core.webconsole.app_app import app` + `@app.get("/api/foo/...")`。

`<img>` / iframe 子资源不会带 `Authorization`。需要鉴权的图片用 SDK `blob()` 或 `?token=`（与插件 ICON 相同）。

完整参考：ZZZeroUID `zzzerouid_webconsole/` + `web/`。

## 22.5 反模式

- 不要 `FastAPI()` / `uvicorn.run` 另起端口。
- 不要把密钥写进静态 HTML。
- 不要把 `static_dir` 指到插件 Python 源码根目录。
- 不要用 `history[-n:]` 之类无关改动；本能力与 AI 前缀缓存无关。
