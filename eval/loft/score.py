"""LOFT 判分纯函数。不扫描进程、不读 core token。"""

import re
import json

_ANSWER_IS_RE = re.compile(
    r"(?:The answer is|答案是)\s*[:：]\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)

PidGold = list[str] | list[list[str | int]] | str | None
SpanGold = str | list[str] | list[list[str]] | None


def extract_final_answer(text: str) -> str:
    if not text:
        return ""
    matched = _ANSWER_IS_RE.search(text)
    if matched:
        return matched.group(1).strip().splitlines()[0].strip()
    return text.strip()


def gold_pids(answers: PidGold) -> list[str]:
    if not isinstance(answers, list):
        return []
    out: list[str] = []
    for item in answers:
        if isinstance(item, list) and item:
            out.append(str(item[0]))
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return list(dict.fromkeys(out))


def gold_spans(answers: SpanGold) -> list[str]:
    if isinstance(answers, str) and answers.strip():
        return [answers.strip()]
    if not isinstance(answers, list):
        return []
    out: list[str] = []
    for item in answers:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return list(dict.fromkeys(out))


def parse_pid_list(blob: str) -> list[str]:
    raw_text = blob.strip()
    if raw_text.startswith("["):
        parsed: list[str] | None = None
        try:
            loaded = json.loads(raw_text)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, list):
            parsed = []
            for item in loaded:
                if isinstance(item, str) and item.strip():
                    parsed.append(item.strip())
                elif isinstance(item, int):
                    parsed.append(str(item))
        elif isinstance(loaded, str) and loaded.strip():
            parsed = [loaded.strip()]
        if parsed:
            return parsed
        # 答案行被截断时 JSON 不闭合，例如 ["9433958"
        ids = re.findall(r"\d{5,}", raw_text)
        if ids:
            return ids
    if (raw_text.startswith('"') and raw_text.endswith('"')) or (raw_text.startswith("'") and raw_text.endswith("'")):
        raw_text = raw_text[1:-1].strip()
    if raw_text:
        return [raw_text]
    return []


def recall_at_k(gold: list[str], pred: list[str], k: int) -> float:
    if not gold:
        return 1.0 if not pred else 0.0
    need = gold[:k] if k < len(gold) else gold
    hit = 0
    pred_k = pred[:k]
    for gid in need:
        if gid in pred_k:
            hit += 1
    return hit / len(need)


def retrieval_pass(gold: PidGold, agent: str) -> tuple[bool, float, str]:
    gids = gold_pids(gold)
    extracted = extract_final_answer(agent)
    pred = parse_pid_list(extracted) if extracted else []
    if not pred:
        blob = agent.lower()
        pred = [gid for gid in gids if gid.lower() in blob]
    rec1 = recall_at_k(gids, pred, 1)
    k = max(1, len(gids))
    recn = recall_at_k(gids, pred, k)
    ok = rec1 >= 1.0 if k == 1 else recn >= 1.0
    return ok, rec1, f"recall@1={rec1:.2f} recall@{k}={recn:.2f} pred={pred[:5]}"


def rag_rule_pass(gold: SpanGold, agent: str) -> bool | None:
    spans = gold_spans(gold)
    if not spans:
        return None
    blob = (extract_final_answer(agent) or agent).lower()
    for span in spans:
        if span.lower() in blob:
            return True
    return False
