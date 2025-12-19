# ==========================================
# Stage 0: 参数
# ==========================================
# 原始镜像 astral/uv:python3.12-bookworm-slim
# cnb.cool 镜像 docker.cnb.cool/gscore-mirror/docker-sync/astral-uv:python3.12-bookworm-slim

# 定义基础镜像的参数
ARG GSCORE_BASE_IMAGE=docker.cnb.cool/gscore-mirror/docker-sync/astral-uv:python3.12-bookworm-slim

# 定义 Python 镜像源
ARG GSCORE_PYTHON_INDEX=https://pypi.org/simple

# ==========================================
# Stage 1: Base (最基础的系统环境)
# 包含：Python, 时区, 编译工具, Git, 空虚拟环境
# ==========================================
FROM ${GSCORE_BASE_IMAGE} AS base

# 暴露端口 (放在最上面)
EXPOSE 8765

WORKDIR /gsuid_core

# 环境变量
ENV UV_PROJECT_ENVIRONMENT=/venv
ENV PATH="/venv/bin:$PATH"
ENV UV_LINK_MODE=copy

# 1. 安装最基础的系统依赖
# 2. 创建虚拟环境
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    gcc \
    python3-dev \
    build-essential \
    tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && uv venv /venv --seed \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 配置 git safe.directory
RUN git config --global --add safe.directory '*'

# ==========================================
# Stage 2: Playwright Base (浏览器环境层)
# 包含：Chromium, 中文字体, Python Playwright 库(已安装在 venv)
# ==========================================
FROM base AS playwright_base

# 再次声明 Python 镜像源
ARG GSCORE_PYTHON_INDEX

# 设置浏览器全局路径
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 1. 安装 Playwright 运行所需的额外依赖 (中文字体 + 浏览器依赖)
# 2. 下载 Chromium 及其依赖
RUN --mount=type=cache,target=/root/.cache/uv \
    apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    libffi-dev \
    && uv pip install playwright --index ${GSCORE_PYTHON_INDEX} \
    && playwright install --with-deps chromium \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ==========================================
# Stage 3: Runtime (挂载模式)
# 继承自 playwright_base
# ==========================================
FROM playwright_base AS runtime

# 启动命令
CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "::"]

# ==========================================
# Stage 4: Bundle (全量模式)
# 继承自 playwright_base
# ==========================================
FROM playwright_base AS bundle

# 再次声明 Python 镜像源
ARG GSCORE_PYTHON_INDEX

# 1. 复制项目依赖，不要lock文件
COPY pyproject.toml README.md ./

# 2. 应用自定义镜像源
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --index ${GSCORE_PYTHON_INDEX}

# 3. 复制项目代码
COPY . .

# 4. 启动命令
CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "::"]
