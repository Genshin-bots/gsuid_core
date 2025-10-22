import json
import time
import base64
import inspect
from pathlib import Path
from functools import wraps
from typing import Dict, List, Tuple, Union

import aiofiles
from PIL import Image

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.image.convert import convert_img, convert_img_sync

IMAGE_CACHE = get_res_path('IMAGE_CACHE')

CACHE: Dict[float, Dict[str, Union[Path, str]]] = {}


def gs_cache(expire_time=3600):
    def wrapper(func):

        is_coroutine = inspect.iscoroutinefunction(func)

        if is_coroutine:

            @wraps(func)
            async def inner_async(*args, **kwargs):
                time_key = time.time()
                all_args = list(args) + list(kwargs.values())
                file_key = func.__name__
                for arg in all_args:
                    if isinstance(arg, (str, int, float, bool, Tuple, Path)):
                        file_key += '_' + repr(arg)
                    elif isinstance(arg, Dict):
                        file_key += '_' + str(
                            hash(json.dumps(arg, sort_keys=True))
                        )
                    elif isinstance(arg, List):
                        file_key += '_' + str(hash(json.dumps(arg)))
                    else:
                        continue

                if not file_key:
                    file_key = repr(func.__name__)

                result = _value = None
                WILL_DELETE = []

                logger.trace(f'{func.__name__} 开始缓存...')
                logger.trace(CACHE)

                for key in CACHE:
                    value = CACHE[key]
                    if time_key - key <= expire_time:
                        if file_key in value:
                            _value = value[file_key]
                            logger.trace(f'{func.__name__} 命中缓存 {_value}')
                            break
                    else:
                        WILL_DELETE.append(key)
                else:
                    result = await func(*args, **kwargs)

                if WILL_DELETE:
                    for key in WILL_DELETE:
                        if key in CACHE:
                            del CACHE[key]

                if _value is not None:
                    if isinstance(_value, Path):
                        result = await convert_img(_value)
                    else:
                        result = _value
                elif result is not None:
                    img_data = None
                    cache_target = IMAGE_CACHE / f'{time_key}_{file_key}.jpg'
                    if isinstance(result, Image.Image):
                        result.save(cache_target)
                    elif isinstance(result, bytes):
                        img_data = result
                    elif isinstance(result, str) and result.startswith(
                        'base64://'
                    ):
                        img_data = base64.b64decode(result[9:])
                    else:
                        cache_target = result

                    if img_data:
                        async with aiofiles.open(cache_target, 'wb') as f:
                            await f.write(img_data)

                    if time_key not in CACHE:
                        CACHE[time_key] = {}
                    if file_key not in CACHE[time_key]:
                        CACHE[time_key][file_key] = cache_target

                    logger.trace(f'{func.__name__} 进入缓存...')

                return result

            return inner_async
        else:

            @wraps(func)
            def inner_sync(*args, **kwargs):
                time_key = time.time()
                all_args = list(args) + list(kwargs.values())
                file_key = ''
                for arg in all_args:
                    if isinstance(arg, (str, int, float, bool, Tuple, Path)):
                        file_key += '_' + repr(arg)
                    elif isinstance(arg, Dict):
                        file_key += '_' + str(
                            hash(json.dumps(arg, sort_keys=True))
                        )
                    elif isinstance(arg, List):
                        file_key += '_' + str(hash(json.dumps(arg)))
                    else:
                        continue

                if not file_key:
                    file_key = repr(func.__name__)

                result = _value = None
                WILL_DELETE = []

                logger.trace(f'{func.__name__} 开始缓存...')
                logger.trace(CACHE)

                for key in CACHE:
                    value = CACHE[key]
                    if time_key - key <= expire_time:
                        if file_key in value:
                            _value = value[file_key]
                            logger.trace(f'{func.__name__} 命中缓存 {_value}')
                            break
                    else:
                        WILL_DELETE.append(key)
                else:
                    result = func(*args, **kwargs)

                if WILL_DELETE:
                    for key in WILL_DELETE:
                        if key in CACHE:
                            del CACHE[key]

                if _value is not None:
                    if isinstance(_value, Path):
                        result = convert_img_sync(_value)
                    else:
                        result = _value
                elif result is not None:
                    img_data = None
                    cache_target = IMAGE_CACHE / f'{time_key}_{file_key}.jpg'
                    if isinstance(result, Image.Image):
                        result.save(cache_target)
                    elif isinstance(result, bytes):
                        img_data = result
                    elif isinstance(result, str) and result.startswith(
                        'base64://'
                    ):
                        img_data = base64.b64decode(result[9:])
                    else:
                        cache_target = result

                    if img_data:
                        with open(cache_target, 'wb') as f:
                            f.write(img_data)

                    if time_key not in CACHE:
                        CACHE[time_key] = {}
                    if file_key not in CACHE[time_key]:
                        CACHE[time_key][file_key] = cache_target

                    logger.trace(f'{func.__name__} 进入缓存...')

                return result

            return inner_sync

    return wrapper
