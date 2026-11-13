"""收件箱 / 线程视图 / 工具证据层 实机群聊验收（需已启动 core，勿带 --dev）。

模拟一个 OneBot 风格适配器：每条出站都回 ``recall_message_id`` 回执，使 Bot 出站
拿到平台 msg_id，群友才能「引用 Bot」。全部人名 / ID / 群号为虚拟数据。

判定只用结构事实：出站帧（文本 / reply / at 段）+ session log（run_start / tool_call /
user_input）。人格口吻类结论只打印，供人工复核。

用法::
    $env:GSUID_LOCAL_TEST_TOKEN = "<WS_TOKEN>"
    uv run python eval/manual/inbox_group_e2e.py               # 全部用例
    uv run python eval/manual/inbox_group_e2e.py --only C3,C4b  # 子集
    uv run python eval/manual/inbox_group_e2e.py --samples 3   # C4a/C4b 采样次数
"""

from __future__ import annotations

import json
import time
import asyncio
import argparse
from typing import Literal, Callable, Awaitable
from dataclasses import field, dataclass

import _ws_env
import websockets
from msgspec import json as msgjson
from websockets.asyncio.client import ClientConnection

from gsuid_core.models import Message, MessageSend, MessageReceive

BOT_ID = "inbox_e2e"
BOT_SELF = "900000001"
WAKE = "sayu"
ACT_FOR_DENY_HEAD = "本批有多人找你"
# session log 空闲 60 秒才落盘：全部用例跑完后统一等这么久再判 log 类断言
LOG_SETTLE_S = 75.0
EXEMPT_TOOLS = frozenset({"act_for", "find_tools", "read_handle", "capability_map"})
USERS: dict[str, str] = {
    "A": "100000011",
    "B": "100000012",
    "C": "100000013",
    "D": "100000014",
}
NAMES: dict[str, str] = {k: f"测试用户{k}" for k in USERS}


@dataclass
class Outbound:
    ts: float
    target_id: str
    texts: list[str]
    reply_ids: list[str]
    ats: list[str]
    platform_id: str


@dataclass
class LogEntry:
    ts: float
    kind: str
    data: dict[str, object]


