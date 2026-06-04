#!/usr/bin/env python3
"""测试 PyPI 镜像源可用性与下载速度，并给出最快镜像源的启动命令。

用法: python3 check_pypi_mirrors.py
"""

import os
import re
import sys
import time
import unicodedata
import urllib.request
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

PROBE_PACKAGE = "pip"  # 探针包：所有镜像必有，按文件名排序选中的 wheel 约 1.3MB，体积适中
TIMEOUT = 10  # 单次请求超时（秒）
SPEED_MAX_BYTES = 3 * 1024 * 1024  # 测速最多下载 3MB
SPEED_MAX_SECONDS = 8  # 测速最长持续时间
BAR_WIDTH = 16  # 速度条最大宽度
UA = {"User-Agent": "pip/24.0 gscore-mirror-check"}

MIRRORS = [
    ("官方", "https://pypi.org/simple/"),
    ("阿里", "https://mirrors.aliyun.com/pypi/simple/"),
    ("腾讯云", "https://mirrors.cloud.tencent.com/pypi/simple/"),
    ("火山引擎", "https://mirrors.volces.com/pypi/simple/"),
    ("华为云", "https://mirrors.huaweicloud.com/repository/pypi/simple/"),
    ("清华大学", "https://pypi.tuna.tsinghua.edu.cn/simple/"),
    ("中国科学技术大学", "https://mirrors.ustc.edu.cn/pypi/simple/"),
    ("北京外国语大学", "https://mirrors.bfsu.edu.cn/pypi/web/simple/"),
    ("上海交通大学", "https://mirror.sjtu.edu.cn/pypi/web/simple/"),
    ("南京大学", "https://mirror.nju.edu.cn/pypi/web/simple/"),
]

# ANSI 颜色：仅终端输出时启用；os.system("") 用于激活 Windows 控制台的 ANSI 支持
TTY = sys.stdout.isatty()
if TTY and os.name == "nt":
    os.system("")
BOLD, DIM, GREEN, RED, CYAN, RESET = (
    ("\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[36m", "\033[0m") if TTY else ("",) * 6
)


