"""LOFT 评测：BEIR 文本检索 + RAG / GlobalQA。

LOFT（Google DeepMind, arXiv:2406.13121）把语料塞进超长上下文做检索 / RAG。
GsCore 协议与 CorpusQA 相同：按数据集灌入记忆，再 chat_with_history 作答。

- retrieval（BEIR 子集）：金标是 passage id，主指标 recall@1 / mrecall
- rag（GlobalQA）：金标是短答案，主指标子串 EM，拿不准再 LLM

数据：https://storage.googleapis.com/loft-bench/{retrieval|rag}/{dataset}.zip

用法::

  uv run python eval/loft/run_loft.py download --task retrieval --dataset scifact --length 128k
  uv run python eval/loft/run_loft.py all --task retrieval --dataset scifact --length 128k --limit 20
  uv run python eval/loft/run_loft.py all --task rag --dataset nq --length 128k --limit 20
  uv run python eval/run_eval.py loft all --task rag --dataset nq --length 128k --limit 20
"""

from __future__ import annotations

import os
import sys
import json
import time
import socket
import asyncio
import hashlib
import zipfile
import argparse
import urllib.request
from typing import Any
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _inherit_core_token() -> None:
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
    if os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip():
        return
    try:
        import psutil
    except ImportError:
        return
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.Error, OSError):
            continue
        if "gsuid_core.core" not in cmd:
            continue
        try:
            env = proc.environ()
        except (psutil.Error, OSError):
            continue
        tok = env.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
        if tok:
            os.environ["GSUID_LOCAL_TEST_TOKEN"] = tok
            return


_inherit_core_token()

import httpx  # noqa: E402

import eval.common.http_client as _http_client  # noqa: E402
from eval.common import (  # noqa: E402
    DEFAULT_TIMEOUT,
    DEFAULT_BASE_URL,
    dump_json,
    load_json,
    call_batch_observe,
    judge_single_answer,
    call_chat_with_history,
    call_clear_user_global,
    extract_text_from_response,
)
from eval.loft.score import rag_rule_pass, retrieval_pass, extract_final_answer  # noqa: E402
from eval.common.runner import run_items, summarize_by  # noqa: E402
from gsuid_core.ai_core.text_chunk import chunk_text as _shared_chunk_text  # noqa: E402

_http_client._LOCAL_TEST_TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "")

DIR = Path(__file__).resolve().parent
DATA_DIR = DIR / "data"
RESULTS_DIR = DIR / "results"
GCS_BASE = "https://storage.googleapis.com/loft-bench"

RETRIEVAL_DATASETS = (
    "scifact",
    "arguana",
    "fever",
    "fiqa",
    "nq",
    "quora",
    "msmarco",
    "hotpotqa",
    "musique",
    "quest",
    "qampari",
    "topiocqa",
    "webis_touche2020",
)
RAG_DATASETS = ("nq", "hotpotqa", "musique", "quest", "qampari", "topiocqa")
LENGTHS = ("32k", "128k", "1m")
TASKS = ("retrieval", "rag")

_INGEST_CHUNK_CHARS = 800
_INGEST_BATCH = 40

_PROBE_RETRIEVAL = (
    "The corpus is already stored in memory. Each passage is tagged "
    "【文档】<pid>. Return the pid of the passage that best supports the query. "
    'Last line: The answer is: ["<pid>"]\n\nQuery: {question}'
)
_PROBE_RAG = (
    "Answer using only the documents already stored in memory. "
    "Give a short span copied from the documents when possible. "
    "Last line: The answer is: <value>\n\nQuery: {question}"
)


def _gcs_zip(task: str, dataset: str) -> str:
    return f"{GCS_BASE}/{task}/{dataset}.zip"


def _ds_root(task: str, dataset: str, length: str) -> Path:
    return DATA_DIR / task / dataset / length


def _user_id(task: str, dataset: str, length: str) -> str:
    short = "r" if task == "retrieval" else "g"
    return f"eval_loft_{short}_{dataset}_{length}"


