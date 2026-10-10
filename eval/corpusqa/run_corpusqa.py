"""CorpusQA 评测：灌入跨文档语料，再让 GsCore 做语料级统计 / 比较。

CorpusQA（Tongyi-Zhiwen, arXiv:2601.14952）测的是证据高度分散的语料级分析：
过滤、聚合、跨文档计算。官方 128k/1m jsonl 在 Hugging Face，每条含完整语料 +
程序化金标。GsCore 不把 128k token 塞进单轮上下文，而是：

  按 domain 清库 → batch_observe 灌文档 → chat_with_history 作答 → 规则+LLM 判分

协议对齐生产 Chat：评测助手 + enable_tools + memory_eval=False + 不抽实体。
同一 domain 共享一份语料（doc_files 集合唯一），只灌一次。

用法::

  uv run python eval/corpusqa/run_corpusqa.py download --scale 128k
  uv run python eval/corpusqa/run_corpusqa.py all --scale 128k --limit 40
  uv run python eval/run_eval.py corpusqa all --scale 128k --limit 40
"""

from __future__ import annotations

import os
import re
import sys
import json
import math
import time
import random
import socket
import asyncio
import hashlib
import argparse
import urllib.request
from typing import Any, Dict
from pathlib import Path
from collections import defaultdict
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
from eval.common.runner import run_items, summarize_by  # noqa: E402
from gsuid_core.ai_core.text_chunk import chunk_text as _shared_chunk_text  # noqa: E402

_http_client._LOCAL_TEST_TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "")

DIR = Path(__file__).resolve().parent
DATA_DIR = DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
RESULTS_DIR = DIR / "results"

HF_REPO = "Tongyi-Zhiwen/CorpusQA"
SCALE_FILES = {
    "128k": "128k_4domains.jsonl",
    "1m": "1m_4domains.jsonl",
}
SCALE_ORDER = ("128k", "1m")
DOMAINS = ("education_en", "financial_en", "financial_zh", "real_estate_en")

