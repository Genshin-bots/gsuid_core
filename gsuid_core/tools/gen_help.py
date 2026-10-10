"""
Deprecated: 此脚本用于从 Help.xlsx 生成 Help.json，属于一次性工具脚本。
框架运行时不再依赖此文件，后续版本将移除。

如需重新生成 Help.json，请手动执行此脚本（需安装 openpyxl）。
"""

sample = {
    "name": "",
    "desc": "",
    "eg": "",
    "need_ck": False,
    "need_sk": False,
    "need_admin": False,
}

result: dict[str, dict[str, list[dict[str, str | bool]]]] = {}
