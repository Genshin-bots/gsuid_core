import sys
import time
import random
import asyncio
import statistics
from typing import Union
from collections import deque

from models import Message, MessageReceive
from msgspec import json as msgjson
from websockets.exceptions import ConnectionClosedError
from websockets.asyncio.client import ClientConnection, connect as ws_connect

from gsuid_core.i18n import t

sys.path.append("..")

# 配置参数
TOTAL_REQUESTS = 600  # 总发送量
TARGET_DURATION = 5.0  # 目标发送持续时间 (秒)
WAIT_TIMEOUT = 25  # 发送结束后，额外等待接收的最长时间 (秒)

# 计算平均发送间隔
AVG_INTERVAL = TARGET_DURATION / TOTAL_REQUESTS
MAX_JITTER = AVG_INTERVAL * 2


class GsBenchmarkClient:
    def __init__(self):
        self.ws: ClientConnection | None = None
        self.ws_url = ""
        self.recv_count = 0
        self.running = True

        # 新增：用于计算延迟
        self.sent_timestamps = deque()  # 存放发送时间戳
        self.latencies = []  # 存放计算出的延迟 (毫秒)

        # 新增：完成信号
        self.finish_event = asyncio.Event()

    @classmethod
    async def async_connect(cls, IP: str = "localhost", PORT: Union[str, int] = "8765"):
        self = cls()
        self.ws_url = f"ws://{IP}:{PORT}/ws/Nonebot"
        print(t("[-] 正在连接至 {p0} ...", p0=self.ws_url))
        self.ws = await ws_connect(self.ws_url, max_size=2**25, open_timeout=30, ping_interval=None)
        print(t("[+] 连接成功！准备开始基准测试"))
        return self

    async def recv_loop(self):
        """后台接收任务"""
        assert self.ws is not None
        try:
            async for _ in self.ws:
                recv_time = time.perf_counter()
                self.recv_count += 1

                # --- 延迟计算逻辑 (FIFO) ---
                if self.sent_timestamps:
                    # 取出最早的一个发送时间
                    send_time = self.sent_timestamps.popleft()
                    # 计算延迟 (转为毫秒)
                    latency_ms = (recv_time - send_time) * 1000
                    self.latencies.append(latency_ms)

                # --- 结束条件判断 ---
                # 如果接收数量达到了发送总量，触发结束信号
                if self.recv_count >= TOTAL_REQUESTS:
                    if not self.finish_event.is_set():
                        self.finish_event.set()

        except ConnectionClosedError:
            if self.running:
                print(t("[!] 连接意外断开"))
                # 如果断连，强制触发结束，避免主程序死锁
                self.finish_event.set()
        except Exception as e:
            if self.running:
                print(t("[!] 接收循环异常: {e}", e=e))

    async def generate_random_msg(self, index):
        """生成随机测试消息"""
        content_str_List = ["我的自选", "ww帮助", "gs帮助"]
        content_str = random.choice(content_str_List)

        content = Message(type="text", data=content_str)
        group_id = random.choice(["8888", "88888"])

        msg = MessageReceive(
            bot_id="console",
            bot_self_id="3399214199",
            user_type="direct",
            user_pm=0,
            group_id=group_id,
            user_id="99999",
            content=[content],
        )
        return msgjson.encode(msg)

    async def run_benchmark(self):
        assert self.ws is not None
        print(
            t(
                "[*] 开始测试：计划在 {TARGET_DURATION} 秒内发送 {TOTAL_REQUESTS} 条消息...",
                TARGET_DURATION=TARGET_DURATION,
                TOTAL_REQUESTS=TOTAL_REQUESTS,
            )
        )

        start_time = time.perf_counter()

        for i in range(TOTAL_REQUESTS):
            msg_bytes = await self.generate_random_msg(i)

            # 1. 记录发送时间 (先记录再发，或者发完立即记录，误差极小)
            t_send = time.perf_counter()
            await self.ws.send(msg_bytes)
            self.sent_timestamps.append(t_send)

            # 2. 模拟抖动
            sleep_time = random.uniform(0, MAX_JITTER)
            await asyncio.sleep(sleep_time)

            if (i + 1) % 100 == 0:
                print(t("    -> 已发送: {p0}/{TOTAL_REQUESTS}", p0=i + 1, TOTAL_REQUESTS=TOTAL_REQUESTS))

        send_duration = time.perf_counter() - start_time
        print(t("[*] 发送完毕，耗时 {send_duration:.2f}s。等待剩余响应...", send_duration=send_duration))
        return send_duration

    async def start(self):
        recv_task = asyncio.create_task(self.recv_loop())

        try:
            # 1. 执行发送
            await self.run_benchmark()

            # 2. 等待接收完成 (设置超时时间)
            try:
                # 等待 finish_event 被 set，或者超时
                await asyncio.wait_for(self.finish_event.wait(), timeout=WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                print(
                    t(
                        "[!] 等待响应超时 (超过 {WAIT_TIMEOUT}秒)，部分消息可能未收到或服务器处理积压。",
                        WAIT_TIMEOUT=WAIT_TIMEOUT,
                    )
                )

            # 3. 打印详细报告
            self.print_report()

        finally:
            self.running = False
            if self.ws is not None:
                await self.ws.close()
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass

    def print_report(self):
        print("\n" + "=" * 40)
        print(t("          基准测试报告"))
        print("=" * 40)

        # 基础数据
        lost_count = TOTAL_REQUESTS - self.recv_count
        print(t("请求总数 : {TOTAL_REQUESTS}", TOTAL_REQUESTS=TOTAL_REQUESTS))
        print(t("成功接收 : {p0}", p0=self.recv_count))
        print(t("丢包/未回: {lost_count} ({p0:.1f}%)", lost_count=lost_count, p0=lost_count / TOTAL_REQUESTS * 100))

        if self.latencies:
            # 延迟统计
            avg_lat = statistics.mean(self.latencies)
            median_lat = statistics.median(self.latencies)
            max_lat = max(self.latencies)
            min_lat = min(self.latencies)
            # P95 (95% 的请求快于此时间)
            p95_lat = statistics.quantiles(self.latencies, n=20)[18]

            # 计算吞吐量 (基于最后一个接收到的包的时间 - 第一个包发出的时间)
            # 这是一个近似值，如果需要更精确的吞吐量，可以记录 total_time

            print("-" * 40)
            print(t("延迟统计 (Latency):"))
            print(t("  平均 (Avg) : {avg_lat:.2f} ms", avg_lat=avg_lat))
            print(t("  中位 (Med) : {median_lat:.2f} ms", median_lat=median_lat))
            print(f"  P95 Line   : {p95_lat:.2f} ms")
            print(t("  最小 (Min) : {min_lat:.2f} ms", min_lat=min_lat))
            print(t("  最大 (Max) : {max_lat:.2f} ms", max_lat=max_lat))
        else:
            print(t("[-] 未收集到延迟数据 (未收到任何响应)"))

        print("=" * 40)


async def main():
    try:
        client = await GsBenchmarkClient.async_connect()
        await client.start()
    except OSError as e:
        print(t("[X] 无法连接到服务器: {e}", e=e))
    except Exception as e:
        print(t("[X] 发生错误: {e}", e=e))


if __name__ == "__main__":
    asyncio.run(main())
