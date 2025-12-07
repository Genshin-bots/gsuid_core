import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parents[5]))
sys.path.append(str(Path(__file__).parents[2]))
__package__ = "gsuid_core.utils"

from gsuid_core.utils.cookie_manager.add_fp import deal_fp  # noqa: E402


async def main():
    data = {}
    fp, device_id, device_info = await deal_fp(data)
    print(f"FP: {fp}")
    print(f"Device ID: {device_id}")
    print(f"Device Info: {device_info}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
