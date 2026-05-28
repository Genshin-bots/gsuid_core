# gsuid_core 业务镜像
# 基于 docker/base/Dockerfile 预构建的基础镜像(已包含 Python+uv+playwright+chromium+字体)

ARG GSCORE_BUILTIN_BASE=docker.cnb.cool/gscore-mirror/gsuid_core/gscore-uv-3.12:latest
ARG GSCORE_PYTHON_INDEX=https://pypi.org/simple

# ==========================================
# Runtime: 代码 + venv 通过 volume 挂载,镜像只提供环境
# ==========================================
FROM ${GSCORE_BUILTIN_BASE} AS runtime

EXPOSE 8765
WORKDIR /gsuid_core

CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "0.0.0.0"]

# ==========================================
# Bundle: 代码 + 依赖一起打入镜像
# ==========================================
FROM ${GSCORE_BUILTIN_BASE} AS bundle

ARG GSCORE_PYTHON_INDEX
EXPOSE 8765
WORKDIR /gsuid_core

COPY pyproject.toml README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --index ${GSCORE_PYTHON_INDEX}

COPY . .

CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "0.0.0.0"]
