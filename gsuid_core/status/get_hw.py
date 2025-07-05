import asyncio
import platform

import psutil

from gsuid_core.logger import logger

# --- 优化后的异步函数 ---


async def get_cpu_info():
    """异步获取CPU信息"""
    # 耗时操作：在线程中运行 psutil.cpu_percent
    # to_thread 会将阻塞函数放入线程池，避免阻塞事件循环
    usage_task = asyncio.to_thread(psutil.cpu_percent, interval=1)

    # 非耗时操作：可以同步执行
    cpu_name = "Unknown CPU"
    try:
        # 优先从 /proc/cpuinfo 获取 (Linux)
        with open('/proc/cpuinfo', 'r') as f:
            for line in f:
                if line.startswith('model name'):
                    cpu_name = line.split(': ')[1].strip()
                    break
    except (FileNotFoundError, IndexError):
        # 如果失败，尝试 platform.processor()
        try:
            cpu_name = platform.processor() or "Unknown CPU"
        except Exception as e:
            logger.error(f"获取CPU名称失败: {e}")
            cpu_name = "Unknown CPU"

    # 简化名称处理
    cpu_name = ' '.join(cpu_name.split()[:2])
    cores = psutil.cpu_count(logical=True)

    # 等待耗时操作完成
    usage = await usage_task

    return {"name": f"{cpu_name} ({cores}核)", "value": usage, "type": "CPU"}


async def get_memory_info():
    """异步获取内存信息（实际是IO密集度低，可以不改，但为了统一性改为async）"""

    # 这个函数本身非常快，但为了与其他异步函数统一，也包装一下
    def _get_mem():
        mem = psutil.virtual_memory()
        total_gb = round(mem.total / (1024**3), 1)
        used_mem_gb = round(mem.used / (1024**3), 1)
        usage_percent = mem.percent
        return {
            "name": f"{used_mem_gb}GB / {total_gb}GB",
            "value": usage_percent,
            "type": "Memory",
        }

    return await asyncio.to_thread(_get_mem)


def _get_disk_sync():
    """磁盘扫描的同步逻辑（这是一个潜在的I/O密集操作）"""
    total_size, used_size = 0, 0
    for part in psutil.disk_partitions(all=False):
        # 过滤掉非物理或特殊文件系统
        if 'fixed' in part.opts or part.fstype != '':
            try:
                usage = psutil.disk_usage(part.mountpoint)
                used_size += usage.used
                total_size += usage.total
            except (PermissionError, FileNotFoundError):
                continue
    return total_size, used_size


async def get_disk_info():
    """异步获取磁盘信息"""
    total_size, used_size = await asyncio.to_thread(_get_disk_sync)

    # 格式化显示
    def format_size(size_bytes):
        gb = size_bytes / (1024**3)
        if gb >= 1000:
            return f"{round(gb / 1024, 1)}TB"
        return f"{round(gb, 1)}GB"

    total_str = format_size(total_size)
    used_str = format_size(used_size)

    usage_percent = (
        round(used_size / total_size * 100, 1) if total_size > 0 else 0
    )
    return {
        "name": f"{used_str} / {total_str}",
        "value": usage_percent,
        "type": "Disk",
    }


async def get_network_info():
    """异步获取网络信息（逻辑基本不变，因为它已经是异步的了）"""
    # 异步获取两次流量统计
    before = psutil.net_io_counters()
    await asyncio.sleep(1)  # 这个sleep是并行的关键
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

    speed_max = 1000.0  # 默认值
    # 此部分获取最大带宽的逻辑已经是异步subprocess，无需大改
    try:
        if platform.system() == 'Linux':
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
                with open(
                    f'/sys/class/net/{default_interface}/speed', 'r'
                ) as f:
                    speed_max = float(f.read().strip())
        elif platform.system() == 'Windows':
            proc = await asyncio.create_subprocess_exec(
                'powershell',
                '-Command',
                "(Get-NetAdapter | Where-Object {$_.Status -eq 'Up'}).Speed",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            # Powershell可能返回多个值，取第一个有效的
            for line in stdout.decode().strip().splitlines():
                if line.isdigit():
                    speed_max = float(line) / 1e6  # 转换为 Mbps
                    break
    except Exception as e:
        logger.exception(f"获取最大网络带宽失败: {e}")

    usage_percent = (
        min(round((speed_current / speed_max) * 100, 1), 100)
        if speed_max > 0
        else 0
    )
    return {
        "name": f"{speed_max:.0f}Mbps",
        "value": usage_percent,
        "type": "Network",
    }


async def get_swap_info():
    """异步获取SWAP信息"""

    def _get_swap():
        swap = psutil.swap_memory()
        total_gb = swap.total / (1024**3)
        name_total = (
            f"{round(total_gb / 1024, 1)}TB"
            if total_gb >= 1000
            else f"{round(total_gb, 1)}GB"
        )
        used_gb = swap.used / (1024**3)
        name_used = f"{round(used_gb, 1)}GB"

        return {
            "name": f"{name_used} / {name_total}",
            "value": swap.percent,
            "type": "Swap",
        }

    return await asyncio.to_thread(_get_swap)
