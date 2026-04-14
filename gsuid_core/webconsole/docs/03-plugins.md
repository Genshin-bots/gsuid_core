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
            "icon": "base64..."
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

---

## 3.4 保存插件配置
```
POST /api/plugins/{plugin_name}/config
```

**请求体**：插件配置键值对

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

---

## 3.9 卸载插件
```
DELETE /api/plugins/{plugin_name}
```

---

## 3.10 获取插件市场
```
GET /api/plugins/market
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [...]
}
```

---

## 3.11 检查插件更新
```
GET /api/plugins/{plugin_name}/update
```
