"""评测脚本 IO 工具。

集中处理：

- JSON / JSONL 读写与解析；
- 增量更新辅助：扫描已有 ``answers.json``，收集已处理的 ``question_id``；
- 评测数据加载：支持 ``list[dict]`` 的 JSON 与 ``jsonl``。
"""

from __future__ import annotations

import os
import json
from typing import Any, Set, Dict, List, Tuple, Optional

# ─────────────────────────────────────────────
# 基础 IO
# ─────────────────────────────────────────────


def load_json(path: str) -> Any:
    """加载 JSON 文件并返回 Python 对象。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_eval_data(json_path: str) -> List[Dict[str, Any]]:
    """加载评测数据文件（顶层为 ``List[Dict]`` 的 JSON）。

    与 :func:`load_json` 的差别是会打印加载条数，便于评测脚本直接调用。
    """
    data = load_json(json_path)
    if isinstance(data, list):
        print(f"[Loader] 已加载 {len(data)} 条记录，来自 {json_path}")
    return data


def dump_json(path: str, data: Any) -> None:
    """把 Python 对象以格式化 JSON 写入文件（原子覆盖：写 temp 再 os.replace）。

    probe/judge 每条结果都落盘，中途 Ctrl-C 不会截断出半个 JSON 坏掉续跑文件。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    """按行加载 JSONL 文件，每行一个 JSON 对象。"""
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


# ─────────────────────────────────────────────
# 增量更新
# ─────────────────────────────────────────────


def load_existing_answers(output_dir: str) -> Tuple[List[Dict[str, Any]], Set[str], Optional[str]]:
    """加载 ``output_dir/answers.json``，用于增量更新。

    返回 ``(existing_results, existing_ids, answers_file)``。
    没有已有结果时 ``answers_file`` 为 ``None``，由调用方决定默认文件名。
    """
    answers_file = os.path.join(output_dir, "answers.json")
    if not os.path.isfile(answers_file):
        return [], set(), None

    try:
        with open(answers_file, "r", encoding="utf-8") as f:
            existing_results = json.load(f)
        if not isinstance(existing_results, list):
            print(f"[Resume] 已有文件格式异常，忽略: {answers_file}")
            return [], set(), None
        existing_ids = {
            item["question_id"] for item in existing_results if isinstance(item, dict) and "question_id" in item
        }
        print(f"[Resume] 已加载 {len(existing_results)} 条已有结果，来自 {answers_file}")
        print(f"[Resume] 已处理 {len(existing_ids)} 道题目，将跳过这些题目")
        return existing_results, existing_ids, answers_file
    except Exception as e:
        print(f"[Resume] 读取已有结果失败: {e}")
        return [], set(), None


def read_existing_ids(path: str, id_field: str = "question_id") -> Set[str]:
    """从一个 JSON/JSONL 文件中读取所有记录的 ``id_field``，组成集合。

    用于断点续跑 / 增量评测时跳过已处理项。
    """
    ids: Set[str] = set()
    if not os.path.isfile(path):
        return ids

    if path.endswith(".jsonl"):
        records = load_jsonl(path)
    else:
        try:
            obj = load_json(path)
        except Exception:
            return ids
        records = obj if isinstance(obj, list) else [obj]

    for r in records:
        if isinstance(r, dict) and id_field in r:
            ids.add(str(r[id_field]))
    return ids
