#!/bin/bash

# 初始化步骤
set -e
echo "[ docker-entrypoint ] init script in..."

# 可选参数：根据传入的环境变量 TZ 设定时区
if [[ $TZ ]]; then
    cp /usr/share/zoneinfo/$TZ /etc/localtime
    echo $TZ > /etc/timezone
	echo "[ docker-entrypoint ] 设置时区为：$TZ..."
fi

# 可选参数：根据传入的环境变量 GSCORE_HOST 设置 HOST 参数
if [[ $GSCORE_HOST ]]; then
    if [[ ! -f /gsuid_core/data/config.json ]]; then
        echo "{ \"HOST\": \"$GSCORE_HOST\" }" > /gsuid_core/data/config.json
    else
        echo "[ docker-entrypoint ] config.json 配置文件已存在，容器不是初次启动，忽略 HOST 参数..."
    fi
fi

echo "[ docker-entrypoint ] init script OK!..."
# 执行传入的 CMD
exec "$@"