@dataclass
class CaseResult:
    name: str
    passed: bool
    checks: list[tuple[str, bool]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Sim:
    """单 WS 连接；后台收帧并回 echo。"""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws
        self.outbound: list[Outbound] = []
        self._seq = 0
        self._bot_seq = 0
        self._task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        while True:
            raw = await self.ws.recv()
            resp = msgjson.decode(raw, type=MessageSend)
            if not resp.content:
                continue
            self._bot_seq += 1
            platform_id = f"bot-{self._bot_seq}"
            texts: list[str] = []
            reply_ids: list[str] = []
            ats: list[str] = []
            for seg in resp.content:
                if seg.type == "text" and str(seg.data or "").strip():
                    texts.append(str(seg.data))
                elif seg.type in ("reply", "reply_id") and seg.data:
                    reply_ids.append(str(seg.data))
                elif seg.type == "at" and seg.data:
                    ats.append(str(seg.data))
            if texts or reply_ids or ats:
                self.outbound.append(
                    Outbound(time.time(), str(resp.target_id or ""), texts, reply_ids, ats, platform_id)
                )
                print(f"  [OUT→{resp.target_id}] reply={reply_ids} at={ats} {' | '.join(texts)[:160]}")
            if resp.echo:
                ack = MessageReceive(
                    bot_id=BOT_ID,
                    bot_self_id=BOT_SELF,
                    content=[Message(type="recall_message_id", data={"echo": resp.echo, "id": platform_id})],
                )
                await self.ws.send(msgjson.encode(ack))

    async def say(
        self,
        group_id: str,
        who: str,
        text: str,
        *,
        at_bot: bool = False,
        quote: tuple[str, str] | None = None,
    ) -> str:
        """quote=(被引用 msg_id, 被引用正文)。引用 Bot 时按 C 端习惯自动补 @Bot。"""
        self._seq += 1
        msg_id = f"e2e-{group_id}-{self._seq}"
        content: list[Message] = []
        if quote is not None:
            content.append(Message(type="reply_id", data=quote[0]))
            content.append(Message(type="reply", data=quote[1]))
        if at_bot:
            content.append(Message(type="at", data=BOT_SELF))
        content.append(Message(type="text", data=text))
        msg = MessageReceive(
            bot_id=BOT_ID,
            bot_self_id=BOT_SELF,
            msg_id=msg_id,
            user_type="group",
            group_id=group_id,
            user_id=USERS[who],
            sender={"nickname": NAMES[who]},
            user_pm=6,
            content=content,
        )
        await self.ws.send(msgjson.encode(msg))
        tag = f" ↩{quote[0]}" if quote else ""
        print(f"  [IN {group_id}] {NAMES[who]}{' @Bot' if at_bot else ''}{tag}: {text}")
        return msg_id

    def outs(self, group_id: str, since: float) -> list[Outbound]:
        return [o for o in self.outbound if o.target_id == group_id and o.ts >= since]

    async def wait_quiet(self, group_id: str, *, first: float, idle: float, hard: float = 300.0) -> None:
        """先等首条出站（最多 first 秒），出现后再等 idle 秒无新出站；总上限 hard。"""
        start = time.time()
        while time.time() - start < hard:
            await asyncio.sleep(1.0)
            outs = self.outs(group_id, start)
            if not outs:
                if time.time() - start >= first:
                    return
                continue
            if time.time() - outs[-1].ts >= idle:
                return


def session_log_entries(group_id: str, since: float) -> list[LogEntry]:
    """主会话（非子 Agent）session log 里 since 之后的条目。"""
    out: list[LogEntry] = []
    for path in _ws_env.SESSION_LOG_DIR.glob("*.json"):
        if path.stat().st_mtime < since - 5:
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        sid = str(doc["session_id"]) if "session_id" in doc else ""
        if f":group:{group_id}" not in sid:
            continue
        if "is_subagent" in doc and bool(doc["is_subagent"]):
            continue
        entries = doc["entries"] if "entries" in doc and isinstance(doc["entries"], list) else []
        for e in entries:
            if not isinstance(e, dict) or "type" not in e or "timestamp" not in e:
                continue
            ts = float(e["timestamp"])
            if ts < since:
                continue
            data = e["data"] if "data" in e and isinstance(e["data"], dict) else {}
            out.append(LogEntry(ts, str(e["type"]), data))
    out.sort(key=lambda x: x.ts)
    return out


def runs(entries: list[LogEntry]) -> int:
    return sum(1 for e in entries if e.kind == "run_start")


def user_inputs(entries: list[LogEntry]) -> list[str]:
    return [str(e.data["content"]) for e in entries if e.kind == "user_input" and "content" in e.data]


def tool_calls(entries: list[LogEntry]) -> list[str]:
    return [str(e.data["tool_name"]) for e in entries if e.kind == "tool_call" and "tool_name" in e.data]


def tool_returns(entries: list[LogEntry]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in entries:
        if e.kind != "tool_return":
            continue
        name = str(e.data["tool_name"]) if "tool_name" in e.data else ""
        body = str(e.data["content"]) if "content" in e.data else str(e.data)
        out.append((name, body))
    return out


def no_tag_leak(outs: list[Outbound]) -> bool:
    return not any("<reply" in t or "＜reply" in t or "SILENCE" in t for o in outs for t in o.texts)


Verdict = Callable[[], CaseResult]


def _finish(name: str, checks: list[tuple[str, bool]], notes: list[str]) -> CaseResult:
    return CaseResult(name, all(ok for _, ok in checks), checks, notes)


async def case_c2(sim: Sim) -> Verdict:
    """C2 排队引用他人：Bot 运行中，B 引用 C 的话说「神了」，不得当成夸 Bot。"""
    gid = "100000902"
    t0 = time.time()
    await sim.say(gid, "C", "pil活")
    m_draw = await sim.say(gid, "C", "让你画图")
    await asyncio.sleep(1.0)
    await sim.say(gid, "B", f"{WAKE} ぴー活是什么意思", at_bot=True)
    await asyncio.sleep(2.5)
    await sim.say(gid, "B", "神了", quote=(m_draw, "让你画图"))
    await sim.wait_quiet(gid, first=120, idle=30)

    def verdict() -> CaseResult:
        ents = session_log_entries(gid, t0)
        outs = sim.outs(gid, t0)
        quote_rendered = any("神了" in u and "↩#" in u and NAMES["C"] in u for u in user_inputs(ents))
        checks = [
            ("Bot 回答了点名问题", len(outs) >= 1),
            ("「神了」没有单独唤醒主模型（轮数 = 1）", runs(ents) == 1),
            ("无协议标签泄漏", no_tag_leak(outs)),
        ]
        return _finish("C2 排队引用他人", checks, [f"runs={runs(ents)} 「神了」引用进时间线={quote_rendered}"])

    return verdict


async def case_c3(sim: Sim) -> Verdict:
    """C3 多人同时找：5 秒内 3 人点名，其中 2 人请求相同。"""
    gid = "100000903"
    t0 = time.time()
    ask = f"{WAKE} 早餐喝粥好还是吃面包好，一句话说说"
    ids = [await sim.say(gid, "A", ask, at_bot=True)]
    await asyncio.sleep(0.6)
    ids.append(await sim.say(gid, "B", ask, at_bot=True))
    await asyncio.sleep(0.6)
    ids.append(await sim.say(gid, "C", f"{WAKE} 讲一个很短的冷笑话", at_bot=True))
    await sim.wait_quiet(gid, first=150, idle=30)

    def verdict() -> CaseResult:
        ents = session_log_entries(gid, t0)
        outs = sim.outs(gid, t0)
        n = runs(ents)
        checks = [
            ("主模型轮数 ≤ 2", 1 <= n <= 2),
            ("气泡数 ≤ 3 × 轮数", len(outs) <= 3 * max(1, n)),
            ("至少回了一个人", len(outs) >= 1),
            ("引用段只指向本批消息", all(set(o.reply_ids) <= set(ids) for o in outs)),
            ("@ 段只指向本批说话人", all(set(o.ats) <= set(USERS.values()) for o in outs)),
            ("待回应块出现在 user 输入里", any("（待你回应）本批" in u for u in user_inputs(ents))),
            ("无协议标签泄漏", no_tag_leak(outs)),
        ]
        return _finish("C3 多人同时找", checks, [f"runs={n} bubbles={len(outs)}"])

    return verdict


async def _bot_reply_to(sim: Sim, gid: str, who: str, text: str) -> Outbound | None:
    t = time.time()
    await sim.say(gid, who, text, at_bot=True)
    await sim.wait_quiet(gid, first=120, idle=15)
    outs = sim.outs(gid, t)
    return outs[-1] if outs else None


async def case_c4(sim: Sim, samples: int, mode: Literal["ask", "lurk"]) -> Verdict:
    """C4a 引用求评价应回；C4b 引用只发「哈哈哈」应沉默。"""
    name = "C4a 引用求评价" if mode == "ask" else "C4b 引用围观"
    replied = 0
    valid = 0
    notes: list[str] = []
    for i in range(samples):
        gid = f"10000094{i}" if mode == "ask" else f"10000095{i}"
        base = await _bot_reply_to(sim, gid, "A", f"{WAKE} 用一句话说说你对咖啡的看法")
        if base is None:
            notes.append(f"样本{i}: 前置问题 Bot 未回复，跳过")
            continue
        await asyncio.sleep(3)
        t = time.time()
        body = "你怎么看，这说法靠谱吗" if mode == "ask" else "哈哈哈"
        await sim.say(gid, "B", body, at_bot=True, quote=(base.platform_id, " ".join(base.texts)[:60]))
        await sim.wait_quiet(gid, first=60, idle=15)
        got = len(sim.outs(gid, t)) > 0
        valid += 1
        replied += 1 if got else 0
        notes.append(f"样本{i}: {'回复' if got else '沉默'}")
    if mode == "ask":
        # 软唤醒是否回话由 TurnRouter 决定，允许偶发沉默；过半不回才算管线问题
        check = (f"回复率 ≥ 60%（{replied}/{valid}）", replied >= 1 and replied / valid >= 0.6)
    else:
        silent = valid - replied
        check = (f"沉默率 ≥ 70%（{silent}/{valid}）", valid > 0 and silent / valid >= 0.7)
    result = _finish(name, [check], notes)
    return lambda: result


async def case_c5(sim: Sim) -> Verdict:
    """C5 省略主语续问：A 点名问 X → Bot 答 → 30 秒内 A 不点名问「那 Y 呢」。"""
    gid = "100000905"
    base = await _bot_reply_to(sim, gid, "A", f"{WAKE} 番茄炒蛋先放番茄还是先放蛋")
    if base is None:
        missing = _finish("C5 省略主语续问", [("前置问题有回复", False)], [])
        return lambda: missing
    await asyncio.sleep(4)
    t = time.time()
    await sim.say(gid, "A", "那青椒炒肉呢")
    await sim.wait_quiet(gid, first=90, idle=15)
    outs = sim.outs(gid, t)
    checks = [("续问得到回复", len(outs) >= 1), ("无协议标签泄漏", no_tag_leak(outs))]
    result = _finish("C5 省略主语续问", checks, [" | ".join(" ".join(o.texts) for o in outs)[:200]])
    return lambda: result


async def case_c1(sim: Sim) -> Verdict:
    """C1 跨人凭据：B 查个人订单后 A 问同一问题，A 那轮须自己重查且看得到 B 的观测。"""
    gid = "100000901"
    ask = f"{WAKE} 帮我查一下我瑞幸最近的订单"
    t0 = time.time()
    await sim.say(gid, "B", ask, at_bot=True)
    await sim.wait_quiet(gid, first=180, idle=30)
    await asyncio.sleep(3)
    t1 = time.time()
    await sim.say(gid, "A", ask, at_bot=True)
    await sim.wait_quiet(gid, first=180, idle=30)

    def verdict() -> CaseResult:
        first = [e for e in session_log_entries(gid, t0) if e.ts < t1]
        second = session_log_entries(gid, t1)
        # 用哪类工具由实体别名表决定（本环境瑞幸→GenshinUID），不变量是「按各自身份重查」而非具体工具名
        b_tools = [n for n in tool_calls(first) if n not in EXEMPT_TOOLS]
        a_tools = [n for n in tool_calls(second) if n not in EXEMPT_TOOLS]
        if not b_tools:
            return _finish("C1 跨人凭据", [("B 那轮调用了用户态工具（前置）", False)], ["B 那轮无工具调用，用例不可判"])
        evidence_seen = any("（证据）" in u and f"替{NAMES['B']}查" in u for u in user_inputs(second))
        checks = [
            ("A 那轮重新调用了用户态工具", len(a_tools) >= 1),
            ("A 那轮证据块列出替 B 查的观测", evidence_seen),
        ]
        return _finish("C1 跨人凭据", checks, [f"B 工具={b_tools} A 工具={a_tools}"])

    return verdict


async def case_act_for(sim: Sim) -> Verdict:
    """多目标批次：A、D 同时要查个人订单；非豁免工具必须在 act_for 之后才执行成功。"""
    gid = "100000906"
    t0 = time.time()
    await sim.say(gid, "A", f"{WAKE} 帮我看看我瑞幸最近的订单", at_bot=True)
    await asyncio.sleep(0.5)
    await sim.say(gid, "D", f"{WAKE} 也帮我看看我瑞幸最近的订单", at_bot=True)
    await sim.wait_quiet(gid, first=240, idle=30)

    def verdict() -> CaseResult:
        ents = session_log_entries(gid, t0)
        calls = tool_calls(ents)
        rets = tool_returns(ents)
        first_act = calls.index("act_for") if "act_for" in calls else -1
        before = [n for n in (calls[:first_act] if first_act >= 0 else calls) if n not in EXEMPT_TOOLS]
        denied = [n for n, body in rets if ACT_FOR_DENY_HEAD in body]
        listed = any(e.kind == "tools_list" and "tools" in e.data and "act_for" in str(e.data["tools"]) for e in ents)
        checks = [
            ("同批两人进了一轮（工具身份块出现）", any("（工具身份）" in u for u in user_inputs(ents))),
            ("act_for 在本轮工具列表里", listed),
            ("act_for 之前的非豁免工具全部被拒", len(before) <= len(denied)),
            ("得到回复", len(sim.outs(gid, t0)) >= 1),
        ]
        if any(n.startswith("mcp_") for n in calls):
            checks.append(("MCP 工具在 act_for 之后调用", first_act >= 0))
        return _finish("ACT 多目标工具身份", checks, [f"calls={calls}"])

    return verdict


CASES: dict[str, Callable[[Sim, int], Awaitable[Verdict]]] = {
    "C1": lambda s, n: case_c1(s),
    "C2": lambda s, n: case_c2(s),
    "C3": lambda s, n: case_c3(s),
    "C4a": lambda s, n: case_c4(s, n, "ask"),
    "C4b": lambda s, n: case_c4(s, n, "lurk"),
    "C5": lambda s, n: case_c5(s),
    "ACT": lambda s, n: case_act_for(s),
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="逗号分隔用例名，如 C3,C4b")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    picked = [c.strip() for c in args.only.split(",") if c.strip()] or list(CASES)
    url = f"ws://localhost:8765/ws/{BOT_ID}?token={_ws_env.require_token()}"
    ws = await websockets.connect(url, max_size=2**25, open_timeout=30)
    sim = Sim(ws)
    verdicts: list[Verdict] = []
    for name in picked:
        print(f"\n=== {name} ===")
        verdicts.append(await CASES[name](sim, args.samples))
        await asyncio.sleep(3)
    print(f"\n等待 session log 落盘 {LOG_SETTLE_S:.0f}s …")
    await asyncio.sleep(LOG_SETTLE_S)
    results = [v() for v in verdicts]
    await ws.close()
    print("\n=========== 结果 ===========")
    for r in results:
        print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}")
        for label, ok in r.checks:
            print(f"    {'✓' if ok else '✗'} {label}")
        for note in r.notes:
            print(f"    · {note}")
    print(f"\n{sum(r.passed for r in results)}/{len(results)} 通过")


if __name__ == "__main__":
    asyncio.run(main())
