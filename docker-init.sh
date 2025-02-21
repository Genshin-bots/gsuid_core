#!/bin/bash

# 延时 10 秒
sleep 10

# 执行指定操作，将初始生成的配置文件链接到映射目录
echo "service started, now create links for volume mapping"

# 检查是否创建软链接，如果没有就创建软链接
[ ! -L /docker_mapping/data ] && ln -s /app/gsuid_core/data /docker_mapping/data
[ ! -L /docker_logs ] && ln -s /app/gsuid_core/logs /docker_logs
[ ! -L /docker_plugins ] && ln -s /app/gsuid_core/plugins /docker_plugins

echo "mapping complete!!!"