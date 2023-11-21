import httpx


async def sget(url: str):
    async with httpx.AsyncClient(timeout=None) as client:
        resp = await client.get(url=url)
        return resp
