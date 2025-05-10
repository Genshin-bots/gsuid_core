import asyncio
import platform

import psutil

from gsuid_core.logger import logger


def get_cpu_info():
    cpu_name = None  # 初始化变量

    # 尝试从 /proc/cpuinfo 获取型号
    try:
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('model name'):
                    cpu_name = line.split(': ')[1].strip()
                    break  # 找到后立即退出循环
    except FileNotFoundError:
        logger.warning("未找到 /proc/cpuinfo 文件，尝试其他方式获取CPU名称")

    # 如果未从文件获取到名称，尝试其他方式
    if cpu_name is None:
        logger.debug(
            "未从 /proc/cpuinfo 获取到CPU名称，尝试 platform.processor()"
        )
        try:
            cpu_name = platform.processor() or "Unknown CPU"
        except Exception as e:
            logger.error(f"获取CPU名称失败: {e}")
            cpu_name = "Unknown CPU"

    # 找不到，干脆别找了，叫Unknown CPU也挺好，至少比后台报错前台半天没响应的好QAQ

    # 处理CPU名称格式
    try:
        cpu_name = ' '.join(cpu_name.split()[:2])
    except Exception:
        cpu_name = "Unknown CPU"

    # 获取核心数和使用率
    cores = psutil.cpu_count(logical=True)
    usage = psutil.cpu_percent(interval=1)

    return {"name": f"{cpu_name} ({cores}核)", "value": usage}


def get_memory_info():
    mem = psutil.virtual_memory()
    total_gb = round(mem.total / (1024**3), 1)
    used_mem_gb = round(mem.used / (1024**3), 1)

    # 最大内存
    usage_percent = mem.percent
    return {"name": f"{used_mem_gb}GB / {total_gb}GB", "value": usage_percent}


def get_disk_info():
    # 获取所有物理硬盘信息（跨平台）
    total_size = 0
    used_size = 0
    for part in psutil.disk_partitions(all=False):
        if (
            'fixed' in part.opts or part.fstype != ''
        ):  # 过滤可移动磁盘和虚拟分区
            try:
                usage = psutil.disk_usage(part.mountpoint)
                used_size += usage.used
                total_size += usage.total
            except PermissionError:
                continue  # 跳过无权限访问的分区

    # 转换为 TB/GB 显示
    total_gb = total_size / (1024**3)
    if total_gb >= 1000:
        total_tb = round(total_gb / 1024, 1)
        name = f"{total_tb}TB"
    else:
        name = f"{round(total_gb, 1)}GB"

    # 转换为TB 显示
    used_gb = used_size / (1024**3)
    if used_gb >= 1000:
        used_tb = round(used_gb / 1024, 1)
        used = f"{used_tb}TB"
    else:
        used = f"{round(used_gb, 1)}GB"
    name = f"{used} / {name}"

    # 使用总量百分比
    if total_size == 0:
        usage_percent = 0
    else:
        usage_percent = round(used_size / total_size * 100, 1)

    return {"name": name, "value": usage_percent}


async def get_network_info():
    # 异步获取两次流量统计
    before = psutil.net_io_counters()
    await asyncio.sleep(1)
    after = psutil.net_io_counters()
    speed_current = (
        (
            after.bytes_sent
            - before.bytes_sent
            + after.bytes_recv
            - before.bytes_recv
        )
        * 8
        / 1e6
    )  # Mbps

    # 异步获取最大带宽
    speed_max = 1000
    try:
        if platform.system() == 'Linux':
            # 异步执行命令：获取默认网络接口
            proc = await asyncio.create_subprocess_exec(
                'ip',
                'route',
                'show',
                'default',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            default_interface = (
                stdout.decode().split('dev ')[1].split()[0]
                if 'dev' in stdout.decode()
                else None
            )

            if default_interface:
                # 异步读取 /sys/class/net/{interface}/speed
                proc = await asyncio.create_subprocess_exec(
                    'cat',
                    f'/sys/class/net/{default_interface}/speed',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                speed_str = stdout.decode().strip()
                if speed_str.isdigit():
                    speed_max = float(speed_str)

        elif platform.system() == 'Windows':
            # 异步执行 PowerShell 命令
            proc = await asyncio.create_subprocess_exec(
                'powershell',
                '-Command',
                "(Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}).Speed",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode().strip()
            if output.isdigit():
                speed_max = float(output) / 1e6  # 转换为 Mbps
    except Exception as e:
        logger.exception(f"获取网络速度失败: {e}")

    usage_percent = min(round((speed_current / speed_max) * 100, 1), 100)
    return {"name": f"{speed_max:.0f}Mbps", "value": usage_percent}


def get_swap_info():
    swap = psutil.swap_memory()

    # 总容量格式化（GB/TB）
    total_gb = swap.total / (1024**3)
    if total_gb >= 1000:
        total_tb = round(total_gb / 1024, 1)
        name = f"{total_tb}TB"
    else:
        name = f"{round(total_gb, 1)}GB"

    # 已使用容量
    used_gb = swap.used / (1024**3)
    used = f"{round(used_gb, 1)}GB"
    name = f"{used} / {name}"

    # 使用率百分比（若无 SWAP 则显示 0）
    usage = swap.percent if swap.total > 0 else 0.0
    return {"name": name, "value": usage}
