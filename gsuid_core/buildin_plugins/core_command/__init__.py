"""init"""

from gsuid_core.sv import Plugins

Plugins(
    name="core_command",
    force_prefix=["core", "Core", "CORE"],
    allow_empty_prefix=False,
)
