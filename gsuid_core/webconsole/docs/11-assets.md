# 11. 图片资源 API - /api/assets

## 11.1 上传图片
```
POST /api/assets/upload
```

**请求体**：
```json
{
    "image": "base64编码数据",
    "filename": "image.jpg",
    "upload_to": "GsCore/bg",
    "target_filename": "custom_name.jpg"
}
```

**响应**：
```json
{
    "status": 0,
    "msg": "上传成功",
    "data": {
        "path": "/absolute/path/to/image.jpg",
        "url": "/api/assets/preview?path=base64encoded"
    }
}
```

保存位置必须落在 `data/` 目录内：相对名 `data` 表示根目录，也可传 `get_res_path()` 下的绝对路径。

---

## 11.2 预览图片
```
GET /api/assets/preview?path=base64encoded
```
路径必须落在 `data/` 目录内。`<img>` 可用 `?token=`（EventSource 同源限制）；其它请求优先 `Authorization: Bearer`。

---

## 11.3 删除图片
```
DELETE /api/assets/delete
```

**Query 参数**：`path`: URL 编码的文件路径

---

## 11.4 上传图片（文件）
```
POST /api/uploadImage/{suffix}/{filename}/{UPLOAD_PATH:path}
```

**Form Data**：`file`: 图片文件

---

## 11.5 获取图片
```
GET /api/getImage/{suffix}/{filename}/{IMAGE_PATH:path}
```

---

## 11.6 阅后即焚图片
```
GET /api/tempImage/{image_id}
```
