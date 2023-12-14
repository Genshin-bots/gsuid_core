from typing import Dict, TypedDict


class GlobalVal(TypedDict):
    receive: int
    send: int
    command: int
    group: Dict[str, Dict[str, int]]


global_val: GlobalVal = {
    'receive': 0,
    'send': 0,
    'command': 0,
    'group': {},
}
