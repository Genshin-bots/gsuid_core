# 十七、代码规范红线

GsCore 对代码质量有严格要求，以下规则**绝对禁止**：

## 17.1 禁止事项

```python
# ❌ 禁止：try-except 兜底（掩盖类型和逻辑问题）
try:
    result = data.get("key")
except (AttributeError, KeyError):
    result = None

# ❌ 禁止：cast() 类型强制转换
from typing import cast
result = cast(str, some_value)

# ❌ 禁止：type: ignore 抑制自身代码的类型错误
data = some_function()  # type: ignore

# ❌ 禁止：getattr/dict.get 兜底
name = getattr(user, "name", None)
value = data.get("key", None)

# ❌ 禁止：同步阻塞函数（整个项目是异步的）
def fetch_data(url: str) -> dict:
    import requests
    return requests.get(url).json()
```

## 17.2 正确做法

```python
# ✅ 正确：Union + isinstance 守卫
from typing import Union

def process(result: str | int | None) -> str:
    if isinstance(result, int):
        return str(result)
    if result is None:
        return ""
    return result

# ✅ 正确：所有函数必须有类型注解
async def get_user(user_id: str, bot_id: str) -> GameBind | None:
    return await GameBind.get_bind(user_id, bot_id)

# ✅ 正确：TypedDict 代替裸字典
from typing import TypedDict

class CharData(TypedDict):
    level: int
    constellation: int
    weapon: str

# ✅ 正确：全部使用异步 I/O
async def fetch_data(url: str) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        return resp.json()
```

## 17.3 `ai_return` 辅助函数的特殊说明

`_ai_return_xxx()` 系列辅助函数是**唯一允许使用 `try/except`** 的地方，因为：
1. 它们是观测性代码，不属于业务逻辑
2. 提取失败绝对不能影响图片生成和发送
3. 失败时只记录 `logger.warning`，不 raise

```python
# ✅ 唯一允许 try/except 的地方
def _ai_return_xxx(data: dict) -> None:
    try:
        result = f"..."
        ai_return(result)
    except Exception as e:
        logger.warning(f"[插件名] ai_return 数据提取失败: {e}")
```
