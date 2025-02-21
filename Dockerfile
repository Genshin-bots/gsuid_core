# 基准镜像更新版本至官方文档推荐版本：3.12
FROM m.daocloud.io/docker.io/library/python:3.12-slim-bullseye

WORKDIR /app/gsuid_core
# 暴露 8765 端口
EXPOSE 8765
ENV PATH="${PATH}:/root/.local/bin"
ENV TZ=Asia/Shanghai

# 调整层顺序，这些安装是固定的，单独分层
RUN echo build start ---------------------------- \
    && apt-get update -y \
    && apt install curl git -y \
    && apt-get autoremove \
    && apt-get clean \
    && pip install --no-cache-dir --upgrade pip \
    && pip install uv

# 下面的内容与项目代码相关，有可能变换，单独分层
ADD ./ /app/
RUN uv sync \
    && echo build end ----------------------------
# 不需要删除 WORKDIR 中的内容，这是主程序所在的文件夹
# && rm -rf /app/*

# 使用传入的 TZ 环境变量设定时间和时区，默认为 Asia/Shanghai
CMD cp /usr/share/zoneinfo/$TZ /etc/localtime; echo $TZ > /etc/timezone && \
    cd /app/gsuid_core \
    && echo service started, now create links for volume mapping \
    && /app/docker-init.sh & \
    uv run core