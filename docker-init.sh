#!/bin/bash

# 设定时区配置
cp /usr/share/zoneinfo/$TZ /etc/localtime
echo $TZ > /etc/timezone


# 延时 10 秒
sleep 10

# 执行指定操作，将初始生成的配置文件链接到映射目录
echo "service started, now create links for volume mapping"

# 检查是否创建软链接，如果没有就创建软链接
# 检查是否创建软链接，如果没有就创建软链接
# if [ ! -L /docker_mapping/data ]; then
#     ln -s /app/data/ /docker_mapping/data/
# fi

# if [ ! -L /docker_logs ]; then
#     ln -s /app/data/logs/ /docker_mapping/logs/
# fi

# if [ ! -L /docker_plugins ]; then
#     ln -s /app/data/plugins/ /docker_mapping/plugins/
# fi

echo "mapping complete!!!"