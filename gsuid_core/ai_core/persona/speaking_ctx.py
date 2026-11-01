"""当前 assistant 自发话的人格名（ContextVar，零依赖）。

Bot.send / capture_bot 的被动 observe 没有 event 上下文，靠它在「本句是谁在说」
的窗口里拿到人格名，让 meme 知识写入落到正确的 self scope。
"""

from contextvars import Token, ContextVar

_speaking_persona: ContextVar[str] = ContextVar("speaking_persona", default="")


def set_speaking_persona(name: str) -> Token:
    return _speaking_persona.set(name)


def reset_speaking_persona(token: Token) -> None:
    _speaking_persona.reset(token)


def get_speaking_persona() -> str:
    return _speaking_persona.get()