def _tag_suffix(tag: str) -> str:
    return f"_{tag}" if tag else ""


def _out_dir(task: str, dataset: str, length: str) -> Path:
    return RESULTS_DIR / task / dataset / length


def _answers_path(task: str, dataset: str, length: str, tag: str) -> Path:
    return _out_dir(task, dataset, length) / f"answers{_tag_suffix(tag)}.json"


def _judge_path(task: str, dataset: str, length: str, tag: str) -> Path:
    return _out_dir(task, dataset, length) / f"judge{_tag_suffix(tag)}.json"


def _report_path(task: str, dataset: str, length: str, tag: str) -> Path:
    return _out_dir(task, dataset, length) / f"report{_tag_suffix(tag)}.md"


def _progress_path(task: str, dataset: str, length: str) -> Path:
    return _out_dir(task, dataset, length) / "progress.json"


def chunk_text(text: str, target: int = _INGEST_CHUNK_CHARS) -> list[str]:
    return _shared_chunk_text(text, target)


def docs_to_turns(docs: list[tuple[str, str]]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for title, body in docs:
        chunks = chunk_text(body)
        for i, ch in enumerate(chunks):
            prefix = f"【文档】{title}\n" if i == 0 else f"【文档续】{title}\n"
            turns.append({"role": "user", "content": prefix + ch})
    return turns


def download_dataset(task: str, dataset: str) -> Path:
    dest_root = DATA_DIR / task / dataset
    marker = dest_root / ".downloaded"
    if marker.is_file() and any(dest_root.glob("*/corpus.jsonl")):
        print(f"[download] 已存在 {dest_root}")
        return dest_root
    dest_root.mkdir(parents=True, exist_ok=True)
    url = _gcs_zip(task, dataset)
    print(f"[download] {url}")
    tmp = dest_root / f"{dataset}.zip"
    urllib.request.urlretrieve(url, str(tmp))
    with zipfile.ZipFile(tmp) as zf:
        zf.extractall(dest_root.parent)
    tmp.unlink(missing_ok=True)
    marker.write_text("ok\n", encoding="utf-8")
    print(f"[download] 解压到 {dest_root}")
    return dest_root


def load_corpus(task: str, dataset: str, length: str) -> list[tuple[str, str]]:
    path = _ds_root(task, dataset, length) / "corpus.jsonl"
    if not path.is_file():
        download_dataset(task, dataset)
    docs: list[tuple[str, str]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            pid = str(obj["pid"] if "pid" in obj else "")
            title = str(obj["title_text"] if "title_text" in obj else pid)
            body = str(obj["passage_text"] if "passage_text" in obj else "")
            if not pid or not body.strip():
                continue
            text = f"{title}\n{body}" if title and title != pid else body
            docs.append((pid, text))
    return docs


def load_queries(task: str, dataset: str, length: str, split: str) -> list[dict[str, Any]]:
    name = f"{split}_queries.jsonl"
    path = _ds_root(task, dataset, length) / name
    if not path.is_file():
        download_dataset(task, dataset)
    if not path.is_file():
        raise FileNotFoundError(f"没有 {path}")
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            qid = str(obj["qid"] if "qid" in obj else "")
            q = str(obj["query_text"] if "query_text" in obj else "")
            answers = obj["answers"] if "answers" in obj else []
            items.append(
                {
                    "question_id": qid,
                    "question": q,
                    "answers": answers,
                    "task": task,
                    "dataset": dataset,
                    "length": length,
                }
            )
    return items


def load_progress(task: str, dataset: str, length: str) -> dict[str, Any]:
    path = _progress_path(task, dataset, length)
    if not path.is_file():
        return {"ingest": False}
    data = load_json(str(path))
    return data if isinstance(data, dict) else {"ingest": False}


def save_progress(task: str, dataset: str, length: str, progress: dict[str, Any]) -> None:
    _out_dir(task, dataset, length).mkdir(parents=True, exist_ok=True)
    dump_json(str(_progress_path(task, dataset, length)), progress)


async def wait_core(base_url: str, timeout: float = 300.0) -> bool:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8765
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                break
        except OSError:
            await asyncio.sleep(2.0)
    else:
        return False
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    headers = {"X-Local-Test-Token": token} if token else {}
    url = f"{base_url}/api/ai/memory/batch_observe"
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        while time.time() < deadline:
            try:
                resp = await client.post(url, headers=headers, json={})
                if resp.status_code != 404:
                    print(f"[wait] eval api ready status={resp.status_code}", flush=True)
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
    return False


async def ingest_one(
    client: httpx.AsyncClient,
    base_url: str,
    task: str,
    dataset: str,
    length: str,
    *,
    timeout: float,
    force: bool,
) -> int:
    progress = load_progress(task, dataset, length)
    if progress.get("ingest") and not force:
        print(f"[ingest] {task}/{dataset}/{length} 已完成，跳过")
        return 0
    docs = load_corpus(task, dataset, length)
    if not docs:
        print(f"[ingest] {task}/{dataset}/{length} 无文档")
        return 1
    user_id = _user_id(task, dataset, length)
    turns = docs_to_turns(docs)
    print(f"[ingest] {task}/{dataset}/{length} user={user_id} docs={len(docs)} turns={len(turns)}")
    await call_clear_user_global(client, base_url, user_id, timeout=min(timeout, 120.0))
    observed = 0
    for i in range(0, len(turns), _INGEST_BATCH):
        batch = turns[i : i + _INGEST_BATCH]
        last = i + _INGEST_BATCH >= len(turns)
        resp = await call_batch_observe(
            client=client,
            base_url=base_url,
            user_id=user_id,
            turns=batch,
            flush=last,
            timeout=timeout,
        )
        if resp.get("status") != 0:
            print(f"[ingest] batch {i} 失败: {resp.get('msg')}")
            return 1
        data = resp["data"] if "data" in resp and isinstance(resp["data"], dict) else {}
        n = int(data["observed"]) if "observed" in data else len(batch)
        observed += n
        print(f"[ingest] {min(i + len(batch), len(turns))}/{len(turns)} observed+={n}")
    save_progress(task, dataset, length, {"ingest": True, "docs": len(docs), "turns": len(turns)})
    print(f"[ingest] done observed≈{observed}")
    return 0


async def cmd_download(args: argparse.Namespace) -> int:
    download_dataset(str(args.task), str(args.dataset))
    return 0


async def cmd_ingest(args: argparse.Namespace) -> int:
    if not await wait_core(args.base_url):
        print("[ingest] core 未就绪")
        return 2
    timeout = max(float(args.timeout), 600.0)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        return await ingest_one(
            client,
            args.base_url,
            str(args.task),
            str(args.dataset),
            str(args.length),
            timeout=timeout,
            force=bool(args.force_ingest),
        )


async def cmd_probe(args: argparse.Namespace) -> int:
    task = str(args.task)
    dataset = str(args.dataset)
    length = str(args.length)
    items = load_queries(task, dataset, length, str(args.split))
    items = items[args.start or 0 : args.end]
    if args.limit is not None:
        items = items[: args.limit]
    tag = args.tag or (f"n{len(items)}" if args.limit else "")
    out_file = str(_answers_path(task, dataset, length, tag))
    _out_dir(task, dataset, length).mkdir(parents=True, exist_ok=True)
    tmpl = _PROBE_RETRIEVAL if task == "retrieval" else _PROBE_RAG
    print(f"[probe] {task}/{dataset}/{length} n={len(items)} -> {out_file}")
    if not await wait_core(args.base_url):
        print("[probe] core 未就绪")
        return 2
    timeout = float(args.timeout)
    user_id = _user_id(task, dataset, length)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:

        async def one(q: dict[str, Any]) -> dict[str, Any]:
            qid = str(q["question_id"])
            question = str(q["question"])
            resp = await call_chat_with_history(
                client=client,
                base_url=args.base_url,
                user_id=user_id,
                message=tmpl.format(question=question),
                history=[],
                timeout=timeout,
                enable_observer=False,
                enable_tools=bool(args.enable_tools),
                memory_eval=False,
                persona_name=str(args.persona_name),
            )
            status = resp.get("status_code", -1)
            answer = extract_text_from_response(resp.get("data")) if status == 200 else f"[ERROR] status_code={status}"
            return {
                "question_id": qid,
                "task": task,
                "dataset": dataset,
                "length": length,
                "question": question,
                "standard_answer": q.get("answers"),
                "agent_answer": answer,
                "extracted_answer": extract_final_answer(answer) if status == 200 else "",
                "status_code": status,
            }

        await run_items(
            items,
            one,
            out_file,
            concurrency=max(1, int(args.concurrency)),
            resume=not bool(args.no_resume),
            repair=True,
            label="loft-probe",
        )
    return 0


async def cmd_judge(args: argparse.Namespace) -> int:
    task = str(args.task)
    dataset = str(args.dataset)
    length = str(args.length)
    tag = args.tag or (f"n{args.limit}" if args.limit else "")
    answers_file = args.answers_file or str(_answers_path(task, dataset, length, tag))
    judge_file = args.judge_file or str(_judge_path(task, dataset, length, tag))
    answers = load_json(answers_file)
    if not isinstance(answers, list):
        print(f"[judge] 无效答卷 {answers_file}")
        return 1
    ok_rows = [a for a in answers if isinstance(a, dict)]
    print(f"[judge] {len(ok_rows)} 条 <- {answers_file}")
    timeout = min(float(args.timeout), 180.0)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:

        async def one(a: dict[str, Any]) -> dict[str, Any]:
            qid = str(a.get("question_id", ""))
            question = str(a.get("question", ""))
            gold = a.get("standard_answer")
            agent = str(a.get("agent_answer") or "")
            status = a.get("status_code", 200)
            if status != 200 or agent.startswith("[ERROR]"):
                judge = {"passed": False, "correct": False, "reason": "infra", "method": "infra"}
            elif task == "retrieval":
                passed, rec1, reason = retrieval_pass(gold, agent)
                judge = {
                    "passed": passed,
                    "correct": passed,
                    "reason": reason,
                    "method": "recall",
                    "recall_at_1": rec1,
                }
            else:
                det = rag_rule_pass(gold, agent)
                if det is True:
                    judge = {"passed": True, "correct": True, "reason": "span EM", "method": "rule"}
                elif det is False and not args.llm_fallback_on_rule_fail:
                    judge = {"passed": False, "correct": False, "reason": "span miss", "method": "rule"}
                else:
                    parsed = await judge_single_answer(
                        client=client,
                        base_url=args.base_url,
                        question=question,
                        standard_answer=json.dumps(gold, ensure_ascii=False),
                        agent_answer=agent,
                        timeout=timeout,
                        user_id=f"judge_loft_{hashlib.md5(qid.encode()).hexdigest()[:10]}",
                    )
                    correct = bool(parsed.get("correct"))
                    judge = {
                        "passed": correct,
                        "correct": correct,
                        "reason": str(parsed.get("reason", "")),
                        "method": "llm" if det is not True else "rule",
                    }
            return {
                "question_id": qid,
                "task": task,
                "dataset": dataset,
                "question": question,
                "standard_answer": gold,
                "agent_answer": agent,
                "extracted_answer": a.get("extracted_answer") or extract_final_answer(agent),
                "judge": judge,
            }

        await run_items(
            ok_rows,
            one,
            judge_file,
            concurrency=max(1, int(args.concurrency)),
            resume=not bool(args.no_resume),
            repair=True,
            label="loft-judge",
        )
    write_report(task, dataset, length, tag, judge_file)
    return 0


def write_report(task: str, dataset: str, length: str, tag: str, judge_file: str | None = None) -> None:
    path = judge_file or str(_judge_path(task, dataset, length, tag))
    if not os.path.isfile(path):
        print(f"[report] 没有 {path}")
        return
    records = load_json(path)
    if not isinstance(records, list):
        return
    stats = summarize_by(records, type_field="dataset")
    all_s = stats["__all__"] if "__all__" in stats else {"passed": 0, "total": 0}
    total = int(all_s["total"])
    passed = int(all_s["passed"])
    pct = 100.0 * passed / total if total else 0.0
    recs = [
        float(r["judge"]["recall_at_1"])
        for r in records
        if isinstance(r, dict)
        and isinstance(r.get("judge"), dict)
        and "recall_at_1" in r["judge"]
        and isinstance(r["judge"]["recall_at_1"], (int, float))
    ]
    lines = [
        f"# LOFT {task}/{dataset}/{length}{(' ' + tag) if tag else ''}",
        "",
        "口径：生产 Chat（评测助手 + enable_tools + memory_eval=False）。",
        "语料灌入记忆，题目不把整库塞进当前轮。",
        "",
        f"**总分：{passed}/{total} ({pct:.1f}%)**",
    ]
    if recs:
        lines.append(f"**recall@1 均值：{sum(recs) / len(recs):.3f}**")
    lines.append("")
    out = _report_path(task, dataset, length, tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[report] {out}")


async def cmd_report(args: argparse.Namespace) -> int:
    tag = args.tag or (f"n{args.limit}" if args.limit else "")
    write_report(str(args.task), str(args.dataset), str(args.length), tag)
    return 0


async def cmd_all(args: argparse.Namespace) -> int:
    rc = await cmd_ingest(args)
    if rc:
        return rc
    n_items = len(load_queries(str(args.task), str(args.dataset), str(args.length), str(args.split)))
    if args.limit:
        n_items = min(n_items, int(args.limit))
    if args.limit and not args.tag:
        args.tag = f"n{n_items}"
    rc = await cmd_probe(args)
    if rc:
        return rc
    return await cmd_judge(args)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=DEFAULT_BASE_URL)
    common.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    common.add_argument("--concurrency", type=int, default=4)
    common.add_argument("--task", default="retrieval", choices=TASKS)
    common.add_argument("--dataset", default="scifact")
    common.add_argument("--length", default="128k", choices=LENGTHS)
    common.add_argument("--split", default="test", choices=("test", "dev"))
    common.add_argument("--start", type=int, default=None)
    common.add_argument("--end", type=int, default=None)
    common.add_argument("--limit", type=int, default=None)
    common.add_argument("--tag", default=None)
    common.add_argument("--force-ingest", action="store_true")
    common.add_argument("--no-resume", action="store_true")
    common.add_argument("--persona-name", default="评测助手")
    common.add_argument("--enable-tools", dest="enable_tools", action="store_true", default=True)
    common.add_argument("--no-tools", dest="enable_tools", action="store_false")
    common.add_argument("--answers-file", default=None)
    common.add_argument("--judge-file", default=None)
    common.add_argument("--llm-fallback-on-rule-fail", action="store_true")
    p = argparse.ArgumentParser(description="LOFT（BEIR 检索 + RAG / GlobalQA）GsCore 评测")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("download", "ingest", "probe", "judge", "report", "all"):
        sub.add_parser(name, parents=[common])
    return p


async def main_async(args: argparse.Namespace) -> int:
    args.concurrency = max(1, min(8, int(args.concurrency)))
    allowed = RETRIEVAL_DATASETS if args.task == "retrieval" else RAG_DATASETS
    if args.dataset not in allowed:
        print(f"[loft] {args.task} 不支持 dataset={args.dataset}，可选 {allowed}")
        return 2
    cmd = str(args.cmd)
    if cmd == "download":
        return await cmd_download(args)
    if cmd == "ingest":
        return await cmd_ingest(args)
    if cmd == "probe":
        return await cmd_probe(args)
    if cmd == "judge":
        return await cmd_judge(args)
    if cmd == "report":
        return await cmd_report(args)
    if cmd == "all":
        return await cmd_all(args)
    print(f"unknown cmd {cmd}")
    return 1


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
