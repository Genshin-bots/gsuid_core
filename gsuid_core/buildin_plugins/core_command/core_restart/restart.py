import os
import time
import platform
import subprocess
from typing import Optional
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.server import core_shutdown_execute
from gsuid_core.version import __version__
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.startup_info import core_startup_info
from gsuid_core.utils.database.models import CoreUser, Subscribe
from gsuid_core.utils.plugins_update.utils import check_start_tool
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

bot_start = Path(__file__).parents[3] / "core.py"
restart_sh_path = Path().cwd() / "gs_restart.sh"
update_log_path = Path(__file__).parent / "update_log.json"

_restart_sh = """#!/bin/bash
kill -9 {}
{} &"""


def get_restart_command():
    is_use_custom_restart_command = core_plugins_config.get_config("is_use_custom_restart_command").data
    if is_use_custom_restart_command:
        restart_command = core_plugins_config.get_config("restart_command").data
        logger.info(t("[Core重启] 使用自定义重启命令: {restart_command}", restart_command=restart_command))
        return restart_command
    else:
        tool = check_start_tool()
        if tool == "uv":
            command = "uv run core"
        elif tool == "pdm":
            command = "pdm run core"
        elif tool == "poetry":
            command = "poetry run core"
        elif tool == "python":
            command = "python -m gsuid_core.core"
        else:
            command = "python -m gsuid_core.core"
        logger.info(t("[Core重启] 使用默认重启命令: {command}", command=command))
        return command


async def get_restart_sh() -> str:
    args = f"{get_restart_command()} {str(bot_start.absolute())}"
    return _restart_sh.format(str(bot_start.absolute()), args)


async def restart_genshinuid(
    event: Optional[Event] = None,
    is_send: bool = True,
) -> None:
    pid = os.getpid()
    restart_sh = await get_restart_sh()
    with open(restart_sh_path, "w", encoding="utf8") as f:
        f.write(restart_sh)

    if platform.system() == "Linux":
        # os.system(f'chmod +x {str(restart_sh_path)}')
        # os.system(f'chmod +x {str(bot_start)}')
        pass

    now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))

    if is_send and event:
        await gs_subscribe.add_subscribe(
            subscribe_type="session",
            task_name="[早柚核心] Restart",
            event=event,
            extra_message=now_time,
        )

    await core_shutdown_execute()

    if platform.system() == "Linux":
        subprocess.Popen(
            f"kill -9 {pid} ; sleep 1 ; {get_restart_command()}",
            shell=True,
        )
    elif platform.system() == "Darwin":
        # macOS (Darwin)
        subprocess.Popen(
            f"kill -9 {pid} ; sleep 1 ; {get_restart_command()}",
            shell=True,
        )
    else:
        # Windows
        # 加入 timeout /t 2 /nobreak 来等待 2 秒，确保旧进程彻底死亡，文件锁释放
        subprocess.Popen(
            f"taskkill /F /PID {pid} & timeout /t 2 /nobreak > NUL & {get_restart_command()}",
            shell=True,
        )


async def restart_message():
    if update_log_path.exists():
        update_log_path.unlink()

    datas = await gs_subscribe.get_subscribe(
        task_name="[早柚核心] Restart",
    )
    if datas:
        now_timestamp = time.time()
        now_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_timestamp))
        data = datas[0]
        # 推送给主人，按主人的语言偏好（用户自定义 > 全局）
        lang = await CoreUser.get_user_lang(data.user_id)
        duration_msg = ""
        if core_startup_info.duration is not None:
            duration_msg = t("\n耗时: {p0:.2f}s", lang=lang, p0=core_startup_info.duration)
            if data.extra_message:
                try:
                    shutdown_timestamp = time.mktime(time.strptime(data.extra_message, "%Y-%m-%d %H:%M:%S"))
                    restart_duration = now_timestamp - shutdown_timestamp
                    duration_msg += f" / {restart_duration:.1f}s"
                except ValueError:
                    pass
        await data.send(
            t(
                "🚀 重启完成!\n关机时间: {extra_message}\n重启时间: {now_time}{duration_msg}\n版本: {version}",
                lang=lang,
                extra_message=data.extra_message,
                now_time=now_time,
                duration_msg=duration_msg,
                version=__version__,
            )
        )
        await Subscribe.delete_row(task_name="[早柚核心] Restart")
    else:
        logger.warning(t("[Core重启] 没有找到[Core重启]的订阅, 无推送消息！"))
