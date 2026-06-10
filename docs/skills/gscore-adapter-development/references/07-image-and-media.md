# 七、图片与多媒体

图片是适配器最常出问题的地方，单独成章。核心就一句话：**`image` 永远要同时处理
`base64://` 和 `link://` 两种前缀。**

## 7.1 图片的两种形态（必背）

core 下发的 `image` 段，`data` 只会是这两种之一：

| 前缀 | 含义 | 解出内容 |
|------|------|---------|
| `base64://` | 图片的 base64（默认形态） | `base64.b64decode(data[9:])` → `bytes` |
| `link://` | 远程图片 URL（开启"自动转链接"时） | `data[7:]` → `str` URL |

为什么有两种？core 有个「发送图片自动转链接」配置（`send_pic_config`）：

- 关闭（默认）：core 把图片编码成 `base64://` 下发。
- 开启：core 先把图片传到本地图床/对象存储，下发 `link://https://...`，省带宽、绕过部分平台的
  base64 大小限制。

⚠️ **只处理一种 = 埋雷**：你只写了 `base64://` 分支，用户某天开了转链接，所有图片瞬间发不出且不报错
（`data` 不以 `base64://` 开头，被你的 `else` 吞了）。这是最高频的适配 bug。

## 7.2 标准双形态处理模板

```python
def make_image(image: str):
    if image.startswith("link://"):
        url = image.replace("link://", "")
        return PlatformSeg.image(url)              # 平台支持直接发 URL
    else:  # base64://
        img_bytes = base64.b64decode(image.replace("base64://", ""))
        return PlatformSeg.image(img_bytes)        # 平台收 bytes
```

若平台**只收 URL 不收 bytes**（如部分 Web 平台），把 base64 转成 bytes 后**先上传**得到 url：

```python
if image.startswith("link://"):
    url = image.replace("link://", "")
else:
    img_bytes = base64.b64decode(image.replace("base64://", ""))
    url = await bot.upload_image(img_bytes)        # 平台上传接口换 url
```

若平台**只收 URL 且你没有上传接口**，遇到 `link://` 时可以下载成 bytes 再走平台的本地图片接口：

```python
from .utils import download_image
if image.startswith("link://"):
    img_bytes = await download_image(image.replace("link://", ""))
else:
    img_bytes = base64.b64decode(image.replace("base64://", ""))
```

## 7.3 各平台图片落地实例

### OneBot v11（QQ，收 url 或 base64 皆可）

```python
from nonebot.adapters.onebot.v11 import MessageSegment
# OneBot 的 image 段能直接吃 url，也能吃 base64：
MessageSegment.image(image.replace("link://", ""))   # link:// 去前缀即是 url
# base64:// 形态 OneBot 也认（部分实现），稳妥起见可 decode 后传 bytes
```

### 上传型平台（开黑啦 / Red / 飞书 / 大别野）—— 先上传换 URL

```python
# 开黑啦：先下载/解码成 bytes，再 upload_file 得 url，最后发 image(url)
if image.startswith("link://"):
    img_bytes = await download_image(image.replace("link://", ""))
else:
    img_bytes = base64.b64decode(image.replace("base64://", ""))
url = await bot.upload_file(img_bytes, "GSUID-TEMP")
message.append(MessageSegment.image(url))
```

```python
# 大别野 Villa：base64 走 upload_image，link 走 transfer_image
if image.startswith("link://"):
    img_url = await bot.transfer_image(url=image.replace("link://", ""))
else:
    img_bytes = base64.b64decode(image.replace("base64://", ""))
    img_url = (await bot.upload_image(img_bytes)).url
msg += MessageSegment.image(img_url)
```

### 需要宽高的平台（黑盒 Heybox）

```python
if image.startswith("link://"):
    return MessageSegment.image(image.replace("link://", ""), 720, 1280)  # 给个默认宽高
else:
    img_bytes = base64.b64decode(image.replace("base64://", ""))
    with Image.open(BytesIO(img_bytes)) as img:
        w, h = img.size                              # base64 可直接读真实宽高
    return MessageSegment.local_image(img_bytes, w, h, uuid4().hex + ".jpg")
```

### OneBot v12（上传后用 file_id）

```python
if image.startswith("link://"):
    up = await bot.call_api("upload_file", type="url", url=image[7:], name=fn)
else:
    img_bytes = base64.b64decode(image.replace("base64://", ""))
    up = await bot.call_api("upload_file", type="data", data=img_bytes, name=fn)
await bot.call_api("send_message", message=[{"type": "image", "data": {"file_id": up["file_id"]}}], ...)
```

## 7.4 文件 `file`

```python
data == "文件名|内容"
file_name, file_content = data.split("|")
```
`file_content` 同样可能是 `link://url` 或 base64。各平台差异很大：

- **OneBot v11**：base64 先落地成本地文件，再 `upload_group_file` / `upload_private_file`，发完删除。
- **Milky**：直接 `upload_group_file(group_id, file_name, base64=file_content)`，无需落盘。
- **OneBot v12**：`upload_file(type=url|data)` 换 `file_id` 再 `send_message`。
- **飞书 / 开黑啦**：读 bytes → `im/v1/files` / `upload_file` 换引用再发。

落盘辅助：

```python
def store_file(path: Path, file_b64: str):
    with open(path, "wb") as f:
        f.write(base64.b64decode(file_b64))
def del_file(path: Path):
    if path.exists(): os.remove(path)
```

## 7.5 语音 `record` / 视频 `video`

```python
data == "base64://...."     # 恒为 base64，不会有 link://
b = base64.b64decode(data.replace("base64://", ""))
```
- 有 `MessageSegment.record` / `.video` 的平台直接发。
- 不支持的平台：`logger.warning('[xxx] 暂不支持发送语音/视频')` 然后 `return` 跳过，**别抛异常**。
- 通用解码小工具：

```python
def b64_to_bytes(data: str) -> bytes:
    prefix = "base64://"
    return base64.b64decode(data[len(prefix):] if data.startswith(prefix) else data)
```

## 7.6 上报方向的图片（平台 → core）

反过来，上报时你给 core 的 `image` 段 `data`：

- 优先填**平台可公开访问的 URL**（core 会按需下载），其次 base64。
- 平台只给 `file_id` 时，**在适配器侧先换成 url/base64 再上报**——core 无法理解你平台的 file_id。
- OneBot v11 群文件/离线文件上报：通过 `get_file` 拿到 base64 或本地路径再转 base64，按
  `file` 段上报（`name|base64`）。

## 7.7 一句话清单

- [ ] `image` 同时处理 `base64://` 和 `link://`。
- [ ] 平台只收 URL → base64 先上传/下载换 url。
- [ ] 需要宽高的平台：base64 用 PIL 读真实尺寸，link 给合理默认值。
- [ ] `record`/`video` 恒 base64；`file` 是 `名|内容` 且内容也可能是 link。
- [ ] 不支持的媒体类型一律 warning + 跳过，不中断消息。
</content>
