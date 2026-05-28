# gsuid-base
# 业务镜像 FROM 此镜像,避免重复下载 Chromium 和系统包。
# 升级 Python / Playwright / 字体时改这里并重建。

FROM astral/uv:python3.12-bookworm-slim

ENV UV_PROJECT_ENVIRONMENT=/venv \
    PATH="/venv/bin:$PATH" \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 系统依赖 + 时区 + git safe.directory + 空 venv
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl gcc python3-dev build-essential libffi-dev \
    fonts-noto-cjk tzdata \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && git config --global --add safe.directory '*' \
    && uv venv /venv --seed \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Playwright Python 包 + Chromium 及其浏览器依赖
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install playwright \
    && playwright install --with-deps chromium \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
