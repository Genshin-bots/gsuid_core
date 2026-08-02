# TAKUMI_HTML_GUIDE

> 面向 agent 的 Takumi / pytakumi HTML 写作指南。
>
> 目标：让 agent 生成的 HTML 能被本项目稳定渲染成适合 IM 发送的图片。
>
> 最后验证：2026-07-28，`pytakumi==0.1.0`，Windows + 项目内置 `MiSans-Bold.ttf`。

---

## 1. 心智模型：Takumi 不是浏览器

Takumi / pytakumi 是一个「HTML/CSS → 位图」的离线渲染引擎，适合把结构化内容渲染成图片发送到 IM。

它不是 Chromium / WebView：

- 没有 JavaScript。
- 没有浏览器扩展 CSSOM。
- 不能指望完整浏览器排版行为。
- 不会自动使用系统字体；只有显式注册到 `Renderer` 的字体可用。
- 渲染结果受已注册字体、已验证 CSS 能力、项目封装参数影响。

因此，写 HTML 时要把它当成一个「支持 Flexbox 的静态海报排版引擎」，而不是浏览器。

---

## 2. 项目渲染入口：优先使用封装，不要裸调 pytakumi

项目中已有共享渲染器，会注册：

- `MiSans`：中文主字体，来自 `gsuid_core/utils/fonts/MiSans-Bold.ttf`。
- `Mono`：等宽字体，自动查找 Consolas / Cascadia Mono / Menlo / DejaVu Sans Mono 等；找不到时回退 MiSans。

所以中文内容必须走项目封装，否则可能出现中文豆腐块、缺字或代码不等宽。

### 2.1 常用 API

```python
from gsuid_core.utils.html_render import (
    render_html_to_bytes,
    render_md_to_bytes,
    render_text_to_bytes,
)
```

#### HTML → 图片

```python
img: bytes = await render_html_to_bytes(
    html,
    max_width=720,          # IM 卡片推荐 640~800
    dpi=96,                 # 需要更清晰可用 192，相当于 2x
    default_font_size=14,   # 裸 HTML 片段建议 14+
    allow_refit=True,       # 高度随内容自适应
    image_format="png",
    lang="zh",
)
```

#### Markdown → 图片

```python
img = await render_md_to_bytes(md, max_width=720)
```

#### 纯文本 → 图片

```python
img = await render_text_to_bytes(text, max_width=720)
```

---

## 3. 优先使用 IM 模板

如果内容属于常见卡片类型，优先使用：

```python
from gsuid_core.utils.html_render import im_templates
```

可用模板：

| 模板 | 适合场景 |
|---|---|
| `summary_card` | 总结、复盘、要点列表 |
| `ranking_card` | 排行榜、Top N |
| `comparison_card` | 方案对比、功能对比 |
| `quote_card` | 引用、评价、金句 |
| `metrics_card` | 指标面板、数据统计 |
| `notice_card` | 公告、通知、风险提示 |
| `steps_card` | 步骤、流程、操作指引 |
| `code_card` | 代码片段、日志片段 |

示例：

```python
from gsuid_core.utils.html_render.im_templates import render_summary_card

img = await render_summary_card(
    title="今日要点",
    points=[
        "完成 pytakumi 迁移",
        "回归测试全部通过",
    ],
    footer="Mavis · 2026-07-28",
)
```

只有当现有模板不能表达内容时，再手写 HTML。

---

## 4. 推荐 HTML 骨架

手写 HTML 时，使用完整文档结构 + `<style>`：

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      width: 100%;
      background: linear-gradient(180deg, #101828 0%, #141d30 100%);
      font-family: "MiSans", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
      color: #e9eef7;
    }
    /* 暗底标题必须显式浅色，勿只写字号靠继承 */
    h1, h2, .title, .headline, .section-title {
      color: #edf4ff;
      font-weight: 800;
    }
    .card {
      width: 100%;
      padding: 32px;
    }
  </style>
</head>
<body>
  <div class="card">
    内容
  </div>
