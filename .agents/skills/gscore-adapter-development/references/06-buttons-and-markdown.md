# 六、按钮与 Markdown 适配

按钮和 Markdown 是「富交互平台」专属能力。**不支持就 `logger.warning` 跳过，绝不要让整条消息失败。**

## 6.1 `buttons` 段的数据形态

下发时 `buttons` 段的 `data` 已被 `msgspec.to_builtins()` 转成纯数据，有**两种布局**：

```python
# 布局 A：扁平 List[dict] —— 由平台默认规则排版（如 QQ 默认两个一行）
buttons = [ {btn1}, {btn2}, {btn3}, {btn4} ]

# 布局 B：嵌套 List[List[dict]] —— 每个子列表是自定义的一行
buttons = [ [{btn1}, {btn2}], [{btn3}, {btn4}, {btn5}, {btn6}] ]
#            ↑ 第一行两个        ↑ 第二行四个
```

每个 `{btn}` 是 `Button` 转出的 dict，key 见 [§2.5](./02-data-structures.md)。**适配器必须同时处理
A、B 两种**，用 `isinstance(button, dict)` vs `isinstance(button, list)` 区分：

```python
for button in buttons:
    if isinstance(button, dict):       # 布局 A：单个按钮，按平台规则攒行
        flat.append(make_btn(button))
        if len(flat) >= 2:             # 例：每两个攒一行
            rows.append(flat); flat = []
    elif isinstance(button, list):     # 布局 B：这一项本身就是一整行
        rows.append([make_btn(b) for b in button])
```

> ⚠️ 解码后是 `dict`/`list`，不是 `Button` 对象，**用 `button['text']` 取值，不是 `button.text`**。
> 且权限字段拼写是 `permisson`（少个 i）。

## 6.2 `Button` 关键字段语义（适配时怎么用）

| 字段 | 适配要点 |
|------|---------|
| `text` | 按钮显示文字，直接用。 |
| `data` | **点击后作为"用户下一条消息"回传给机器人**的内容。映射到平台的 callback_data / input。 |
| `action` | `2`=命令按钮（点了等于用户发送 `data`）；`1`=回调按钮（点了触发回调事件，需适配器把回调再上报，见 [§8.3](./08-special-platforms.md)）；`0`=跳转链接；`-1`=自适应（多数平台按 `2` 处理）。 |
| `style` | `0` 灰 / `1` 蓝，映射到平台按钮主题色。 |
| `permisson` | 谁能按。映射到 QQ 频道的 Permission；多数平台无此概念，忽略。 |
| `pressed_text` | 按下后显示文字，部分平台支持（如 QQ 的 visited_label）。 |

## 6.3 各平台按钮映射实例

### QQ 官方（频道/群）—— 原生 InlineKeyboard

```python
def _bt(button: dict):
    from nonebot.adapters.qq.models import Action, Button, Permission, RenderData
    action = button["action"]
    if action == -1:           # 自适应 → 命令按钮
        action = 2
    enter = None
    if action == 1:            # 回调按钮在 QQ 上用 action=2 + enter=True 近似
        action = 2; enter = True
    return Button(
        render_data=RenderData(
            label=button["text"],
            visited_label=button["pressed_text"],
            style=button["style"],
        ),
        action=Action(
            type=action,
            permission=Permission(
                type=button["permisson"],
                specify_role_ids=button["specify_role_ids"],
                specify_user_ids=button["specify_user_ids"],
            ),
            enter=enter,
            unsupport_tips=button["unsupport_tips"],
            data=button["data"],
        ),
    )
```

### Telegram —— InlineKeyboardButton（callback_data）

```python
def _tg_kb(button: dict):
    from nonebot.adapters.telegram.model import InlineKeyboardButton
    return InlineKeyboardButton(text=button["text"], callback_data=button["data"])
# 布局：攒成 List[List[InlineKeyboardButton]]，包进 InlineKeyboardMarkup(inline_keyboard=kb)
```

### Discord —— Button + ActionRow