# 与 batch_observe 服务端 _INGEST_CHUNK_CHARS 对齐，客户端预切以免单请求过大
_INGEST_CHUNK_CHARS = 800
_INGEST_BATCH = 40
_DOC_SPLIT_RE = re.compile(r"(?m)^# Document\s+(\d+)\s*:\s*")
_QUESTION_SPLIT_RE = re.compile(r"(?m)^# (?:Question|问题)\s*:\s*")
_ANSWER_IS_RE = re.compile(
    r"(?:The answer is|答案是)\s*[:：]\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)
_NUM_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_EMPTY_NEG_RE = re.compile(
    r"(\[\]|empty list|no (?:universit|compan|entit|school|match)|none of|"
    r"not found|no such|没有(?:符合|满足|一家|一所|公司)|无符合|空列表)",
    re.IGNORECASE,
)

_PROBE_EN = (
    "Answer using only the documents already stored in memory. "
    "Do the aggregation / comparison yourself. "
    "Put the final answer on its own last line as: The answer is: <value>\n"
    "Value must be a string list (including []), a short string, a number "
    "(2 decimal places, no unit), or a percentage (2 decimal places).\n\n"
    "Question: {question}"
)
_PROBE_ZH = (
    "请只根据记忆中已入库的文档回答。自己做过滤、汇总或比较。"
    "最后单独一行写：The answer is: <value>\n"
    "value 只能是字符串列表（含 []）、短字符串、数字（两位小数、无单位）或百分比（两位小数）。\n\n"
    "问题：{question}"
)


def _hf_url(filename: str) -> str:
    base = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    return f"{base}/datasets/{HF_REPO}/resolve/main/{filename}"


def _jsonl_path(scale: str) -> Path:
    return DATA_DIR / SCALE_FILES[scale]


def _cache_dir(scale: str) -> Path:
    return CACHE_DIR / scale


def _questions_path(scale: str) -> Path:
    return _cache_dir(scale) / "questions.json"


def _docs_path(scale: str, domain: str) -> Path:
    return _cache_dir(scale) / f"{domain}_docs.json"


def _results_dir(scale: str) -> Path:
    return RESULTS_DIR / scale


def _tag_suffix(tag: str) -> str:
    return f"_{tag}" if tag else ""


def _answers_path(scale: str, tag: str) -> Path:
    return _results_dir(scale) / f"answers{_tag_suffix(tag)}.json"


def _judge_path(scale: str, tag: str) -> Path:
    return _results_dir(scale) / f"judge{_tag_suffix(tag)}.json"


def _report_path(scale: str, tag: str) -> Path:
    return _results_dir(scale) / f"report{_tag_suffix(tag)}.md"


def _progress_path(scale: str) -> Path:
    return _results_dir(scale) / "progress.json"


def domain_user_id(scale: str, domain: str, suffix: str = "") -> str:
    # BatchObserveRequest.user_id max_length=64；eval_ 前缀方便评测清库
    # suffix 用于 A/B：同一语料灌进另一个 scope，只切换单个变量（如 extract 开关）。
    return f"eval_cqa_{scale}_{domain}{suffix}"


def iter_jsonl_objects(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                yield obj


def _user_prompt(obj: dict[str, Any]) -> str:
    prompt = obj["prompt"] if "prompt" in obj else None
    if not isinstance(prompt, list):
        return ""
    for turn in prompt:
        if isinstance(turn, dict) and turn.get("role") == "user":
            content = turn["content"] if "content" in turn else ""
            return content if isinstance(content, str) else str(content)
    return ""


def split_corpus(user_prompt: str, doc_files: list[str]) -> list[tuple[str, str]]:
    """从官方 user prompt 抽出文档正文，去掉末尾 Question / 输出格式说明。"""
    corpus = user_prompt
    qm = _QUESTION_SPLIT_RE.search(corpus)
    if qm:
        corpus = corpus[: qm.start()]
    matches = list(_DOC_SPLIT_RE.finditer(corpus))
    if not matches:
        body = corpus.strip()
        title = doc_files[0] if doc_files else "corpus"
        return [(title, body)] if body else []
    docs: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(corpus)
        body = corpus[start:end].strip()
        if not body:
            continue
        idx = int(m.group(1))
        title = doc_files[idx - 1] if 0 <= idx - 1 < len(doc_files) else f"Document {idx}"
        docs.append((title, body))
    return docs


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


def gold_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _system2_arg(args: argparse.Namespace) -> bool | None:
    """System-2 取值：显式开/关，否则 None 交回服务端按配置决定。

    事实（edge）只从 System-2 进注入上下文，不传则吃 ``enable_system2get``
    的全局配置（默认关）——那样 extract 抽出的事实不会被读到。
    """
    if getattr(args, "enable_system2", False):
        return True
    if getattr(args, "no_system2", False):
        return False
    return None


def extract_final_answer(text: str) -> str:
    if not text:
        return ""
    m = _ANSWER_IS_RE.search(text)
    if m:
        return m.group(1).strip().splitlines()[0].strip()
    return text.strip()


def _parse_number(token: str) -> float | None:
    t = token.replace(",", "").strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _numbers_in(text: str) -> list[float]:
    found: list[float] = []
    for m in _NUM_RE.finditer(text.replace("，", ",")):
        n = _parse_number(m.group(0))
        if n is not None:
            found.append(n)
    return found


def _num_close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-3, abs_tol=0.05)


def deterministic_match(gold: object, agent: str) -> bool | None:
    """能确定则 True/False；拿不准返回 None 交给 LLM。"""
    used_extract = bool(_ANSWER_IS_RE.search(agent))
    blob = extract_final_answer(agent) if used_extract else agent
    if isinstance(gold, list):
        if len(gold) == 0:
            if _EMPTY_NEG_RE.search(blob) or blob.strip() in {"[]", "无", "没有"}:
                return True
            return None
        names = [str(x).strip() for x in gold if str(x).strip()]
        if not names:
            return None
        low = blob.lower()
        hit = sum(1 for name in names if name.lower() in low)
        if hit == len(names):
            return True
        if used_extract and hit == 0:
            return False
        return None if hit == 0 else False
    if isinstance(gold, str) and gold.strip().endswith("%"):
        g = _parse_number(gold.strip()[:-1])
        if g is None:
            return None
        nums = _numbers_in(blob)
        for n in nums:
            if _num_close(n, g) or _num_close(n, g / 100.0) or _num_close(n * 100.0, g):
                return True
        if used_extract:
            return False if nums else None
        return False if nums else None
    if isinstance(gold, bool):
        return None
    if isinstance(gold, (int, float)):
        g = float(gold)
        nums = _numbers_in(blob)
        if any(_num_close(n, g) for n in nums):
            return True
        if used_extract:
            return False if nums else None
        return None
    if isinstance(gold, str):
        g = gold.strip()
        if not g:
            return None
        if g.lower() in blob.lower():
            return True
        return False if used_extract else None
    return None


def download_scale(scale: str) -> Path:
    filename = SCALE_FILES[scale]
    dest = _jsonl_path(scale)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 1024:
        print(f"[download] 已存在 {dest} ({dest.stat().st_size} bytes)")
        return dest
    url = _hf_url(filename)
    print(f"[download] {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")

    def _report(block: int, block_size: int, total: int) -> None:
        got = block * block_size
        if total > 0:
            pct = min(100.0, 100.0 * got / total)
            print(f"\r[download] {pct:5.1f}%  {got}/{total}", end="", flush=True)

    urllib.request.urlretrieve(url, str(tmp), reporthook=_report)
    print()
    os.replace(tmp, dest)
    print(f"[download] 完成 {dest} ({dest.stat().st_size} bytes)")
    return dest


def build_index(scale: str, *, force: bool = False) -> list[dict[str, Any]]:
    qpath = _questions_path(scale)
    cache = _cache_dir(scale)
    cache.mkdir(parents=True, exist_ok=True)
    jsonl = _jsonl_path(scale)
    if not jsonl.is_file():
        download_scale(scale)
    if qpath.is_file() and not force:
        all_docs = all(_docs_path(scale, d).is_file() for d in DOMAINS)
        if all_docs:
            data = load_json(str(qpath))
            if isinstance(data, list) and data:
                print(f"[index] 复用 {qpath} ({len(data)} 题)")
                return data

    questions: list[dict[str, Any]] = []
    seen_domain: set[str] = set()
    n = 0
    print(f"[index] 扫描 {jsonl.name} …")
    for obj in iter_jsonl_objects(jsonl):
        n += 1
        qid = str(obj["id"] if "id" in obj else f"row_{n}")
        domain = str(obj["domain"] if "domain" in obj else "unknown")
        raw_docs = obj["doc_files"] if "doc_files" in obj else []
        doc_files = [str(x) for x in raw_docs] if isinstance(raw_docs, list) else []
        questions.append(
            {
                "question_id": qid,
                "domain": domain,
                "scale": str(obj["set"] if "set" in obj else scale),
                "question": str(obj["question"] if "question" in obj else ""),
                "answer": obj["answer"] if "answer" in obj else None,
                "n_docs": len(doc_files),
            }
        )
        if domain not in seen_domain:
            user = _user_prompt(obj)
            docs = split_corpus(user, doc_files)
            payload = {
                "domain": domain,
                "doc_files": doc_files,
                "n_docs": len(docs),
                "chars": sum(len(b) for _, b in docs),
                "documents": [{"title": t, "chars": len(b), "text": b} for t, b in docs],
            }
            dump_json(str(_docs_path(scale, domain)), payload)
            seen_domain.add(domain)
            print(f"[index] {domain}: {len(docs)} 篇 / {payload['chars']} 字")
    dump_json(str(qpath), questions)
    print(f"[index] {n} 题 -> {qpath}")
    return questions


def load_domain_docs(scale: str, domain: str) -> list[tuple[str, str]]:
    path = _docs_path(scale, domain)
    if not path.is_file():
        build_index(scale)
    data = load_json(str(path))
    docs_raw = data["documents"] if isinstance(data, dict) and "documents" in data else []
    out: list[tuple[str, str]] = []
    if not isinstance(docs_raw, list):
        return out
    for item in docs_raw:
        if not isinstance(item, dict):
            continue
        title = str(item["title"] if "title" in item else "doc")
        text = str(item["text"] if "text" in item else "")
        if text.strip():
            out.append((title, text))
    return out


def load_progress(scale: str) -> dict[str, Any]:
    path = _progress_path(scale)
    if not path.is_file():
        return {"ingest": []}
    data = load_json(str(path))
    return data if isinstance(data, dict) else {"ingest": []}


def save_progress(scale: str, progress: dict[str, Any]) -> None:
    _results_dir(scale).mkdir(parents=True, exist_ok=True)
    dump_json(str(_progress_path(scale)), progress)


def select_items(
    items: list[dict[str, Any]],
    *,
    domain: str | None,
    start: int,
    end: int | None,
    limit: int | None,
    seed: int,
) -> list[dict[str, Any]]:
    filtered = [x for x in items if domain is None or x.get("domain") == domain]
    filtered = filtered[start:end]
    if limit is None or limit >= len(filtered):
        return filtered
    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rng = random.Random(seed)
    for it in filtered:
        by_domain[str(it.get("domain", "unknown"))].append(it)
    for bucket in by_domain.values():
        rng.shuffle(bucket)
    out: list[dict[str, Any]] = []
    i = 0
    while len(out) < limit:
        added = False
        for d in sorted(by_domain):
            bucket = by_domain[d]
            if i < len(bucket):
                out.append(bucket[i])
                added = True
                if len(out) >= limit:
                    break
        if not added:
            break
        i += 1
    return out


def probe_message(question: str, domain: str) -> str:
    tmpl = _PROBE_ZH if domain.endswith("_zh") else _PROBE_EN
    return tmpl.format(question=question)


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


async def cmd_ping(base_url: str) -> int:
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "").strip()
    headers = {"X-Local-Test-Token": token} if token else {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        resp = await client.post(
            f"{base_url}/api/ai/memory/batch_observe",
            headers=headers,
            json={},
        )
        print(f"[ping] batch_observe={resp.status_code} body={resp.text[:240]}")
        if resp.status_code == 404:
            print("[ping] local-test gate 未开（需要 GSUID_LOCAL_TEST_MODE=1）")
            return 2
        return 0


async def ingest_domain(
    client: httpx.AsyncClient,
    base_url: str,
    scale: str,
    domain: str,
    *,
    timeout: float,
    force: bool,
    extract: bool = False,
    write_episodes: bool = True,
    extract_window_chars: int = 12000,
    extract_window_turns: int = 20,
    extract_window_timeout: float = 300.0,
    extract_concurrency: int = 4,
    trigger_rebuild: bool = False,
    user_suffix: str = "",
) -> int:
    user_id = domain_user_id(scale, domain, user_suffix)
    progress = load_progress(scale)
    ingested = progress["ingest"] if "ingest" in progress and isinstance(progress["ingest"], list) else []
    # A/B 的对照 scope 用独立 key，否则会被主 scope 的「已完成」静默跳过。
    slot = f"{domain}{user_suffix}"
    if slot in ingested and not force:
        print(f"[ingest] {slot} 已完成，跳过（要重抽事实加 --force-ingest --extract）")
        return 0
    docs = load_domain_docs(scale, domain)
    if not docs:
        print(f"[ingest] {domain} 无文档")
        return 1
    turns = docs_to_turns(docs)
    print(f"[ingest] {domain} user={user_id} docs={len(docs)} turns={len(turns)}")
    cleared = await call_clear_user_global(client, base_url, user_id, timeout=min(timeout, 120.0))
    if cleared.get("status") not in (0, None) and "status" in cleared:
        print(f"[ingest] clear 警告: {cleared.get('msg')}")
    observed = 0
    # extract=True 启用窗口化实体与边抽取，供图谱检索与跨文档聚合使用。
    extra: Dict[str, Any] | None = None
    if extract:
        extra = {
            "extract": True,
            "write_episodes": write_episodes,
            "extract_window_chars": extract_window_chars,
            "extract_window_turns": extract_window_turns,
            "extract_window_timeout": extract_window_timeout,
            "extract_concurrency": extract_concurrency,
        }
    if trigger_rebuild:
        # System-2 读 AIMemHierarchicalGraphMeta，max_layer=0 时直接返回空结果：
        # 抽了实体/边却不建图，事实就没有任何消费方。
        assert extra is not None, "trigger_rebuild 需与 --extract 同用"
        extra["trigger_rebuild"] = True
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
            extra_payload=extra,
        )
        if resp.get("status") != 0:
            print(f"[ingest] {domain} batch {i} 失败: {resp.get('msg')}")
            return 1
        data = resp["data"] if "data" in resp and isinstance(resp["data"], dict) else {}
        n = int(data["observed"]) if "observed" in data else len(batch)
        observed += n
        ex = data.get("extract") if isinstance(data.get("extract"), dict) else {}
        if ex:
            suffix = (
                f" extract(w={ex.get('windows_done', 0)}/{ex.get('windows_total', 0)}"
                f" e+{ex.get('entities_added', 0)} g+{ex.get('edges_added', 0)})"
            )
        else:
            suffix = ""
        print(f"[ingest] {domain} {min(i + len(batch), len(turns))}/{len(turns)} observed+={n}{suffix}")
    ingested = [x for x in ingested if x != slot]
    ingested.append(slot)
    progress["ingest"] = ingested
    save_progress(scale, progress)
    print(f"[ingest] {domain} done observed≈{observed}")
    return 0


