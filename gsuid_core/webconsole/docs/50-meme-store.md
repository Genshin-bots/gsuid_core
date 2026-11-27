# 50. 表情包市场 API - /api/meme-store

## 概述

表情包市场 API 面向 CNB 官方仓库（`https://cnb.cool/gscore-meme/gscore-meme`），提供市场目录浏览、免克隆预览代理（封面 / 整包一览图 / 单张表情）、单分支浅克隆安装、增量同步更新、卸载与多个人格批量挂载能力。

- 认证方式：读取接口与人格挂载需 `require_auth`；安装 / 更新 / 卸载需 `require_admin`。
- 市场导入的表情包落入本地 `AiMemePack`，可自由挂载到多个非 git 托管人格，并在安装/更新时自动导入预打标 `tags.json` 与同步向量。
- 市场条目的 `repo` 必须命中白名单（当前仅官方仓库），否则条目在解析期被丢弃；预览代理与克隆不会跟随目录指向任意主机。
- 预打标 `tags.json` 中 `nsfw_score` 超过本地 `meme_nsfw_threshold` 的表情按 `rejected` 入库：不进包、不同步向量，安装结果以 `rejected` 计数返回。
- 自定义 URL 安装登记克隆后的真实分支；更新 / 卸载会彻底清理不再归属任何包的纯市场来源表情（文件、记录、向量）。

## 端点一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/meme-store/list` | 市场列表（支持 `tab` / `series` / `search` / `limit` / `offset` / `refresh` / `check_updates`） |
| GET | `/api/meme-store/installed` | 列出已通过市场安装的表情包及挂载人格 |
| GET | `/api/meme-store/detail/{store_id}` | 免克隆获取整包 `tags.json` 与单图预览清单 |
| GET | `/api/meme-store/asset/cover/{store_id}` | 代理并本地缓存 2×2 封面图 |
| GET | `/api/meme-store/asset/sheet/{store_id}` | 代理并本地缓存整包一览图 |
| GET | `/api/meme-store/asset/item/{store_id}/{filename}` | 代理并本地缓存单张预览表情 |
| POST | `/api/meme-store/install/{store_id}` | 单分支浅克隆安装白名单表情包（可选传 `persona_names` 同步挂载） |
| POST | `/api/meme-store/install-url` | 通过自定义 Git URL 导入表情包 |
| POST | `/api/meme-store/update/{store_id}` | 增量同步远端分支最新表情，保留人格挂载 |
| DELETE | `/api/meme-store/uninstall/{store_id}` | 卸载包并清理不再归属任何包的市场来源孤儿图 |
| PUT | `/api/meme-store/packs/{pack_id}/personas` | 批量设置某个本地表情包挂载的人格列表 |