```python
def _dc_kb(button: dict):
    from nonebot.adapters.discord.api import Button, ButtonStyle
    return Button(label=button["text"], custom_id=button["data"], style=ButtonStyle.Primary)
# 每行最多放进一个 ActionRow，用 MessageSegment.component(ActionRow(components=[...]))
```

### 米游社大别野 Villa —— 区分回调/输入按钮

```python
def _villa_kb(index: int, button: dict):
    from nonebot.adapters.villa.models import InputButton, CallbackButton
    if button["action"] == 1:   # 回调
        return CallbackButton(id=str(index), text=button["text"], extra=button["data"])
    elif button["action"] == 2: # 命令（输入）
        return InputButton(id=str(index), text=button["text"], input=button["data"])
    else:
        return CallbackButton(id=str(index), text=button["text"], extra=button["data"])
```

### 开黑啦 Kaiheila —— Card module 里的 button 元素

```python
def _kaiheila_kb(button: dict):
    return {
        "type": "button", "theme": "info",
        "value": button["data"], "click": "return-val",
        "text": {"type": "plain-text", "content": button["text"]},
    }
```

### DoDo —— CardButton

```python
def _dodo_kb(button: dict):
    from nonebot.adapters.dodo.models import CardButton, ButtonClickAction
    return CardButton(
        click=ButtonClickAction(value=button["data"], action="call_back"),
        name=button["text"],
    )
```

> 共同套路：写一个 `_xxx_kb(button)` 把单个 dict 转成平台按钮对象，再写攒行逻辑处理 A/B 两种布局。

## 6.4 Markdown 适配

### `markdown`（自由文本 MD）

```python
data == "# 标题\n正文 ![图片](url) ..."
```
- 支持原生 MD 的平台（QQ 官方需模板、开黑啦 KMarkdown、DoDo dodo-md…）直接塞文本段。
- core 端有个细节：MD 里的图片是 `link://` 形式时它会 `replace('link://','')`；适配器收到的
  `markdown` 文本若含 `link://` 也应 `.replace('link://', '')` 再发。
- 不支持 MD 的平台：要么降级成纯文本发，要么 `logger.warning` 跳过。

### `template_markdown`（**仅 QQ 官方**）

```python
data == {"template_id": "xxx", "para": {"key1": "val1", "key2": "val2"}}
```
- QQ 官方群/单聊不允许发自由 MD，只能发**预先报备的模板**。映射到：

```python
from nonebot.adapters.qq.models import MessageMarkdown, MessageMarkdownParams
MessageSegment.markdown(MessageMarkdown(
    custom_template_id=template_markdown["template_id"],
    params=[MessageMarkdownParams(key=k, values=[template_markdown["para"][k]])
            for k in template_markdown["para"]],
))
```
- core 侧的「`markdown` → `template_markdown` 自动转换」由 `segment.py` 的
  `markdown_to_template_markdown()` 按你在 core 配的模板正则完成，**适配器只管把收到的
  `template_markdown` 段落地**。

### `template_buttons`（**仅 QQ 官方**）

```python
data == "按钮模板 ID 字符串"
# → MessageSegment.keyboard(MessageKeyboard(id=template_buttons))
```

## 6.5 `image_size`：配合 MD 渲染图片尺寸

```python
data == [width, height]   # 例 [1080, 1920]
```
- 当 core 把图片以 URL 形式嵌进 markdown（`![图片 #1080px #1920px](url)`）时，需要知道宽高，
  就额外下发一段 `image_size`。
- 普通图片消息**用不到** `image_size`，只有"图片 + markdown 一起发"且走 URL 时才需要。
- 适配器若不做 MD 图文混排，**可直接忽略 `image_size` 段**。

## 6.6 适配优先级建议

1. **先把 `text` + `image` + `at` 跑通**——这覆盖 95% 的实际消息。
2. 平台支持按钮再做 `buttons`；支持 MD 再做 `markdown`。
3. `template_*` 是 QQ 官方专属，接 QQ 官方再处理，其它平台完全不用管。
4. 任何不支持的类型，**`logger.warning('[xxx] 暂不支持 yyy')` + 跳过**，不要抛异常中断整条消息。
</content>