</body>
</html>
```

说明：

- `body { width: 100%; }` 会占满 `render_html_to_bytes(max_width=...)` 指定的宽度。
- `allow_refit=True` 时高度按内容自适应，适合 IM 长图。
- 用户内容必须 HTML 转义：

```python
import html
safe = html.escape(user_text, quote=True)
```

---

## 5. 已验证支持的 CSS 能力

以下能力在 `pytakumi==0.1.0` 下经过实际渲染验证。

### 5.1 布局

推荐程度：`flexbox > grid > inline-block`。

| 能力 | 状态 | 建议 |
|---|---:|---|
| `display:flex` | ✅ | 首选布局方式 |
| `flex-direction:row/column` | ✅ | 行、列都可以 |
| `justify-content` | ✅ | 主轴对齐 |
| `align-items` | ✅ | 交叉轴对齐 |
| `flex:1` | ✅ | 自适应剩余空间 |
| `gap` | ✅ | 推荐代替 margin 做间距 |
| `flex-wrap` | ✅ | 可用于标签流 |
| `display:grid` | ✅ | 基础 `grid-template-columns` 可用，但卡片仍建议 flex |
| `display:inline-block` | ✅ | 可用于行内块 |
| `position:relative` | ✅ | 常用 |
| `position:absolute` | ✅ | 可用于角标、装饰 |
| `display:none` | ✅ | 可隐藏元素 |

### 5.2 尺寸

| 能力 | 状态 |
|---|---:|
| `width` / `height` | ✅ |
| 百分比宽度，如 `width:50%` | ✅ |
| `min-width` / `max-width` | ✅ |
| `min-height` / `max-height` | ✅ |
| `box-sizing:border-box` | ✅，强烈建议全局设置 |

### 5.3 间距、边框、圆角、裁剪

| 能力 | 状态 |
|---|---:|
| `padding` | ✅ |
| `margin` | ✅ |
| `border` | ✅ |
| `border-bottom` 等单边边框 | ✅ |
| `border-style:dashed` | ✅ |
| `border-radius` | ✅ |
| `overflow:hidden` | ✅，可配合圆角裁剪 |

### 5.4 背景与视觉效果

| 能力 | 状态 |
|---|---:|
| 纯色背景 | ✅ |
| `linear-gradient` | ✅ |
| `radial-gradient` | ✅ |
| `box-shadow` | ✅ |
| `text-shadow` | ✅ |
| `opacity` | ✅ |
| `transform:rotate(...)` | ✅ |

### 5.5 文字排版

| 能力 | 状态 |
|---|---:|
| `font-family` | ✅，但只能使用已注册字体 |
| `font-size` | ✅ |
| `font-weight` | ✅ |
| `line-height` | ✅ |
| `letter-spacing` | ✅ |
| `text-transform:uppercase` | ✅ |
| `text-align:left/center/right` | ✅ |
| `text-decoration:underline` | ✅ |
| `text-decoration:line-through` | ✅ |
| `white-space:pre-wrap` | ✅，代码块必备 |
| `word-break:break-word` | ✅ |
| `overflow-wrap:anywhere` | ✅ |

### 5.6 其他元素

| 能力 | 状态 |
|---|---:|
| `<img src="data:image/png;base64,...">` | ✅ |
| `<br>` | ✅ |
| `<hr>` | ✅ |
| `<style>` 标签 | ✅ |
| CSS variables，如 `var(--accent)` | ✅ |

---

## 6. 不支持或不要用的能力

### 6.1 表格：写原生 `<table>` / GFM 表，不要手写 `display:table`

布局引擎本身**没有** CSS table 模型（`display:table` / `table-row` / `table-cell` 不会形成真正列对齐）。

但 **pytakumi 支持表格内容**：

- Markdown：`md_to_pic` / GFM `| ... |` 表会经 `rewrite_tables_for_takumi` 改成 `.md-table` flex 网格
- HTML：`render_html_to_image` 对原生 `<table>` 同样改写并注入最小 flex 样式

**推荐**（让库改写）：

```html
<table>
  <tr><th>名称</th><th>涨跌</th></tr>
  <tr><td>长江电力</td><td>+5.48%</td></tr>
