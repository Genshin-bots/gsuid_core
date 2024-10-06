import json
from pathlib import Path
from typing import TypeVar, Callable, Optional, Awaitable

import aiofiles

from gsuid_core.version import __version__
from gsuid_core.data_store import data_cache_path

T = TypeVar('T')
_HEADER = {'User-Agent': f'gsuid-utils/{__version__}'}


def cache_data(func: Callable[..., Awaitable[T]]):
    async def wrapper(*args, **kwargs) -> Optional[T]:
        id = (
            kwargs.get(
                'id', kwargs.get('name', args[0] if args else func.__name__)
            )
            or func.__name__
        )
        cache_dir: Path = (
            kwargs.get('cache_path', data_cache_path) or data_cache_path
        )
        cache_path = cache_dir / func.__name__
        cache_path.mkdir(parents=True, exist_ok=True)

        if cache_path and cache_path.exists() and cache_path.is_dir():
            cache_file = cache_path / f'{id}.json'
            if cache_file.exists():
                async with aiofiles.open(cache_file, 'r') as file:
                    data = await file.read()
                    return json.loads(data)  # 返回已缓存的数据

        # 如果没有缓存，调用原始函数获取数据
        result = await func(*args, **kwargs)

        # 如果获取了数据，保存到缓存文件
        if cache_path and cache_path.exists() and cache_path.is_dir():
            cache_file = cache_path / f'{id}.json'
            async with aiofiles.open(cache_file, 'w') as file:
                await file.write(json.dumps(result))

        return result

    return wrapper
