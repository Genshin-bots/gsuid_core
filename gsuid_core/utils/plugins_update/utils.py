from typing import List

import psutil

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

start_venv: str = core_plugins_config.get_config('StartVENV').data


def get_command_chain() -> List[str]:
    cmd_chain = []
    process = psutil.Process()
    while process:
        try:
            cmd_chain.extend(process.cmdline())
            process = process.parent()
        except Exception as e:
            logger.warning(f'获取命令链失败...{e}')
            break
    return cmd_chain


def check_start_tool(is_pip: bool = False):
    command_chain = get_command_chain()
    command_chain = [command.lower() for command in command_chain]
    command_chain_str = ' '.join(command_chain)
    logger.debug(f'[检测启动工具] 命令链: {command_chain}')

    PDM = 'pdm'
    POETRY = 'poetry'
    UV = 'uv'
    OTHER = start_venv.strip()
    PYTHON = 'python'
    if OTHER == 'auto':
        OTHER = PYTHON

    if is_pip:
        PIP = ' run python -m pip'
        PDM += PIP
        POETRY += ' run pip'
        UV += PIP
        PYTHON += ' -m pip'

        if OTHER == 'python' or OTHER == 'auto':
            OTHER = 'python -m pip'
        else:
            OTHER += PIP

    if start_venv == 'auto':
        if 'pdm' in command_chain_str:
            command = PDM
        elif 'poetry' in command_chain_str:
            command = POETRY
        elif 'uv' in command_chain or 'uv.exe' in command_chain_str:
            command = UV
        else:
            command = PYTHON
    elif start_venv == 'pdm':
        command = PDM
    elif start_venv == 'poetry':
        command = POETRY
    elif start_venv == 'uv':
        command = UV
    else:
        command = OTHER

    return command