</table>
```

**不要**手写 CSS table 布局：

```html
<div style="display:table">
  <div style="display:table-row">
    <div style="display:table-cell">A</div>
  </div>
</div>
```

若自己用 flex 画表也可以，与库改写二选一即可。

### 6.2 不要用 float

`float` 实测不会产生文字环绕，可能导致重叠。

不要用：

```html
<div style="float:left;width:70px;height:70px"></div>
<p>文字环绕</p>
```

应该用 flex：

```html
<div style="display:flex;gap:12px;align-items:flex-start">
  <div style="width:70px;height:70px;flex:none"></div>
  <div>文字内容</div>
</div>
```

### 6.3 不要依赖原生列表标记

`<ul>` / `<ol>` 的 bullet 和数字标记实测不会渲染，只有缩进。

不要依赖：

```html
<ul>
  <li>项目 A</li>
  <li>项目 B</li>
</ul>
```

应该自己画 bullet：

```html
<div style="display:flex;flex-direction:column;gap:10px">
  <div style="display:flex;gap:10px;align-items:flex-start">
    <div style="color:#3ec9a7;line-height:1.5">●</div>
    <div>项目 A</div>
  </div>
  <div style="display:flex;gap:10px;align-items:flex-start">
    <div style="color:#3ec9a7;line-height:1.5">●</div>
    <div>项目 B</div>
  </div>
</div>
```

### 6.4 不要依赖伪元素排版

`::before` / `::after` 的 `content` 可能渲染，但顺序和排版不稳定。需要前缀、角标、装饰时，用真实 DOM 节点 + flex / absolute。

### 6.5 不要使用外部资源（脚本 / 样式）

不要依赖：

- `<script>`
- `<link rel="stylesheet">`
- `@import`
- 浏览器插件能力

样式写在内联 `style` 属性或 `<style>` 标签中。

**图片例外（AI 出图路径）**：经 `render_html_to_image` 时，引擎会在渲染前把
`<img src>` / CSS `url(...)` 中的 `https://`、`icon:prefix/name`、`img_`、`res_` **自动嵌成
data URI**，agent **一次写完 HTML 即可**，不必先手动转 data URI。裸调 `render_html_to_bytes`
（插件代码路径）仍需自行提供 data URI。

---

## 7. 字体与字符

### 7.1 中文字体

正文必须使用包含 `MiSans` 的字体栈：

```css
font-family: "MiSans", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif;
```

项目封装会自动把 `MiSans` 加入 `font_families`，但 CSS 里也建议显式写上。

### 7.2 等宽字体

代码块使用项目注册的 `Mono`：

```css
font-family: "Mono", "JetBrains Mono", Consolas, Menlo, monospace;
white-space: pre-wrap;
```

示例：

```html
<pre style="
  font-family:'Mono',Consolas,Menlo,monospace;
  white-space:pre-wrap;
  background:#0f1626;
  border:1px solid #2b3850;
  border-radius:12px;
  padding:16px;
  color:#dbe4f5;
  font-size:13px;
  line-height:1.55;
">print("hello")</pre>
```

### 7.3 安全符号

MiSans 对部分符号覆盖不完整。优先使用以下符号：

```text
✓ ✔ √ ✕ × → ← ↑ ↓ ● ○ ◆ ★ · — ｜
```

避免使用：

```text
✗ ✘ ▸
```

以及大量未验证的 emoji。

项目模板层会把 `✗` / `✘` 归一化成 `✕`，但手写 HTML 时最好直接避免缺字符号。

---

## 8. 常见坑

### 8.1 空 HTML + 自适应高度会报错

如果 HTML 实际高度为 0，pytakumi 可能抛：

```text
RuntimeError: Invalid viewport: width or height cannot be 0
```

解决：

- 不要渲染空内容。
- 给根容器 `min-height`。
- 渲染前检查内容是否为空。

### 8.2 不要直接 `from pytakumi import html_to_pic`

业务代码直接裸调 pytakumi 会绕过项目共享 Renderer，导致：

- 中文没有注册 MiSans。
- 代码块没有注册 Mono。
- 字体缓存无法复用。

测试或探针脚本可以裸调，但项目业务路径应使用：

