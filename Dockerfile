FROM python:3.10.16-slim-bullseye

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
    && pip install --no-cache-dir --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    && pip install poetry -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
    && poetry install \
    && poetry lock \
    && rm -rf /app/*

CMD poetry run python3 core.py
