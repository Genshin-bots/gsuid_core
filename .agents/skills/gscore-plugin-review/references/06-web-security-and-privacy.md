# 六、Web 路由、插件前端页、隐私与安全审计

> 返回 [SKILL 主入口](../SKILL.md)

向商店公开发布的插件会运行在成千上万个暴露于局域网或公网的 Core 实例上。**凡涉及 HTTP 路由、前端页、账号登录、文件读写或外部网络请求的插件，本节每一条都属于重点审查对象。**

## 6.1 FastAPI 路由与插件前端页规范

对照 [19-fastapi-plugin-api.md](../../gscore-plugin-development/references/19-fastapi-plugin-api.md) 与 [22-plugin-pages.md](../../gscore-plugin-development/references/22-plugin-pages.md) 核查：

| 检查项 | 级别 | 规则要求 | 常见违规 |
|--------|------|---------|---------|
| 复用共享 `app` | **P0** | 从 `gsuid_core.webconsole.app_app` 导入 `app`，或使用 `PluginAPI()`；**严禁**新建 `FastAPI()` 或 `uvicorn.run()` 另起端口 | 另起端口导致冲突、绕过框架统一反代与热重载清理 |
| 路由前缀规范 | P1 | 自定义接口统一挂载在 `/api/<插件名小写>/...`，避免与 Core 40+ 官方路由或静态前端路径冲突 | 直接挂在根路径 `/login`、`/kq/...` 等自定义非 `/api/` 路径 |
| 接口鉴权 | **P0** | 所有写操作、读取用户私密数据/凭据、管理接口**必须**挂 `Depends(require_auth)`（或 `PluginAPI()` 默认鉴权）或等效强鉴权（HMAC/JWT + `hmac.compare_digest`） | 无鉴权的公开接口允许任意互联网扫描器读取绑定/Cookie 或修改插件状态 |
| 插件前端页规范 | P0/P1 | 用 `register_plugin_page(title=..., static_dir=..., page_id=...)` 注册，**禁止** `app.mount` 暴露源码根目录；`static_dir` 仅含静态前端资源，不含 `.py`/`.env`/密钥；遵循 Hub 主题变量与三语言 `locales/*.json` | 把 `static_dir` 指向插件包根目录导致源码被静态下载；把 API Key 写死在前端 JS |
| 禁止阻塞事件循环 | P0 | API 处理函数内禁止 `time.sleep`、同步 `requests` 或长达数十秒的同步等待（长任务交给 `BackgroundTasks` 或调度器） | 单个 HTTP 请求卡死整个 WebConsole 与 Bot WS 通信 |

## 6.2 网页登录与公网暴露安全红线（高发 P0）

插件若提供「用户在网页输入手机号/验证码/密码/Cookie 完成登录」的流程，必须逐条核验以下 6 项：

1. **严禁自动探测并广播宿主机公网 IP（P0）**：
   - 未配置对外域名（如 `LoginUrl`）时，**绝不可**默认请求 `api.ipify.org` / `httpbin.org` / `ifconfig.me` 拿服务器真实公网 IP 再拼成 `http://<公网IP>:<PORT>/...` 发给用户！
   - 危害：直接暴露部署者的源站 IP（绕过 CDN/反代、引来端口扫描与 DDoS），且让用户在明文 `http://` 下提交手机号、短信验证码或密码。
   - 正确做法：未配置登录地址时直接拒绝生成外链，提示管理员在控制台配置 HTTPS 反代地址。
2. **个人登录链接严禁明文发到公共群聊（P0）**：
   - 群聊发出带 `auth_token` 的登录 URL 与校验码，同群任何成员均可点击链接：要么盗用会话触发短信骚扰，要么用攻击者账号完成登录并绑定到受害者 `user_id`（会话固定/账号劫持）。
   - 正确做法：登录命令限定私聊（`SV(..., area="DIRECT")`），或明确仅私聊下发链接。
