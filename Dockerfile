# 基准镜像更新版本至官方文档推荐版本：3.12 | Update base image to the version recommended by the official documentation: 3.12
FROM m.daocloud.io/docker.io/library/python:3.12-slim-bullseye

# 镜像工作路径保持与文档一致 | Keep the working directory consistent with the documentation
WORKDIR /gsuid_core
# 暴露 8765 端口 | Expose port 8765
EXPOSE 8765
ENV PATH="${PATH}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# 可选参数配置见 entrypoint 脚本 | Optional parameter configuration can be found in the entrypoint script

# 调整层顺序，这些安装是固定的，单独分层 | Adjust layer order, these installations are fixed, separate layer
RUN echo build start ---------------------------- \
    && apt-get update -y \
    && apt install curl git -y \
    && apt-get autoremove \
    && apt-get clean \
    && pip install --no-cache-dir --upgrade pip \
    && pip install uv

# 下面的内容与项目代码相关，有可能变换，单独分层 | The following content is related to project code and may change, separate layer
# 代码添加到根目录下，保证路径与文档一致 | Add code to the root directory to ensure the path is consistent with the documentation
ADD ./ /gsuid_core/
# 如果是海外用户，删除 uv.toml 中镜像加速相关设置，并更新 lock 文件中的包地址 | If you are an overseas user, delete the mirror acceleration settings in uv.toml and update the package addresses in the lock file
RUN uv sync -index "https://pypi.org/simple" && \
    chmod +x /gsuid_core/docker-entrypoint.sh && \
    echo build end ----------------------------

# 将需要初始化的一些代码放到 entrypoint 中 | Put some initialization code into the entrypoint
ENTRYPOINT [ "/gsuid_core/docker-entrypoint.sh" ]

# 最后启动服务 | Finally, start the service
CMD ["uv", "run", "core"]