def display_width(text: str) -> int:
    """计算终端显示宽度（CJK 字符占 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    """按显示宽度左对齐填充。"""
    return text + " " * max(0, width - display_width(text))


def check_latency(name: str, index_url: str) -> dict:
    """请求 simple 索引页，验证返回的是真实的包索引。"""
    url = urljoin(index_url, PROBE_PACKAGE + "/")
    result = {"name": name, "url": index_url, "ok": False, "latency": None, "error": ""}
    try:
        start = time.perf_counter()
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as resp:
            body = resp.read()
        if PROBE_PACKAGE.encode() not in body.lower():
            result["error"] = "响应内容异常（非包索引页）"
            return result
        result.update(ok=True, latency=time.perf_counter() - start, page=body, page_url=url)
    except Exception as exc:  # 任何失败都视为镜像不可用
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def measure_speed(page: bytes, page_url: str) -> tuple[float | None, str]:
    """从 simple 页面选定文件，限量下载并计算 MB/s。"""
    links = re.findall(rb'href="([^"]+)"', page)
    wheels = [link for link in links if b".whl" in link] or links
    if not wheels:
        return None, "页面中无文件链接"
    # 各镜像页面内文件顺序不一致，按文件名排序选取，保证所有镜像下载同一文件、结果可比
    file_url = urljoin(page_url, max(wheels, key=lambda link: link.rsplit(b"/", 1)[-1]).decode())
    try:
        start = time.perf_counter()
        received = 0
        with urllib.request.urlopen(urllib.request.Request(file_url, headers=UA), timeout=TIMEOUT) as resp:
            while received < SPEED_MAX_BYTES:
                chunk = resp.read(65536)
                if not chunk:
                    break
                received += len(chunk)
                if time.perf_counter() - start > SPEED_MAX_SECONDS:
                    break
        elapsed = time.perf_counter() - start
        if received == 0 or elapsed == 0:
            return None, "下载为空"
        return received / elapsed / 1024 / 1024, ""
    except Exception as exc:
        return None, f"文件下载失败: {type(exc).__name__}"


def print_table(results: list[dict]) -> None:
    """按速度降序打印结果表，附速度条。"""
    name_w = max(display_width(r["name"]) for r in results) + 2
    max_speed = max((r.get("speed") or 0 for r in results), default=0)
    print(f"{DIM}{pad('镜像', name_w)}     延迟          速度{RESET}")
    print(f"{DIM}{'─' * (name_w + 25 + BAR_WIDTH)}{RESET}")
    for i, r in enumerate(results):
        if not r["ok"]:
            print(f"{RED}{pad(r['name'], name_w)}{'-':>9}{'-':>14}  {r['error'][:60]}{RESET}")
            continue
        latency = f"{r['latency'] * 1000:7.0f}ms"
        speed = r.get("speed")
        if speed:
            bar = "█" * max(1, round(speed / max_speed * BAR_WIDTH))
            color = GREEN if i == 0 else ""
            print(f"{color}{pad(r['name'], name_w)}{latency}{speed:9.2f} MB/s  {bar}{RESET}")
        else:
            print(f"{pad(r['name'], name_w)}{latency}{'-':>14}  {DIM}{r.get('speed_err', '')}{RESET}")


def print_usage(results: list[dict]) -> None:
    """输出速度前三的镜像源链接，及最快源对应的三种启动命令。"""
    top = [r for r in results if r["ok"]][:3]
    url = top[0]["url"]
    manual = (
        f'$env:UV_DEFAULT_INDEX = "{url}"; uv run core'
        if os.name == "nt"
        else f"export UV_DEFAULT_INDEX={url} && uv run core"
    )
    name_w = max(display_width(r["name"]) for r in top) + 2
    print(f"🏆 推荐前 {len(top)}:")
    for i, r in enumerate(top, 1):
        speed = f"{r['speed']:5.2f} MB/s" if r.get("speed") else f"{'-':>10}"
        color = BOLD + GREEN if i == 1 else ""
        print(f"  {color}{i}. {pad(r['name'], name_w)}{speed}  {r['url']}{RESET}")
    print()
    print(f"{CYAN}▸ docker compose{RESET} {DIM}── 将下行写入 .env 后启动{RESET}")
    print(f"    GSCORE_PYTHON_INDEX={url}")
    print("    docker compose up -d --build\n")
    print(f"{CYAN}▸ docker run{RESET} {DIM}── 在原命令中追加参数{RESET}")
    print(f"    -e UV_DEFAULT_INDEX={url}\n")
    print(f"{CYAN}▸ 手动启动{RESET}")
    print(f"    {manual}")


def main() -> int:
    # Windows 旧版控制台默认 GBK 编码，强制 UTF-8 避免中文/符号输出报错
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    print(f"共 {len(MIRRORS)} 个镜像源，测试中（超时 {TIMEOUT}s）...\n")
    with ThreadPoolExecutor(max_workers=len(MIRRORS)) as pool:
        results = list(pool.map(lambda m: check_latency(*m), MIRRORS))

    # 测速串行执行，避免带宽互相挤占导致结果失真
    alive = [r for r in results if r["ok"]]
    for i, r in enumerate(alive, 1):
        print(f"\r测速中 ({i}/{len(alive)}): {r['name']}".ljust(40), end="", flush=True)
        r["speed"], r["speed_err"] = measure_speed(r.pop("page"), r.pop("page_url"))
    print("\r" + " " * 40 + "\r", end="")

    # 速度降序，测速失败按延迟升序，连不通的排最后
    results.sort(key=lambda r: (-(r.get("speed") or 0), r["latency"] or 9e9))
    print_table(results)

    print(f"\n可用: {len(alive)}/{len(results)}\n")
    if not alive:
        if any("CERTIFICATE_VERIFY_FAILED" in r["error"] for r in results):
            print("提示: SSL 证书验证失败。macOS 上 python.org 安装的 Python")
            print("需先运行 /Applications/Python 3.x/Install Certificates.command")
        return 1
    print_usage(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
