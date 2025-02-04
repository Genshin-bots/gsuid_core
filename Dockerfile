FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/python:3.10-slim-bullseye

ENV TZ=Asia/Shanghai

WORKDIR /app/gsuid_core

ENV PATH="${PATH}:/root/.local/bin"

ADD ./ /app/

RUN sed -i 's/http:\/\/deb.debian.org/http:\/\/ftp.cn.debian.org/g' /etc/apt/sources.list \
    && sed -i 's/http:\/\/security.debian.org/http:\/\/mirrors.ustc.edu.cn/g' /etc/apt/sources.list \
    && apt-get update -y \
    && apt-get upgrade -y \
    && apt install curl git -y \
    && apt-get autoremove \
    && apt-get clean \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && pip install --no-cache-dir --upgrade pip -i https://mirror.nju.edu.cn/pypi/web/simple/ \
    && pip install uv -i https://mirror.nju.edu.cn/pypi/web/simple/ \
    && uv sync \
    && rm -rf /app/*

CMD uv run core
