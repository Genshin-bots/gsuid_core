"""统一写契约：各真值源写完正文后，用同一份元数据登记认知节点。

不搬正文。observe / FileOS / attach / artifact / record 都填
``MemoryWrite``，再 ``remember()``。失败只丢节点。
"""

from typing import Optional
from dataclasses import dataclass

from gsuid_core.ai_core.cognition.types import CogKind


@dataclass(frozen=True)
class MemoryWrite:
    """一次索引层写入。``ref`` 指向原库主键，``handle`` 供 ``read_handle``。"""

    kind: CogKind
    ref: str
    scope_key: str
    owner_user_id: str = ""
    title: str = ""
    summary: str = ""
    as_of: str = ""
    source: str = ""
    handle: str = ""
    canon: str = ""


async def remember(write: MemoryWrite) -> Optional[int]:
    """登记或刷新一条认知节点。原库写入必须已经成功。"""
    from gsuid_core.ai_core.cognition.nodes import sync_node

    if not write.ref:
        return None
    return await sync_node(
        write.kind,
        write.ref,
        scope_key=write.scope_key,
        owner_user_id=write.owner_user_id,
        title=write.title,
        summary=write.summary,
        as_of=write.as_of,
        source=write.source,
        handle=write.handle,
        canon=write.canon,
    )
