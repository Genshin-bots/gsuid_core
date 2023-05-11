FROM python:3.8.12-slim

WORKDIR /app/gsuid_core

ENV PATH="${PATH}:/root/.local/bin"

ADD ./ /app/

RUN mv /etc/apt/sources.list /etc/apt/sources.list.bak \
	&& echo "deb http://ftp.cn.debian.org/debian/ bullseye main non-free contrib" >/etc/apt/sources.list \
    && echo "deb http://ftp.cn.debian.org/debian/ bullseye-updates main non-free contrib" >>/etc/apt/sources.list \
    && echo "deb http://ftp.cn.debian.org/debian/ bullseye-backports main non-free contrib" >>/etc/apt/sources.list \
    && echo "deb-src http://ftp.cn.debian.org/debian/ bullseye main non-free contrib" >>/etc/apt/sources.list \
    && echo "deb-src http://ftp.cn.debian.org/debian/ bullseye-updates main non-free contrib" >>/etc/apt/sources.list \
    && echo "deb-src http://ftp.cn.debian.org/debian/ bullseye-backports main non-free contrib" >>/etc/apt/sources.list \
    && echo "deb http://mirrors.ustc.edu.cn/debian-security/ stable-security main non-free contrib" >>/etc/apt/sources.list \
    && echo "deb-src http://mirrors.ustc.edu.cn/debian-security/ stable-security main non-free contrib" >>/etc/apt/sources.list \
    && rm -rf /var/lib/apt/lists/* && apt-get update

RUN apt install curl git -y

RUN /usr/local/bin/python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

RUN /usr/local/bin/python -m pip install  --no-cache-dir --upgrade --quiet pip

RUN /usr/local/bin/python -m pip install poetry

RUN poetry install && rm -rf /app/*

CMD poetry run python3 core.py
