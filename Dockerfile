# ==========================================
# Stage 1: Base (最基础的系统环境)
# 包含：Python, 时区, 编译工具, Git, 空虚拟环境
# ==========================================
FROM docker.cnb.cool/gscore-mirror/docker-sync/astral-uv:python3.12-bookworm-slim AS base

# 暴露端口 (放在最上面)
EXPOSE 8765

WORKDIR /gsuid_core

# 环境变量
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV PATH="/venv/bin:$PATH"
ENV UV_LINK_MODE=copy

# 1. 安装最基础的系统依赖
# 2. 创建虚拟环境
RUN apt-get update && apt-get install -y \
    git \
    curl \
    gcc \
    python3-dev \
    build-essential \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && uv venv /venv --seed

# 配置 git safe.directory
RUN git config --global --add safe.directory '*'

# ==========================================
# Stage 2: Playwright Base (浏览器环境层)
# 包含：Chromium, 中文字体, Python Playwright 库(已安装在 venv)
# ==========================================
FROM base AS playwright_base

# 设置浏览器全局路径
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 安装 Playwright 运行所需的额外依赖 (中文字体 + 浏览器依赖)
RUN apt-get update && apt-get install -y \
    fonts-noto-cjk \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 1. 安装 Playwright 库到 venv (venv 已在 base 阶段创建)
# 2. 下载 Chromium 及其依赖
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install playwright && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

# ==========================================
# Stage 3: Runtime (挂载模式)
# 继承自 playwright_base
# ==========================================
FROM playwright_base AS runtime

# 可以在这里添加 runtime 特有的环境变量
# ENV MY_ENV_VAR=value

# 启动命令
CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "0.0.0.0"]


# ==========================================
# Stage 4: Bundle (全量模式)
# 继承自 playwright_base
# ==========================================
FROM playwright_base AS bundle

# 1. 复制项目代码
COPY . .

# 2. 安装项目
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# 启动命令
CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "0.0.0.0"]
