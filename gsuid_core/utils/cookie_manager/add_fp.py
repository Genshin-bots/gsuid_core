from typing import Dict

from gsuid_core.utils.api.mys_api import mys_api


async def deal_fp(data: Dict):
    if "fp" in data and ("device_id" in data or "deviceId" in data):
        fp = data["fp"]
        if "device_id" in data:
            device_id = data["device_id"]
        else:
            device_id = data["deviceId"]

        if "device_info" in data:
            device_info = data["device_info"]
        elif "deviceInfo" in data:
            device_info = data["deviceInfo"]
        else:
            device_info = "Unknown/Unknown/Unknown/Unknown"
    else:
        device_id = mys_api.get_device_id()
        seed_id, seed_time = mys_api.get_seed()
        fp = await mys_api.generate_fp(
            device_id,
            data["deviceModel"],
            data["deviceProduct"],
            data["deviceName"],
            data["deviceBoard"],
            data["oaid"],
            data["deviceFingerprint"],
            seed_id,
            seed_time,
        )
        device_info = data["deviceFingerprint"]

    return fp, device_id, device_info