3. **一次性 Token 强度、TTL 与绑定（P0）**：
   - `auth_token` 必须由 `secrets.token_urlsafe(16+)` 生成，严禁用递增 ID、时间戳或 `user_id` 可预测值；
   - 设短 TTL（如 3–5 分钟），登录成功后立即失效（不可复用），同用户重新发起时立即作废旧会话。
4. **服务端防爆破与发码频控（P0）**：
   - 不能只靠前端 JS 按钮倒计时！后端 `/sendSmsCode` 与 `/login` 必须维护单 `auth_token`（及 IP/手机号维度）的最小发码间隔与最大失败次数，超限立刻作废会话，防止成为短信轰炸中继或被撞库爆破验证码。
5. **不泄露敏感日志（P0）**：
   - 日志只记脱敏用户标识与事件结果，**严禁**打印手机号、短信验证码、Cookie、JWT、`sign` 令牌或完整 `Authorization` 头。
6. **会话缓存有界（P1）**：
   - 内存登录会话表必须设 `maxsize`（如 64/128）与过期清扫，防止恶意反复触发登录耗尽内存。

**真实案例（`CalabiyauUID`）**：
- **正向**：[login_services.py](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/calabiyau_login/login_services.py) 使用了 `secrets.token_urlsafe(16)`、有界 `TimedLoginCache(maxsize=64)`、单调时钟 TTL、新会话驱逐旧会话，且模板使用了 `autoescape=True` + `| tojson`。
- **P0 缺陷**：
  1. `get_login_base_url()` 在未配 `FanbookLoginUrl` 且 `HOST` 为 `0.0.0.0`/`127.0.0.1` 时，自动调用 `api.ipify.org` / `httpbin.org` 并把 `http://<公网IP>:8765/kq/fanbook/i/<token>` 发进群聊；
  2. `sv_fanbook_login` 未限制 `area="DIRECT"`，群聊里直接发出登录链接与校验码；
  3. `/kq/fanbook/sendSmsCode` 与 `/kq/fanbook/login` 缺乏服务端冷却与失败次数上限，且路由未按 `/api/<插件名>/...` 规范注册。

## 6.3 通用安全与隐私清单

| 风险类型 | 级别 | 审查要点 |
|----------|------|---------|
| 恶意/后门/遥测行为 | **P0** | 检查全部外部域名清单（`EXTERNAL_HOSTS`）：是否存在未说明的数据上报、收集用户聊天记录/凭据发往非官方私人服务器、远程拉取并 `exec`/`eval` 代码、擅自修改 Core 配置或安装未声明包 |
| 命令注入 / 任意代码执行 | **P0** | 禁止 `shell=True` 拼接用户输入；禁止 `eval`/`exec`/`pickle.loads` 处理外部输入；调用 `git` 等子进程传参必须用参数列表且对 URL/分支加 `--` 分隔 |
| SSRF（服务端请求伪造） | **P0** | 凡插件代用户请求 URL（下载图片/解析链接/拉清单），不得允许请求任意用户可控 URL 到内网地址（`127.0.0.1`、`169.254.169.254`、`10.0.0.0/8`、`192.168.0.0/16`、`file://`） |
| 路径穿越与 `file://` 泄露 | **P0** | 用户输入拼路径必校验；构造 `file://` URI 发文件时，路径必须严格限定在插件专属缓存子目录，且默认关闭 |
| TLS 校验与证书 | **P0** | 禁止 `httpx.AsyncClient(verify=False)` 全局关闭 HTTPS 证书校验（除非有强正当理由且显式提示） |
| 硬编码个人隐私与密钥 | **P0** | 仓库代码/注释/样例不得包含真实个人 QQ 号、真实手机号、私人 API Key、私人 Cookie 或真实群聊记录（对照 `AGENTS.md §1.13`） |
| 第三方逆向接口与凭据落库 | **P2** | 社区签到/查询插件常内置公开抓包得到的客户端 `AppKey`/`AppSecret` 并在本地库保存用户登录令牌。审核需确认**仅发往官方域名**、无转发作者服务器行为，并要求作者在 README 与商店 `installMsg` 中写明账号与隐私免责声明 |
