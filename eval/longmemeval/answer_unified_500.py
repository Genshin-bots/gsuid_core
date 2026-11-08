import os
import sys
import asyncio
from typing import Any
from pathlib import Path
from datetime import datetime

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.common import (  # noqa: E402
    dump_json,
    load_json,
    load_eval_data,
    call_chat_with_history,
    extract_text_from_response,
)

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ.setdefault("GSUID_LOCAL_TEST_TOKEN", "bench-local-20261108")
os.environ.setdefault("GSUID_LOCAL_TEST_MODE", "1")

PREFIX = "v3fix_"
CONCURRENCY = 10
DATA_FILE = os.path.join(str(ROOT), "eval", "longmemeval", "longmemeval_s_cleaned.json")
ANSWERS_FILE = os.path.join(str(ROOT), "eval", "longmemeval", "results", "answers_unified_500.json")
JUDGE_FILE = os.path.join(str(ROOT), "eval", "longmemeval", "results", "judge_unified_500.json")
REPORT_FILE = os.path.join(str(ROOT), "eval", "longmemeval", "results", "report_unified_500.txt")
LOG_FILE = os.path.join(str(ROOT), "eval", "longmemeval", "results", "answer_unified_500.log")
BASE_URL = "http://127.0.0.1:8765"


def log(msg: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    line = f"{now} {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def fmt_clock(raw: str) -> str | None:
    body = " ".join(p for p in (raw or "").split() if not p.startswith("("))
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(body, fmt).isoformat()
        except ValueError:
            continue
    return None


async def main() -> None:
    items = load_eval_data(DATA_FILE)
    file_exists = await asyncio.to_thread(os.path.isfile, ANSWERS_FILE)
    answers: list[dict[str, Any]] = await asyncio.to_thread(load_json, ANSWERS_FILE) if file_exists else []
    done_ids = {str(r["question_id"]) for r in answers if r.get("status_code") == 200}
    todo = [q for q in items if str(q["question_id"]) not in done_ids]
    log(f"[start] total={len(items)} already_done={len(done_ids)} todo={len(todo)} concurrency={CONCURRENCY}")

    results = list(answers)
    write_lock = asyncio.Lock()
    sem = asyncio.Semaphore(CONCURRENCY)
    counter = {"answered": 0}

    limits = httpx.Limits(max_keepalive_connections=20, max_connections=30)
    timeout = httpx.Timeout(180.0)

    async with httpx.AsyncClient(trust_env=False, timeout=timeout, limits=limits) as client:

        async def process_question(q: dict[str, Any]) -> None:
            qid = str(q["question_id"])
            async with sem:
                clock = fmt_clock(q.get("question_date", "")) if q.get("question_date") else None
                try:
                    resp = await call_chat_with_history(
                        client=client,
                        base_url=BASE_URL,
                        user_id=f"{PREFIX}{qid}",
                        message=q["question"],
                        history=[],
                        timeout=180.0,
                        enable_observer=False,
                        enable_system2=False,
                        enable_tools=True,
                        memory_eval=False,
                        persona_name="评测助手",
                        clock_at=clock,
                    )
                    status = resp.get("status_code", -1)
                    text = (
                        extract_text_from_response(resp.get("data"))
                        if status == 200
                        else f"[ERROR] status_code={status}"
                    )
                    record = {
                        "question_id": qid,
                        "question_type": q.get("question_type", "unknown"),
                        "question": q["question"],
                        "standard_answer": q.get("answer", ""),
                        "agent_answer": text,
                        "memory": resp.get("memory"),
                        "status_code": status,
                    }
                except Exception as e:
                    record = {
                        "question_id": qid,
                        "question_type": q.get("question_type", "unknown"),
                        "question": q["question"],
                        "standard_answer": q.get("answer", ""),
                        "agent_answer": f"[ERROR] {e}",
                        "memory": None,
                        "status_code": -1,
                    }

                async with write_lock:
                    results.append(record)
                    await asyncio.to_thread(dump_json, ANSWERS_FILE, results)
                    counter["answered"] += 1
                    curr = counter["answered"]
                    log(f"[answer] ({curr}/{len(todo)}) qid={qid} type={record['question_type']}")

        tasks = [asyncio.create_task(process_question(q)) for q in todo]
        await asyncio.gather(*tasks)

    log(f"[finished_all] total_records={len(results)}")


if __name__ == "__main__":
    asyncio.run(main())