async def cmd_ingest(args: argparse.Namespace) -> int:
    scale = str(args.scale)
    build_index(scale)
    if not await wait_core(args.base_url):
        print("[ingest] core 未就绪")
        return 2
    domains = [args.domain] if args.domain else list(DOMAINS)
    timeout = max(float(args.timeout), 600.0)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        for domain in domains:
            rc = await ingest_domain(
                client,
                args.base_url,
                scale,
                domain,
                timeout=timeout,
                force=bool(args.force_ingest),
                extract=bool(getattr(args, "extract", False)),
                write_episodes=not bool(getattr(args, "no_write_episodes", False)),
                extract_window_chars=int(getattr(args, "extract_window_chars", 12000)),
                extract_window_turns=int(getattr(args, "extract_window_turns", 20)),
                extract_window_timeout=float(getattr(args, "extract_window_timeout", 300.0)),
                extract_concurrency=int(getattr(args, "extract_concurrency", 4)),
                trigger_rebuild=bool(getattr(args, "trigger_rebuild", False)),
                user_suffix=str(getattr(args, "user_suffix", "") or ""),
            )
            if rc:
                return rc
    return 0


async def cmd_probe(args: argparse.Namespace) -> int:
    scale = str(args.scale)
    items = build_index(scale)
    chosen = select_items(
        items,
        domain=args.domain,
        start=args.start or 0,
        end=args.end,
        limit=args.limit,
        seed=int(args.seed),
    )
    tag = args.tag or (f"n{len(chosen)}" if args.limit else "")
    out_file = str(_answers_path(scale, tag))
    _results_dir(scale).mkdir(parents=True, exist_ok=True)
    print(
        f"[probe] scale={scale} n={len(chosen)} persona={args.persona_name} "
        f"tools={args.enable_tools} system2={_system2_arg(args)} user={args.user_suffix or '-'} -> {out_file}"
    )
    if not await wait_core(args.base_url):
        print("[probe] core 未就绪")
        return 2
    timeout = float(args.timeout)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:

        async def one(q: dict[str, Any]) -> dict[str, Any]:
            qid = str(q["question_id"])
            domain = str(q["domain"])
            question = str(q["question"])
            user_id = domain_user_id(scale, domain, str(getattr(args, "user_suffix", "") or ""))
            resp = await call_chat_with_history(
                client=client,
                base_url=args.base_url,
                user_id=user_id,
                message=probe_message(question, domain),
                history=[],
                timeout=timeout,
                enable_observer=False,
                enable_system2=_system2_arg(args),
                enable_tools=bool(args.enable_tools),
                memory_eval=False,
                persona_name=str(args.persona_name),
            )
            status = resp.get("status_code", -1)
            answer = extract_text_from_response(resp.get("data")) if status == 200 else f"[ERROR] status_code={status}"
            return {
                "question_id": qid,
                "domain": domain,
                "scale": scale,
                "question": question,
                "standard_answer": q.get("answer"),
                "agent_answer": answer,
                "extracted_answer": extract_final_answer(answer) if status == 200 else "",
                "memory": resp.get("memory"),
                "status_code": status,
            }

        await run_items(
            chosen,
            one,
            out_file,
            concurrency=max(1, int(args.concurrency)),
            resume=not bool(args.no_resume),
            repair=True,
            label="cqa-probe",
        )
    return 0


