# 十二、Docker 部署

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十一、数据库配置](./11-database.md) · **下一章**：[十三、AI 核心部署要点](./13-ai.md)

Docker 镜像基于 `astral/uv:python3.12-bookworm-slim` 自建 `gscore-uv-3.12` 基础镜像，**已预装**：

- Python 3.12 + uv
- Playwright + Chromium 浏览器
- 常用中文字体
- Linux/amd64 / Linux/arm64

> 用户文档：https://docs.sayu-bot.com/Started/DockerCore.html

## 12.1 两种部署模式

| 模式 | 文件 | 特点 |
|------|------|------|
| **挂载模式（mount）** | `docker-compose.yml` + `Dockerfile`（target=`runtime`） | 挂载本地代码到容器，修改即生效；构建快；适合开发者 |
| **全量模式（bundle）** | `docker-compose.bundle.yml`（拉预构建镜像） | 不用下载源码，直接跑全量镜像；适合生产 / 懒人 |

### 12.1.1 选择 1（挂载模式 / 推荐）

```sh
git clone https://github.com/Genshin-bots/gsuid_core.git
# 或国内镜像
git clone https://cnb.cool/gscore-mirror/gsuid_core.git

cd gsuid_core
cp .env.example .env       # 可选
docker-compose up -d --build
```

> 本地代码改动**直接生效**（代码挂载进容器），但依赖更新需要重建。

### 12.1.2 选择 2（全量模式）

只需 `docker-compose.bundle.yml` + `.env`：

```sh
wget https://raw.githubusercontent.com/Genshin-bots/gsuid_core/master/docker-compose.bundle.yml
cp .env.example .env   # 自行创建或下载
docker-compose -f docker-compose.bundle.yml up -d
```

或纯 docker run：

```sh
docker run -d \
  --name gsuid_core \
  --restart always \
  -p 8765:8765 \
  -v /opt/gscore_data:/gsuid_core/data \
  -v /opt/gscore_plugins:/gsuid_core/gsuid_core/plugins \
  -v gsuid_core_venv:/venv \
  docker.cnb.cool/gscore-mirror/gsuid_core:latest
```

## 12.2 挂载点与持久化

| 容器路径 | 用途 | 必须？ |
|----------|------|--------|
| `/gsuid_core/data` | 数据库 / 配置 / 日志 / AI / 主题 / 备份 | ✅ |
| `/gsuid_core/gsuid_core/plugins` | 业务插件目录 | ✅ |
| `/venv` | Python 虚拟环境（命名卷） | ✅ |

> `data` 与 `plugins` 必须挂到宿主机，**否则容器重建后数据全丢**。
>
> `/venv` 用命名卷持久化，避免每次重建重装依赖；但**镜像升级后跨版本的 wheels
> 可能不兼容**（需 `docker volume rm gsuid_core_venv` 重建）。

## 12.3 .env 配置详解

`.env.example` 完整模板（拷贝为 `.env`）：

```properties
# ============= 基础 =============
PORT=8765

# Python 镜像源
GSCORE_PYTHON_INDEX=https://pypi.org/simple/

# ============= 挂载模式（mount） =============
MOUNT_PATH=.                                                  # 代码挂载源
GSCORE_BUILTIN_BASE=docker.cnb.cool/gscore-mirror/gsuid_core/gscore-uv-3.12:latest
GSCORE_BUILD_TARGET=runtime                                   # 或 bundle

# ============= 全量模式（bundle） =============
DATA_PATH=/opt/gscore_data
PLUGIN_PATH=/opt/gscore_plugins

# ============= 网络代理 =============
# GSCORE_HTTP_PROXY=http://host.docker.internal:7890
# GSCORE_HTTPS_PROXY=http://host.docker.internal:7890
# GSCORE_NO_PROXY=localhost,127.0.0.1,.local,cnb.cool,mirrors.aliyun.com
```

### 12.3.1 国内 PyPI 镜像源速查

| 镜像 | URL |
|------|-----|
| 阿里云 | `https://mirrors.aliyun.com/pypi/simple/` |
| 腾讯云 | `https://mirrors.cloud.tencent.com/pypi/simple/` |
| 火山引擎 | `https://mirrors.volces.com/pypi/simple/` |
| 华为云 | `https://mirrors.huaweicloud.com/repository/pypi/simple/` |
| 清华 | `https://pypi.tuna.tsinghua.edu.cn/simple/` |
| 中科大 | `https://mirrors.ustc.edu.cn/pypi/` |
| 北外 | `https://mirrors.bfsu.edu.cn/pypi/web/simple/` |
| 上交 | `https://mirror.sjtu.edu.cn/pypi/web/simple/` |
| 南大 | `https://mirror.nju.edu.cn/pypi/web/simple/` |

测速脚本：[`check_pypi_mirrors.py`](../../../check_pypi_mirrors.py)（标准库，
Windows / macOS / Linux 均可）：

```sh
python3 check_pypi_mirrors.py
```

输出会推荐最快的源并给出对应启动命令。

## 12.4 容器生命周期管理

### 12.4.1 查看 / 日志

```sh
docker ps -a | grep gsuid_core
docker logs -f gsuid_core
docker logs --tail 200 gsuid_core
```

