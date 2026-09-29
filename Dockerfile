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

# 挂载模式下 venv 来自挂载卷，playwright 可能比镜像内烘焙的更新：入口幂等对齐一次。
# 装不上也必须放行 —— 框架层 ensure_chromium 会在启动后台再试并告警。
CMD ["sh", "-c", "playwright install chromium || true; exec uv run --python /venv/bin/python core --host 0.0.0.0"]

# ==========================================
# Bundle: 代码 + 依赖一起打入镜像
# ==========================================
FROM ${GSCORE_BUILTIN_BASE} AS bundle

ARG GSCORE_PYTHON_INDEX
EXPOSE 8765
WORKDIR /gsuid_core

COPY pyproject.toml README.md ./

# 代码 + 依赖一起打入镜像：uv sync 装的是 uv.lock 锁定的 playwright，
# 构建期就把对应 revision 的 chromium 烘进镜像（与基线镜像对齐时是秒级 no-op）。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --index ${GSCORE_PYTHON_INDEX} \
    && playwright install chromium

COPY . .

CMD ["uv", "run", "--python", "/venv/bin/python", "core", "--host", "0.0.0.0"]
