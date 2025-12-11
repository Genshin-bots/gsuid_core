# 基于 astral/uv 的 Python 3.12 Bookworm-slim 镜像
# 原始镜像
# FROM astral/uv:python3.12-bookworm-slim
# CNB加速镜像
FROM docker.cnb.cool/gscore-mirror/docker-sync/astral-uv:python3.12-bookworm-slim

# 设置工作目录
WORKDIR /gsuid_core

# 暴露 8765 端口
EXPOSE 8765

# uv 环境变量
ENV UV_PROJECT_ENVIRONMENT=/venv
# 设置 PATH，优先从 /venv/bin 查找可执行文件
ENV PATH="/venv/bin:$PATH"
# 启用绑定挂载缓存
ENV UV_LINK_MODE=copy

# 安装系统依赖（包括编译工具和时区数据）
RUN apt-get update && apt-get install -y \
    git \
    curl \
    gcc \
    python3-dev \
    build-essential \
    tzdata && \
    rm -rf /var/lib/apt/lists/* && \
    ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone

# 配置 git safe.directory，防止容器内 ownership 报错
RUN git config --global --add safe.directory '*'

# 在镜像层创建 venv
RUN uv venv --seed /venv

# 启动命令
CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "0.0.0.0"]