async def cmd_judge(args: argparse.Namespace) -> int:
    scale = str(args.scale)
    answers_file = args.answers_file or str(_answers_path(scale, args.tag or ""))
    if args.limit and not args.answers_file and not args.tag:
        answers_file = str(_answers_path(scale, f"n{args.limit}"))
    judge_file = args.judge_file or str(_judge_path(scale, args.tag or ""))
    if args.limit and not args.judge_file and not args.tag:
        judge_file = str(_judge_path(scale, f"n{args.limit}"))
    answers = load_json(answers_file)
    if not isinstance(answers, list):
        print(f"[judge] 无效答卷 {answers_file}")
        return 1
    ok = [a for a in answers if isinstance(a, dict)]
    print(f"[judge] {len(ok)} 条 <- {answers_file}")
    timeout = min(float(args.timeout), 180.0)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:

        async def one(a: dict[str, Any]) -> dict[str, Any]:
            qid = str(a.get("question_id", ""))
            question = str(a.get("question", ""))
            gold = a.get("standard_answer")
            agent = str(a.get("agent_answer") or "")
            status = a.get("status_code", 200)
            if status != 200 or agent.startswith("[ERROR]"):
                return {
                    "question_id": qid,
                    "domain": a.get("domain"),
                    "question": question,
                    "standard_answer": gold,
                    "agent_answer": agent,
                    "judge": {"passed": False, "correct": False, "reason": "infra", "method": "infra"},
                }
            det = None if args.llm_only else deterministic_match(gold, agent)
            if det is True:
                judge = {"passed": True, "correct": True, "reason": "deterministic", "method": "rule"}
            elif det is False and not args.llm_fallback_on_rule_fail:
                judge = {"passed": False, "correct": False, "reason": "deterministic miss", "method": "rule"}
            else:
                parsed = await judge_single_answer(
                    client=client,
                    base_url=args.base_url,
                    question=question,
                    standard_answer=gold_text(gold),
                    agent_answer=agent,
                    timeout=timeout,
                    user_id=f"judge_cqa_{hashlib.md5(qid.encode()).hexdigest()[:10]}",
                )
                correct = bool(parsed.get("correct"))
                judge = {
                    "passed": correct,
                    "correct": correct,
                    "reason": str(parsed.get("reason", "")),
                    "method": "llm",
                }
            return {
                "question_id": qid,
                "domain": a.get("domain"),
                "question": question,
                "standard_answer": gold,
                "agent_answer": agent,
                "extracted_answer": a.get("extracted_answer") or extract_final_answer(agent),
                "judge": judge,
            }

        await run_items(
            ok,
            one,
            judge_file,
            concurrency=max(1, int(args.concurrency)),
            resume=not bool(args.no_resume),
            repair=True,
            label="cqa-judge",
        )
    write_report(scale, args.tag or (f"n{args.limit}" if args.limit else ""), judge_file)
    return 0


