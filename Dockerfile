# 基准镜像更新版本至官方文档推荐版本：3.12
FROM docker.io/library/python:3.12-slim-bullseye

# 镜像工作路径保持与文档一致
WORKDIR /gsuid_core
# 暴露 8765 端口
EXPOSE 8765
ENV PATH="${PATH}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# 可选参数配置见 entrypoint 脚本

# 调整层顺序，这些安装是固定的，单独分层
RUN echo build start ---------------------------- \
    && apt-get update -y \
    && apt install curl git -y \
    && apt-get autoremove \
    && apt-get clean \
    && pip install --no-cache-dir --upgrade pip \
    && pip install uv

# 下面的内容与项目代码相关，有可能变换，单独分层
# 代码添加到根目录下，保证路径与文档一致
ADD ./ /gsuid_core/
# 如果是海外用户，删除 uv.toml 中镜像加速相关设置
RUN sed -i '/\[\[index\]\]/,/default = true/d' uv.toml && \
    uv sync \
    && chmod +x /gsuid_core/docker-entrypoint.sh \
    && echo build end ----------------------------

# 将需要初始化的一些代码放到 entrypoint 中
ENTRYPOINT [ "/gsuid_core/docker-entrypoint.sh" ]
CMD ["uv", "run", "core"]
