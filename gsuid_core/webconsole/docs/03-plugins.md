# 3. 插件 API - /api/plugins

## 3.1 获取插件列表
```
GET /api/plugins/list
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "id": "plugin_name",
            "name": "插件名称",
            "description": "插件描述",
            "enabled": true,
            "status": "running",
            "commit": "a1b2c3d"
        }
    ]
}
```

---

## 3.2 获取插件详情
```
GET /api/plugins/{plugin_name}
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": "plugin_name",
        "name": "插件名称",
        "description": "...",
        "enabled": true,
        "commit": "a1b2c3d",
        "icon": "base64...",
        "config": {
            "config_key": {
                "value": {},
                "default": {},
                "type": "string",
                "title": "配置项",
                "desc": "描述"
            }
        },
        "config_groups": [...],
        "service": {
            "enabled": true,
            "pm": 6,
            "priority": 5,
            "area": "ALL",
            "black_list": [],
            "white_list": [],
            "prefix": [],
            "force_prefix": []
        },
        "sv_list": [
            {
                "name": "服务名称",
                "enabled": true,
                "pm": 6,
                "priority": 5,
                "area": "GROUP",
                "black_list": [],
                "white_list": [],
                "commands": [
                    {
                        "type": "command",
                        "keyword": "帮助",
                        "block": false,
                        "to_me": false
                    },
                    {
                        "type": "prefix",
                        "keyword": "我的",
                        "block": false,
                        "to_me": false
                    },
                    {
                        "type": "keyword",
                        "keyword": "原石",
                        "block": false,
                        "to_me": false
                    },
                    {
                        "type": "regex",
                        "keyword": ".*原石.*",
                        "block": false,
                        "to_me": false
                    }
                ]
            }
        ]
    }
}
```

> **前端调用说明**：前端可以通过遍历 `data.sv_list` 获取每个服务（SV），每个服务的 `commands` 数组包含了该服务下所有触发器的信息，可用于渲染命令列表。
>
> **commands 字段说明**：
> - `type`: 触发器类型，可选值: `"command"`(命令), `"prefix"`(前缀匹配), `"suffix"`(后缀匹配), `"keyword"`(关键字匹配), `"fullmatch"`(完全匹配), `"regex"`(正则匹配), `"file"`(文件类型), `"message"`(消息)
> - `keyword`: 触发关键字/正则表达式
> - `block`: 是否阻止后续触发
> - `to_me`: 是否仅响应 @ 机器人

---

## 3.3 获取插件配置
```
GET /api/plugins/{plugin_name}/config
```

返回指定插件的所有配置项，每项包含 `value`、`default`、`type`、`title`、`desc` 及类型特有字段。

> 📖 **详细说明**：配置类型的完整字段定义、请求体格式、返回体结构及前端渲染建议，请参阅 [39. 插件配置类型参考](./39-plugin-config-types.md)。

---

## 3.4 保存插件配置
```
POST /api/plugins/{plugin_name}/config
```

**请求体（平铺格式）**：
```json
{
    "config_key_1": "新值",
    "config_key_2": true
}
```

**请求体（config_groups 格式）**：
```json
{
    "config_groups": [
        {
            "config_name": "配置组名",
            "config": {
                "config_key_1": "新值"
            }
        }
    ]
}
```

> 📖 各配置类型的值格式要求详见 [39. 插件配置类型参考](./39-plugin-config-types.md)。

---

## 3.5 更新插件服务配置
```
POST /api/plugins/{plugin_name}/service
```

**请求体**：
```json
{
    "enabled": true,
    "pm": 6,
    "priority": 5,
    "area": "ALL",
    "black_list": [],
    "white_list": [],
    "prefix": [],
    "force_prefix": []
}
```

---

## 3.6 切换插件开关
```
POST /api/plugins/{plugin_name}/toggle
```

---

## 3.7 重新加载插件
```
POST /api/plugins/{plugin_name}/reload
```

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_name` | string | ✅ | 插件名称 |

**响应**：
```json
{
    "status": 0,
    "msg": "✨ 已重载插件 xxx!"
}
```

**失败响应**：
```json
{
    "status": 1,
    "msg": "❌ 未知的插件类型 xxx"
}
```
或者
```json
{
    "status": 1,
    "msg": "❌ 重载失败: xxx"
}
```

---

## 3.8 安装插件
```
POST /api/plugins/install
```

**请求体**：
```json
{
    "plugin_name": "插件名",
    "plugin_version": "1.0.0",
    "repo_url": "https://..."
}
```

> ℹ️ **实际实现的插件商店接口位于 `/api/plugin-store/*`**（含商店列表、按 ID 安装、按 URL 安装、更新、卸载），详见 [43. 插件商店 API](./43-plugin-store.md)。本文档 §3.8 - §3.11 为早期占位说明，请以新文档为准。

---

## 3.9 卸载插件
```
DELETE /api/plugins/{plugin_name}
```

> ℹ️ 实际实现请使用 `DELETE /api/plugin-store/uninstall/{plugin_id}`，详见 [43. 插件商店 API §43.5](./43-plugin-store.md#435-卸载已安装插件)。

---

## 3.10 获取插件市场
```
GET /api/plugins/market
```

> ℹ️ 实际实现请使用 `GET /api/plugin-store/list`，详见 [43. 插件商店 API §43.1](./43-plugin-store.md#431-获取插件商店列表)。

---

## 3.11 检查插件更新
```
GET /api/plugins/{plugin_name}/update
```

> ℹ️ 实际更新操作请使用 `POST /api/plugin-store/update/{plugin_id}` 或更细粒度的 [28. Git 版本管理 API §28.6 - §28.7](./28-git-update.md)。

---

## 3.12 获取插件 ICON 图片
```
GET /api/plugins/icon/{plugin_name}
```

**说明**：直接返回插件的 ICON.png 图片文件（非 base64），适用于前端 `<img>` 标签直接引用。插件名称会自动去除首尾下划线后再进行查找。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_name` | string | ✅ | 插件名称，首尾下划线会被自动去除（如 `_GsCore_` → `GsCore`） |

**成功响应**：
- HTTP Status: `200`
- Content-Type: `image/png`
- Body: PNG 图片二进制数据

**失败响应**：
```json
{
    "status": -1,
    "msg": "插件 'xxx' 的 ICON 不存在"
}
```

**前端调用示例**：
```html
<!-- 直接在 img 标签中使用 -->
<img src="/api/plugins/icon/GsCore" alt="插件图标" />

<!-- JavaScript 中动态构建 URL -->
<script>
const pluginName = "_GsCore_";
const iconUrl = `/api/plugins/icon/${encodeURIComponent(pluginName)}`;
</script>
```

> **与 `/api/plugins/list` 的区别**：`/api/plugins/list` 返回的 `icon` 字段是 base64 编码的 Data URI 字符串，适合内联使用；而 `/api/plugins/icon/{name}` 直接返回图片文件，适合通过 URL 引用，可利用浏览器缓存，性能更优。
