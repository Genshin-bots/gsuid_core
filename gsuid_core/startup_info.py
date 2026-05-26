from typing import Optional
from dataclasses import dataclass


@dataclass
class CoreStartupInfo:
    duration: Optional[float] = None


core_startup_info = CoreStartupInfo()
