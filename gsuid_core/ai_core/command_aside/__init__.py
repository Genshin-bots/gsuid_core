"""命令回执之后的命令补充。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import command_with_aside as command_with_aside

__all__ = ["command_with_aside"]


def __getattr__(name: str) -> object:
    # 包初始化不拉 runner。runner 顶层 import SV，SV 再 import Bot，
    # Bot 又要 import 本包的 capture，顶层互指会成环。
    if name == "command_with_aside":
        from .runner import command_with_aside

        return command_with_aside
    raise AttributeError(name)