```python
from gsuid_core.utils.html_render import render_html_to_bytes
```

### 8.3 不要假设安装了 dev 版功能

例如 `rewrite_tables_for_takumi` 可能只存在于 pytakumi 源码仓库，不一定存在于已安装的 `pytakumi==0.1.0`。

代码中如使用可选功能，必须做能力探测或 try/except 降级。

### 8.4 IM 场景不要写太小的字

聊天窗口里图片会被压缩。建议：

- 正文 ≥ 14px。
- 标题 ≥ 24px。
- 辅助文字 ≥ 12px。
- 卡片宽度 640~800px。
- 需要高清可传 `dpi=192`。

### 8.5 修饰色被基类盖掉（class 优先级）

```css
/* 错：.item .value 特异性更高，.up/.down 永不生效 */
.item .value { color:#edf4ff; }
.up { color:#6ee7b7; }
/* 对 */
.item .value.up { color:#6ee7b7; }
.item .value.down { color:#fca5a5; }
```

### 8.6 语义色不要一页多套

同一页先定 3～4 个语义角色色并贯彻；暗底强调用浅 tint（`#fca5a5` `#6ee7b7` `#fde68a`）。
正文 / 底栏 / pill 不要各用一套互不相关的红绿金。

---

## 9. 可复制的最小卡片模板

```python
from gsuid_core.utils.html_render import render_html_to_bytes
import html

def build_card(title: str, lines: list[str]) -> str:
    safe_title = html.escape(title, quote=True)
    items = "".join(
        f'''
        <div style="display:flex;gap:10px;align-items:flex-start">
          <div style="color:#3ec9a7;line-height:1.5">●</div>
          <div style="flex:1;font-size:15px;line-height:1.55">{html.escape(line, quote=True)}</div>
        </div>
        '''
        for line in lines
    )

    return f'''
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
  width:100%;
  background:linear-gradient(180deg,#101828 0%,#141d30 100%);
  font-family:"MiSans","PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;
  color:#e9eef7;
}}
.card {{
  width:100%;
  padding:32px;
}}
.title {{
  font-size:28px;
  font-weight:800;
  line-height:1.3;
  margin-bottom:18px;
}}
.list {{
  display:flex;
  flex-direction:column;
  gap:12px;
}}
.footer {{
  margin-top:24px;
  padding-top:14px;
  border-top:1px solid #2b3850;
  color:#6b7890;
  font-size:13px;
}}
</style>
</head>
<body>
  <div class="card">
    <div class="title">{safe_title}</div>
    <div class="list">{items}</div>
    <div class="footer">Generated by gsuid_core</div>
  </div>
</body>
</html>
'''

async def render_card(title: str, lines: list[str]) -> bytes:
    return await render_html_to_bytes(
        build_card(title, lines),
        max_width=720,
        default_font_size=15,
    )
```

---

## 10. Agent 写 HTML 前检查清单

- [ ] 是否优先使用了 `im_templates`？
- [ ] 是否通过 `render_html_to_bytes` 渲染，而不是裸调 pytakumi？
- [ ] 中文字体栈是否包含 `MiSans`？
- [ ] 代码块是否使用 `"Mono", Consolas, Menlo, monospace`？
- [ ] 布局是否主要使用 flexbox？
- [ ] 是否避免了 `display:table`、`float`、原生 `<ul>` marker？
- [ ] 是否避免了远程资源和 JavaScript？
- [ ] 用户内容是否 HTML 转义？
- [ ] 是否避免使用 `✗`、`✘`、`▸` 等可能缺字的符号？
- [ ] 空内容是否有兜底，避免高度为 0？
- [ ] 字号是否适合 IM 小图阅读？

---

## 11. 相关代码与测试

- 渲染封装：`gsuid_core/utils/html_render/__init__.py`
- IM 模板：`gsuid_core/utils/html_render/im_templates.py`
- 迁移回归测试：`tests/test_pytakumi_migration.py`
- 模板测试：`tests/test_im_templates.py`

运行验证：

```powershell
python -m pytest tests/test_pytakumi_migration.py tests/test_im_templates.py
```