### 12.4.2 进入容器

```sh
docker exec -it gsuid_core bash
```

容器内常用命令：

```sh
# 查看 Python 版本
python --version

# 重启 Core（容器内）
core重启

# 装额外包（持久化在 /venv 卷）
uv pip install <package>

# git 代理（一次性）
git config --global http.proxy http://host.docker.internal:7890

# 资源查看
df -h
free -h
```

### 12.4.3 资源下载加速

容器内已预装 Playwright + Chromium，无需手动 `playwright install`。

业务资源（图片 / wiki / 圣遗物）由插件启动时下载，挂在代理或挂载后 `core重启` 即可。

### 12.4.4 升级镜像

```sh
# 全量模式
docker-compose -f docker-compose.bundle.yml pull
docker-compose -f docker-compose.bundle.yml up -d

# 挂载模式（拉新代码即可）
cd gsuid_core
git pull
docker-compose up -d --build
```

### 12.4.5 环境重置（依赖冲突）

```sh
# 挂载模式
docker-compose down -v        # ⚠️ 删 venv-data 卷
docker-compose up -d --build

# 全量模式（docker run）
docker stop gsuid_core && docker rm gsuid_core
docker volume rm gsuid_core_venv
# 然后重新跑 docker run

# 全量模式（docker compose）
docker-compose -f docker-compose.bundle.yml down -v
docker-compose -f docker-compose.bundle.yml up -d
```

> 警告：会删除 `/venv` 卷，**手动装的包需要重装**；`/gsuid_core/data` **不丢**。

## 12.5 高级：网络代理

### 12.5.1 Python / 容器代理（HTTP）

`.env`：

```properties
GSCORE_HTTP_PROXY=http://host.docker.internal:7890
GSCORE_HTTPS_PROXY=http://host.docker.internal:7890
GSCORE_NO_PROXY=localhost,127.0.0.1,.local,cnb.cool,mirrors.aliyun.com,pypi.tuna.tsinghua.edu.cn
```

> 宿主机 IP 在容器内是 `host.docker.internal`（依赖 `extra_hosts`，compose 已配）。

### 12.5.2 git 代理

`.env` 不覆盖 git 代理，**需要单独进容器设**：

```sh
docker exec -it gsuid_core git config --global http.proxy http://host.docker.internal:7890
docker exec -it gsuid_core git config --global https.proxy http://host.docker.internal:7890
```

或者用 SSH 模式（见 [九、插件 §9.5](./09-plugins.md#95-git-镜像源切换)）。

### 12.5.3 Docker daemon 代理

如果连 Docker Hub 都拉不动（公司内网等），需要改 Docker daemon 代理：
`/etc/systemd/system/docker.service.d/http-proxy.conf`，不在 Core 范围内。

## 12.6 多架构支持

镜像同时支持 `linux/amd64`（x86_64）和 `linux/arm64`（Apple Silicon / 树莓派）。
Docker 自动选择，**无需手动指定**。

## 12.7 镜像 / 容器架构

```
docker pull docker.cnb.cool/gscore-mirror/gsuid_core:latest
```

| Tag | 说明 |
|-----|------|
| `latest` | 最新稳定版 |
| `:x.y.z` | 指定版本（如 `:0.10.5`） |
| `gscore-mirror/gsuid_core/gscore-uv-3.12:latest` | 仅基础镜像（Python+uv+playwright+chromium+字体），不含 GsCore 代码（挂载模式用） |

## 12.8 常见错误

| 现象 | 原因 / 解决 |
|------|--------------|
| `port is already allocated` | 8765 被占用，改 `.env` 的 `PORT` |
| 容器起来又立刻退出 | `docker logs gsuid_core` 看错误（依赖 / 配置 / 端口） |
| `permission denied` 写 data | 检查宿主机挂载目录权限，容器内是 root |
| Playwright 渲染失败 | 镜像已预装，若挂载模式自建镜像需重装 |
| `git clone` 卡死 | 配 git 代理或切镜像源 |
| `uv sync` 超时 | PyPI 网络差，换 `GSCORE_PYTHON_INDEX` |
| 容器能起但 WS 连不上 | `HOST` 没改（镜像默认 `--host 0.0.0.0` 通常 OK） |
| 升级后报错「模块找不到」 | `docker-compose down -v` 重置 venv |
| 升级后 ws 连接被 1008 | 镜像默认设了 token 校验，但 `config.json` 是空的，需配 `WS_TOKEN` |

## 12.9 部署 Checklist

- [ ] 选好模式（mount / bundle）
- [ ] 三个挂载点（`data` / `plugins` / `venv`）都到位
- [ ] `data/config.json` 配了 `HOST=0.0.0.0` + `WS_TOKEN`
- [ ] `masters` 填了自己的 ID
- [ ] （公网）防火墙放行 `8765`
- [ ] （可选）`.env` 配了 PyPI 镜像源
- [ ] （可选）配了 git 代理 / 镜像源
- [ ] （可选）`AutoUpdateCore` + `AutoRestartCore` 配合 `--restart always`
- [ ] 浏览器开 `http://<server>:8765/app` 能进 WebConsole