def write_report(scale: str, tag: str, judge_file: str | None = None) -> None:
    path = judge_file or str(_judge_path(scale, tag))
    if not os.path.isfile(path):
        print(f"[report] 没有 {path}")
        return
    records = load_json(path)
    if not isinstance(records, list):
        return
    stats = summarize_by(records, type_field="domain")
    lines = [
        f"# CorpusQA {scale}{(' ' + tag) if tag else ''}",
        "",
        "口径：生产 Chat（评测助手 + enable_tools + memory_eval=False）。",
        "语料按 domain 灌入记忆，题目不把 128k 正文塞进当前轮。",
        "",
    ]
    all_s = stats["__all__"] if "__all__" in stats else {"passed": 0, "total": 0}
    total = int(all_s["total"])
    passed = int(all_s["passed"])
    pct = 100.0 * passed / total if total else 0.0
    lines.append(f"**总分：{passed}/{total} ({pct:.1f}%)**")
    lines.append("")
    lines.append("| 域 | 通过 | 总数 | 准确率 |")
    lines.append("|---|---:|---:|---:|")
    for domain in DOMAINS:
        if domain not in stats:
            continue
        s = stats[domain]
        p, t = int(s["passed"]), int(s["total"])
        lines.append(f"| {domain} | {p} | {t} | {100.0 * p / t if t else 0.0:.1f}% |")
    lines.append("")
    out = _report_path(scale, tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[report] {out}")


async def cmd_all(args: argparse.Namespace) -> int:
    scale = str(args.scale)
    build_index(scale)
    n_items = len(
        select_items(
            load_json(str(_questions_path(scale))),
            domain=args.domain,
            start=args.start or 0,
            end=args.end,
            limit=args.limit,
            seed=int(args.seed),
        )
    )
    if args.limit and not args.tag:
        args.tag = f"n{n_items}"
    rc = await cmd_ingest(args)
    if rc:
        return rc
    rc = await cmd_probe(args)
    if rc:
        return rc
    return await cmd_judge(args)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=DEFAULT_BASE_URL)
    common.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    common.add_argument("--concurrency", type=int, default=4)
    common.add_argument("--scale", default="128k", choices=SCALE_ORDER)
    common.add_argument("--domain", default=None, choices=DOMAINS)
    common.add_argument("--start", type=int, default=None)
    common.add_argument("--end", type=int, default=None)
    common.add_argument("--limit", type=int, default=None, help="分层抽样题量（按 4 域轮询）")
    common.add_argument("--seed", type=int, default=0)
    common.add_argument("--tag", default=None, help="答卷/判分文件后缀")
    common.add_argument("--force-ingest", action="store_true")
    common.add_argument(
        "--extract",
        action="store_true",
        help="灌库时开窗口化实体/边抽取（逐份抽事实 → 实体/边 → System-2 聚合）",
    )
    common.add_argument("--no-write-episodes", action="store_true", help="只抽事实不写 granular Episode")
    common.add_argument(
        "--trigger-rebuild",
        action="store_true",
        help="灌库后同步建分层图（System-2 读它选节点；不建图则抽出的事实无人消费）",
    )
    common.add_argument("--extract-window-chars", type=int, default=12000)
    common.add_argument("--extract-window-turns", type=int, default=20)
    common.add_argument("--extract-window-timeout", type=float, default=300.0)
    common.add_argument("--extract-concurrency", type=int, default=4)
    common.add_argument("--no-resume", action="store_true")
    common.add_argument("--persona-name", default="评测助手")
    common.add_argument("--enable-tools", dest="enable_tools", action="store_true", default=True)
    common.add_argument("--no-tools", dest="enable_tools", action="store_false")
    common.add_argument(
        "--enable-system2",
        dest="enable_system2",
        action="store_true",
        default=False,
        help="查询时放行 System-2（走分层图选节点/边；不传则吃服务端配置）",
    )
    common.add_argument(
        "--no-system2",
        dest="no_system2",
        action="store_true",
        default=False,
        help="查询时显式关 System-2（对照臂，避免受服务端配置漂移影响）",
    )
    common.add_argument(
        "--user-suffix",
        default="",
        help="给评测 user_id 加后缀（灌库与探针必须同后缀）：A/B 对照灌到独立 scope",
    )
    common.add_argument("--answers-file", default=None)
    common.add_argument("--judge-file", default=None)
    common.add_argument("--llm-only", action="store_true", help="跳过数字/列表规则匹配，全部走 LLM")
    common.add_argument(
        "--llm-fallback-on-rule-fail",
        action="store_true",
        help="规则判错后再让 LLM 看一遍（默认规则失败即 FAIL）",
    )
    p = argparse.ArgumentParser(description="CorpusQA（语料级分析）GsCore 评测")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("download", "index", "ping", "ingest", "probe", "judge", "report", "all", "smoke"):
        sub.add_parser(name, parents=[common])
    return p


async def main_async(args: argparse.Namespace) -> int:
    args.concurrency = max(1, min(8, int(args.concurrency)))
    cmd = str(args.cmd)
    if cmd == "download":
        download_scale(str(args.scale))
        return 0
    if cmd == "index":
        build_index(str(args.scale), force=True)
        return 0
    if cmd == "ping":
        return await cmd_ping(args.base_url)
    if cmd == "ingest":
        return await cmd_ingest(args)
    if cmd == "probe":
        return await cmd_probe(args)
    if cmd == "judge":
        return await cmd_judge(args)
    if cmd == "report":
        tag = args.tag or (f"n{args.limit}" if args.limit else "")
        write_report(str(args.scale), tag)
        return 0
    if cmd == "smoke":
        args.limit = args.limit or 4
        args.tag = args.tag or "smoke"
        return await cmd_all(args)
    if cmd == "all":
        return await cmd_all(args)
    print(f"unknown cmd {cmd}")
    return 1


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
