from typing import Dict, Tuple

from gsuid_core.utils.api.mys_api import mys_api

_REQUIRED_DEVICE_KEYS = (
    "deviceModel",
    "deviceProduct",
    "deviceName",
    "deviceBoard",
    "oaid",
    "deviceFingerprint",
)


_FP_KEYS = ("fp", "device_fp")
_DEVICE_ID_KEYS = ("device_id", "deviceId")
_DEVICE_INFO_KEYS = ("device_info", "deviceInfo", "deviceFingerprint")


def _first_non_empty(data: Dict[str, str], keys: Tuple[str, ...]) -> str:
    for key in keys:
        if key in data:
            val = data[key].strip()
            if val:
                return val
    return ""


async def deal_fp(data: Dict[str, str]) -> Tuple[str, str, str]:
    fp = _first_non_empty(data, _FP_KEYS)
    device_id = _first_non_empty(data, _DEVICE_ID_KEYS)

    if fp and device_id:
        device_info = _first_non_empty(data, _DEVICE_INFO_KEYS) or "OnePlus/PHK110/OP5913L1:14/SKQ1.221119.001"
        return fp, device_id, device_info

    missing = [key for key in _REQUIRED_DEVICE_KEYS if key not in data or not data[key].strip()]
    if missing:
        raise ValueError(f"缺少设备字段: {', '.join(missing)}")

    resolved_device_id = device_id or mys_api.get_device_id()
    seed_id, seed_time = mys_api.get_seed()
    generated_fp = await mys_api.generate_fp(
        resolved_device_id,
        data["deviceModel"].strip(),
        data["deviceProduct"].strip(),
        data["deviceName"].strip(),
        data["deviceBoard"].strip(),
        data["oaid"].strip(),
        data["deviceFingerprint"].strip(),
        seed_id,
        seed_time,
    )
    return generated_fp, resolved_device_id, data["deviceFingerprint"].strip()
