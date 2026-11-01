"""多人格隔离 E2E：打运行中的 core（ws://localhost:8765）。

用法：GSUID_LOCAL_TEST_TOKEN=xxx uv run --no-sync python eval/manual/multi_persona_e2e.py

观测通道：会话日志 ``data/ai_core/session_logs/*.json`` 顶层 ``persona_name`` +
``user_input`` 条目里的当轮装配文本（presence/relationship/mood 块都在里面）。
LLM 挂掉时（回复为执行出错文本）需要真实回复的断言标 SKIP(llm_unavailable)。
"""

from __future__ import annotations

import sys
import json
import time
import shutil
import asyncio
import hashlib
import sqlite3
import tempfile
from typing import Literal, Optional
from pathlib import Path
from collections.abc import Callable

import _ws_env
import websockets
from msgspec import json as msgjson

from gsuid_core.models import Message, MessageSend, MessageReceive

REPO = _ws_env.REPO_ROOT
DATA = REPO / "data"
DB_PATH = DATA / "GsData.db"
PERSONA_DIR = DATA / "ai_core" / "persona"
LOG_DIR = _ws_env.SESSION_LOG_DIR
LEASES_JSON = DATA / "ai_core" / "dm_leases.json"
ASIDE_JSON = DATA / "ai_core" / "aside_recall.json"

BOT_ID = "console"
BOT_SELF_ID = "900000001"
P_A = "早柚"
P_B = "e2e_persona_b"
G_A = "e2e_mp_grp_a"
G_B = "e2e_mp_grp_b"
U1, U2, U3, U4 = "e2e_mp_u1", "e2e_mp_u2", "e2e_mp_u3", "e2e_mp_u4"

RESULTS: list[tuple[str, str, str]] = []  # (name, PASS|FAIL|SKIP, note)


def report(name: str, status: str, note: str = "") -> None:
    RESULTS.append((name, status, note))
    print(f"[{status:>4}] {name}  {note}", flush=True)


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _send_text() -> Callable[..., MessageReceive]:
    def build(
        user_type: Literal["group", "direct", "channel", "sub_channel"],
        group_id: Optional[str],
        user_id: str,
        text: str,
        at: bool = False,
    ) -> MessageReceive:
        content = []
        if at:
            content.append(Message(type="at", data=BOT_SELF_ID))
        content.append(Message(type="text", data=text))
        return MessageReceive(
            bot_id=BOT_ID,
            bot_self_id=BOT_SELF_ID,
            user_type=user_type,
            user_pm=0,
            group_id=group_id,
            user_id=user_id,
            content=content,
            sender={"nickname": user_id},
        )

    return build


_BUILD = _send_text()


async def _reconnect(ws_box: list) -> None:
    try:
        await ws_box[0].close()
    except Exception:
        pass
    ws_box[0] = await websockets.connect(_ws_env.ws_url(), max_size=2**25, open_timeout=30)


async def send_and_recv(ws_box: list, user_type, group_id, user_id, text, *, at=False, idle=150.0, hard=420.0):
    """发一条消息并收回复（LLM 404 时也会回「执行出错」文本）。连接被重置时重连一次。"""
    before = time.time()
    for attempt in range(2):
        try:
            await ws_box[0].send(msgjson.encode(_BUILD(user_type, group_id, user_id, text, at)))
            break
        except (websockets.ConnectionClosed, ConnectionAbortedError, OSError):
            if attempt:
                raise
            await _reconnect(ws_box)
    print(f"  [SENT] {user_type} {group_id or user_id}: {text[:50]}", flush=True)
    texts: list[str] = []
    end = before + hard
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws_box[0].recv(), timeout=min(idle, end - time.time()))
        except asyncio.TimeoutError:
            break
        except (websockets.ConnectionClosed, ConnectionAbortedError, OSError):
            # 已发出的消息服务端照常处理；重连只影响后续接收
            await _reconnect(ws_box)
            continue
        try:
            resp = msgjson.decode(raw, type=MessageSend)
        except Exception:
            continue
        for seg in resp.content or []:
            if seg.type == "text" and str(seg.data).strip():
                texts.append(str(seg.data))
                print(f"  [RECV] {str(seg.data)[:80]}", flush=True)
    return texts, before


def newest_log(needle: str, after_ts: float):
    """session_id 含 needle 的最新日志。优先 mtime>=after_ts 的；没有则退到
    全体同名文件中最新者（续写旧段时 mtime 可能滞后）。"""
    fresh, stale = [], []
    for p in LOG_DIR.glob("*.json"):
        if needle in p.name:
            (fresh if p.stat().st_mtime >= after_ts - 1 else stale).append(p)
    pool = fresh or stale
    if not pool:
        return None, None
    best = max(pool, key=lambda p: p.stat().st_mtime)
    try:
        return best, json.loads(best.read_text(encoding="utf-8"))
    except Exception:
        return best, None


def last_user_input(log: dict) -> str:
    body = ""
    for e in log.get("entries", []):
        if e.get("type") == "user_input":
            body = str(e.get("data", {}).get("content", ""))
    return body


def _entry_ts(e: dict) -> float:
    raw = e.get("timestamp", "")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    return 0.0


def find_turn(needle: str, after_ts: float, sent_text: str, tries: int = 45):
    """所有匹配日志中，消息行恰为 sent_text 且 entry 时间 >= after_ts 的最新 user_input。

    返回 (content, persona_name, log)。私聊首字切人格会开新 session_uuid
    分段文件；同文本也会出现在后续轮次的对话历史里——用 ``--- 消息 ---``
    行精确定位本轮，而不是按文件 mtime 或裸子串匹配。
    """
    msg_line = f"--- 消息 ---\n{sent_text}\n"
    for _ in range(tries):
        best: tuple[float, str, str, dict] | None = None
        for p in LOG_DIR.glob("*.json"):
            if needle not in p.name:
                continue
            try:
                log = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for e in log.get("entries", []):
                if e.get("type") != "user_input":
                    continue
                c = str(e.get("data", {}).get("content", ""))
                if msg_line not in c:
                    continue
                ts = _entry_ts(e)
                if ts < after_ts - 2:
                    continue
                if best is None or ts > best[0]:
                    best = (ts, c, str(log.get("persona_name") or ""), log)
        if best is not None:
            return best[1], best[2], best[3]
        time.sleep(2)
    return "", None, None


def _persona_from_ctx(c: str) -> str | None:
    """当轮真实人格看 user_input 里的人格卡身份行；文件头 persona_name 只是分段创建者，
    私聊首字切人格后同链会话仍写旧文件，头部会误导。"""
    m = c.find("身份：你是「")
    if m < 0:
        return None
    s = c[m + len("身份：你是「") :]
    e = s.find("」")
    return s[:e] if e > 0 else None


def persona_for_turn(needle: str, after_ts: float, sent_text: str, tries: int = 10) -> tuple[str | None, dict | None]:
    c, persona, log = find_turn(needle, after_ts, sent_text, tries)
    return (_persona_from_ctx(c) or persona), log


def ctx_for_turn(needle: str, after_ts: float, sent_text: str, tries: int = 10) -> tuple[str, dict | None]:
    c, _persona, log = find_turn(needle, after_ts, sent_text, tries)
    return c, log


def is_err_reply(texts: list[str]) -> bool:
    """LLM 不可达时网关回的执行失败兜底文案（两种形态都认）。"""
    from gsuid_core.ai_core.utils import is_agent_error_text

    marks = ("处理失败", "[AI 执行失败]")
    if not texts:
        return True
    return all(is_agent_error_text(t) or any(m in t for m in marks) for t in texts)


def sqlite_rows(sql: str, args: tuple = ()):
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def sqlite_write(sql: str, args: tuple = ()) -> int:
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── persona / 状态准备 ─────────────────────────────────────────────────────


def make_persona_b() -> None:
    src = PERSONA_DIR / "凛音"
    dst = PERSONA_DIR / P_B
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    cfg_path = dst / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["scope"]["data"] = "specific"
    cfg["target_groups"]["data"] = [G_B]
    cfg["ai_mode"]["data"] = ["提及应答"]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


async def db_ready() -> None:
    from gsuid_core.utils.database.base_models import init_database

    await init_database()


async def write_state(scope: str, key: str, value) -> None:
    from gsuid_core.ai_core.state_store import state_set_value

    await state_set_value(scope, key, value)


_NN_DEFAULTS = {
    "user_name": "",
    "favorability": 0,
    "last_delta": 0,
    "last_reason": "",
    "last_eval_at": 0,
    "daily_gain": 0,
    "daily_loss": 0,
    "daily_ymd": "",
    "interaction_count": 0,
    "memory_count": 0,
    "tags": "[]",
    "last_interaction_time": 0,
    "last_positive_interact_at": 0,
    "interaction_vibe": "",
    "shared_callbacks": "[]",
    "last_vibe_distill_count": 0,
    "last_proactive_dm_at": 0,
    "dm_unanswered_streak": 0,
    "persona_name": "",
}


def seed_row(user: str, bot: str, persona: str, **fields) -> None:
    cols = ["user_id", "bot_id", "persona_name"]
    vals = [user, bot, persona]
    merged = dict(_NN_DEFAULTS)
    merged.update(fields)
    merged.setdefault("favorability", 30)
    for k, v in merged.items():
        if k in ("user_id", "bot_id", "persona_name"):
            continue
        cols.append(k)
        vals.append(v)
    ph = ",".join("?" for _ in cols)
    sqlite_write(
        f"INSERT OR REPLACE INTO userfavorability ({','.join(cols)}) VALUES ({ph})",
        tuple(vals),
    )


async def run() -> int:
    leases_before = LEASES_JSON.read_bytes() if LEASES_JSON.exists() else b""
    aside_before = ASIDE_JSON.read_bytes() if ASIDE_JSON.exists() else b""

    real_bot_id: str = BOT_ID
    ws_box: list | None = None
    homeo_a_orig = None
    try:
        make_persona_b()
        await db_ready()
        ws_box = [await websockets.connect(_ws_env.ws_url(), max_size=2**25, open_timeout=30)]
        # ── E1 路由 ─────────────────────────────────────────────────────────
        texts_a, t0 = await send_and_recv(ws_box, "group", G_A, U1, "早柚，今天想随便聊聊，最近过得怎么样", at=True)
        texts_b, t1 = await send_and_recv(ws_box, "group", G_B, U1, "你好，在吗，陪我聊两句", at=True)
        # 注：会话日志空闲60秒才落盘（IDLE_PERSIST_THRESHOLD），所有日志类断言统一延后到发送完毕再读

        # ── E2 好感度隔离 ────────────────────────────────────────────────────
        await asyncio.sleep(1)
        await send_and_recv(ws_box, "group", G_A, U1, "我最近开始学吉他，手指按弦按得好疼，但是很开心", at=True)
        await send_and_recv(ws_box, "group", G_B, U1, "我最近开始学吉他，手指按弦按得好疼，但是很开心", at=True)
        rows, personas = [], set()
        for _ in range(30):  # settle 写库滞后于回复，轮询到两行都落库
            rows = sqlite_rows(
                "SELECT persona_name, bot_id, favorability FROM userfavorability"
                " WHERE user_id=? AND user_id LIKE 'e2e_mp_%'",
                (U1,),
            )
            personas = {r[0] for r in rows}
            if personas >= {P_A, P_B}:
                break
            await asyncio.sleep(3)
        if rows:
            real_bot_id = rows[0][1]
        if is_err_reply(texts_a + texts_b):
            note = f"SKIP(llm_unavailable); rows seen: {personas}"
            report("E2 favorability two rows", "SKIP" if personas < {P_A, P_B} else "PASS", note)
        else:
            report("E2 favorability two rows", "PASS" if personas >= {P_A, P_B} else "FAIL", f"rows={rows}")

        # ── E3 homeostasis 提示注入 ──────────────────────────────────────────
        await write_state(
            f"persona:{P_B}",
            "homeostasis",
            {
                "energy": 0.20,
                "social_satiety": 0.40,
                "consecutive_turns": 0,
                "last_turn_at": 0.0,
                "last_updated_at": time.time(),
                "topic_fatigue": {},
            },
        )
        _, t2 = await send_and_recv(ws_box, "group", G_B, U1, "再聊一句吗", at=True)
        # 早柚真实稳态可能本身就低能量（连续对话 turn_shock 累积），先把精力抬高再发对照组
        homeo_a_snap = sqlite_rows(
            "SELECT value FROM aipersistentstate WHERE scope=? AND state_key='homeostasis'",
            (f"persona:{P_A}",),
        )
        homeo_a_orig = homeo_a_snap[0][0] if homeo_a_snap else None
        await write_state(
            f"persona:{P_A}",
            "homeostasis",
            {
                "energy": 0.85,
                "social_satiety": 0.40,
                "consecutive_turns": 0,
                "last_turn_at": 0.0,
                "last_updated_at": time.time(),
                "topic_fatigue": {},
            },
        )
        _, t3 = await send_and_recv(ws_box, "group", G_A, U1, "再聊一句吗", at=True)

        # ── E4 presence ACL ──────────────────────────────────────────────────
        now = time.time()
        await write_state(
            f"persona:{P_B}",
            "presence",
            {
                "offscreen_doing": "e2e_offscreen_marker",
                "offscreen_at": now,
                "echoes": [
                    {
                        "target_key": G_A,
                        "is_private": False,
                        "summary": "e2e_grp_echo_marker",
                        "emitted_at": now - 100,
                        "answered": False,
                        "settled": False,
                    },
                    {
                        "target_key": f"private:{U2}",
                        "is_private": True,
                        "summary": "e2e_priv_echo_marker",
                        "emitted_at": now - 100,
                        "answered": False,
                        "settled": False,
                    },
                ],
            },
        )
        _, t4 = await send_and_recv(ws_box, "group", G_B, U1, "再来一句", at=True)

        # ── E5 DM 租约路由 + streak 清零 ──────────────────────────────────────
        from gsuid_core.ai_core.proactive.dm_lease import grant_dm_lease

        grant_dm_lease(U2, P_B, real_bot_id)
        seed_row(U2, real_bot_id, P_B, favorability=50, dm_unanswered_streak=2)
        _, t5 = await send_and_recv(ws_box, "direct", None, U2, "看到你了，最近怎么样呀")
        streak = []
        for _ in range(30):  # 私聊回复到达后 settle 才把 streak 归零，轮询等待
            streak = sqlite_rows(
                "SELECT dm_unanswered_streak FROM userfavorability WHERE user_id=? AND persona_name=?",
                (U2, P_B),
            )
            if streak and streak[0][0] == 0:
                break
            await asyncio.sleep(3)
        ok5_streak = bool(streak and streak[0][0] == 0)

        # ── E6 私聊首字切人格 ────────────────────────────────────────────────
        _, t6 = await send_and_recv(ws_box, "direct", None, U2, "早柚，你今天在忙什么呀")
        lease_p = None
        for _ in range(30):  # 首字匹配到写 leases.json 之间有处理延迟，轮询等切换生效
            lease_data = json.loads(LEASES_JSON.read_text(encoding="utf-8")).get("leases", {})
            lease_p = next((v.get("persona_name") for k, v in lease_data.items() if k == U2), None)
            if lease_p == P_A:
                break
            await asyncio.sleep(3)
        _, t7 = await send_and_recv(ws_box, "direct", None, U2, "我跟你说，早柚刚才那句话好可爱")
        await asyncio.sleep(5)  # 若误切会覆盖租约，稍等再读
        lease_data2 = json.loads(LEASES_JSON.read_text(encoding="utf-8")).get("leases", {})
        lease_p2 = next((v.get("persona_name") for k, v in lease_data2.items() if k == U2), None)

        # ── E7 vibe/callbacks 注入 ───────────────────────────────────────────
        seed_row(
            U3,
            real_bot_id,
            P_A,
            favorability=50,
            interaction_vibe="e2e_vibe_marker",
            shared_callbacks='["e2e_cb_marker"]',
            interaction_count=5,
        )
        _, t8 = await send_and_recv(ws_box, "group", G_A, U3, "还记得我们上次的约定吗", at=True)
        _, t9 = await send_and_recv(ws_box, "group", G_B, U3, "还记得我们上次的约定吗", at=True)

        # ── 日志类断言统一在这里读盘 ─────────────────────────────────────────
        # session_logger 只在会话空闲 60s 后增量落盘（POLL_INTERVAL=15s 轮询），
        # 此时所有会话都已静默超过阈值，逐条按消息行 + 时间戳精确取回。
        pa, _ = persona_for_turn(G_A, t0, "早柚，今天想随便聊聊，最近过得怎么样")
        pb, _ = persona_for_turn(G_B, t1, "你好，在吗，陪我聊两句")
        report("E1 routing persona", "PASS" if (pa == P_A and pb == P_B) else "FAIL", f"grp_a={pa} grp_b={pb}")

        ctx_b, _ = ctx_for_turn(G_B, t2, "再聊一句吗")
        ctx_a, _ = ctx_for_turn(G_A, t3, "再聊一句吗")
        tired = "有点倦" in ctx_b or "回应简短随性" in ctx_b
        report(
            "E3 homeostasis hint",
            "PASS" if (tired and "有点倦" not in ctx_a) else "FAIL",
            f"b_tired={tired} a_clean={'有点倦' not in ctx_a} ctx_len={len(ctx_b)}/{len(ctx_a)}",
        )

        ctx_b3, _ = ctx_for_turn(G_B, t4, "再来一句")
        e4a = "e2e_offscreen_marker" in ctx_b3
        e4b = "e2e_grp_echo_marker" not in ctx_b3 and "e2e_priv_echo_marker" not in ctx_b3
        report("E4a offscreen+echo ACL (grp_b)", "PASS" if (e4a and e4b) else "FAIL", f"offscreen={e4a} no_echo={e4b}")

        p_priv, _ = persona_for_turn(U2, t5, "看到你了，最近怎么样呀")
        ok5 = p_priv == P_B and ok5_streak
        report("E5 lease routes + streak reset", "PASS" if ok5 else "FAIL", f"persona={p_priv} streak={streak}")
        ctx_priv, _ = ctx_for_turn(U2, t5, "看到你了，最近怎么样呀")
        e4c = "e2e_priv_echo_marker" in ctx_priv and "e2e_grp_echo_marker" not in ctx_priv
        report("E4b private echo ACL", "PASS" if e4c else "FAIL", f"ctx_has_priv={'e2e_priv_echo_marker' in ctx_priv}")

        p_sw, _ = persona_for_turn(U2, t6, "早柚，你今天在忙什么呀")
        ok6 = p_sw == P_A and lease_p == P_A and lease_p2 == P_A
        report(
            "E6 lease start-of-msg switch",
            "PASS" if ok6 else "FAIL",
            f"turn_persona={p_sw} lease={lease_p} mid_lease={lease_p2}",
        )

        ctx_u3a, _ = ctx_for_turn(G_A, t8, "还记得我们上次的约定吗")
        ctx_u3b, _ = ctx_for_turn(G_B, t9, "还记得我们上次的约定吗")
        e7a = "e2e_vibe_marker" in ctx_u3a and "e2e_cb_marker" in ctx_u3a
        e7b = "e2e_vibe_marker" not in ctx_u3b and "e2e_cb_marker" not in ctx_u3b
        report("E7 vibe/callbacks scoped", "PASS" if (e7a and e7b) else "FAIL", f"a_has={e7a} b_clean={e7b}")

        # ── E8 双边蒸馏触发 ──────────────────────────────────────────────────
        seed_row(U4, real_bot_id, P_A, favorability=40, interaction_count=30, last_vibe_distill_count=0)
        texts8, t10 = await send_and_recv(
            ws_box, "group", G_A, U4, "跟你分享个事，我今天终于把拖了很久的项目收尾了，特别轻松", at=True
        )
        deadline = time.time() + 90
        final_count = None
        while time.time() < deadline:
            r = sqlite_rows(
                "SELECT last_vibe_distill_count, interaction_count, interaction_vibe FROM userfavorability "
                "WHERE user_id=? AND persona_name=?",
                (U4, P_A),
            )
            if r and r[0][0] >= r[0][1]:
                final_count = r[0]
                break
            await asyncio.sleep(3)
        if final_count is not None:
            vibe_note = f"count={final_count[0]} vibe={'set' if final_count[2] else 'empty'}"
            note_extra = "; LLM down, count-only expected" if is_err_reply(texts8) else ""
            report("E8 dyadic distill count", "PASS", vibe_note + note_extra)
        else:
            report("E8 dyadic distill count", "FAIL", f"row after 90s: {r}")

        # ── E10 老库迁移 ─────────────────────────────────────────────────────
        backup = DATA / "GsCore_BACKUP_PATH" / "2026-10-19-2026-10-19" / "GsData.db"
        if not backup.exists():
            report("E10 legacy migration", "FAIL", f"backup missing: {backup}")
        else:
            with tempfile.TemporaryDirectory() as td:
                work = Path(td) / "GsData.db"
                shutil.copy2(backup, work)
                ok10, note10 = await _e10_migration(work)
                report("E10 legacy migration", "PASS" if ok10 else "FAIL", note10)

        # ── E11 自治/巡检触发 ───────────────────────────────────────────────
        report(
            "E11 autonomy/heartbeat trigger",
            "NOT COVERED",
            "no master command or local-test endpoint to trigger a run without waiting for scheduler interval",
        )

    finally:
        if ws_box is not None:
            await ws_box[0].close()
        # ── 清理 ────────────────────────────────────────────────────────────
        try:
            shutil.rmtree(PERSONA_DIR / P_B, ignore_errors=True)
            if homeo_a_orig is not None:
                await write_state(f"persona:{P_A}", "homeostasis", json.loads(homeo_a_orig))
            sqlite_write("DELETE FROM userfavorability WHERE user_id LIKE 'e2e_mp_%'")
            sqlite_write(
                "DELETE FROM aipersistentstate WHERE scope=? OR scope LIKE 'self:%:e2e_persona_b'",
                (f"persona:{P_B}",),
            )
            if leases_before:
                LEASES_JSON.write_bytes(leases_before)
            if aside_before:
                ASIDE_JSON.write_bytes(aside_before)
            left = sqlite_rows("SELECT COUNT(*) FROM userfavorability WHERE user_id LIKE 'e2e_mp_%'")[0][0]
            left2 = sqlite_rows(
                "SELECT COUNT(*) FROM aipersistentstate "
                "WHERE scope LIKE 'persona:e2e%' OR scope LIKE 'self:%:e2e_persona_b'"
            )[0][0]
            print(f"[CLEAN] remaining e2e favorability rows={left} state rows={left2}", flush=True)
            for name, before, path in (("leases", leases_before, LEASES_JSON), ("aside", aside_before, ASIDE_JSON)):
                cur = path.read_bytes() if path.exists() else b""
                h0 = hashlib.sha256(before).hexdigest()[:16]
                h1 = hashlib.sha256(cur).hexdigest()[:16]
                print(f"[CLEAN] {name}.json checksum before={h0} after={h1} match={before == cur}", flush=True)
        except Exception as e:
            print(f"[CLEAN-ERR] {e}", flush=True)

    def _rk(item):
        name = item[0]
        num = ""
        i = 1
        while i < len(name) and name[i].isdigit():
            num += name[i]
            i += 1
        suffix = name[i] if i < len(name) and name[i].isalpha() else ""
        return (int(num) if num else 99, suffix)

    print("\n=== RESULT TABLE ===")
    for n, st, note in sorted(RESULTS, key=_rk):
        print(f"{st:>4}  {n}  {note}")
    fails = sum(1 for _, st, _ in RESULTS if st == "FAIL")
    print(f"=== {len(RESULTS) - fails}/{len(RESULTS)} ok, {fails} FAIL ===")
    return 1 if fails else 0


async def _e10_migration(work: Path):
    """把老库拷本过一遍 schema 迁移 + 默认人格归并。"""
    from sqlalchemy import text
    from sqlalchemy.pool import NullPool
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from gsuid_core.utils.database import base_models
    from gsuid_core.ai_core.database.models import UserFavorability
    from gsuid_core.ai_core.relationship.migration import migrate_userfavorability_schema

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{work.as_posix()}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 10.0},
    )
    old_engine, old_maker = base_models.engine, base_models.async_maker
    base_models.engine = engine
    base_models.async_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        # 复刻启动顺序：先 exec_list 补列/建索引（对老表幂等），再做约束迁移
        from gsuid_core.utils.database.startup import exec_list

        async with engine.begin() as conn:
            before = (await conn.execute(text("SELECT COUNT(*) FROM userfavorability"))).scalar_one()
        for stmt in exec_list:
            if "userfavorability" not in stmt:
                continue
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(stmt))
            except Exception:
                pass
        await migrate_userfavorability_schema(engine)
        moved, deleted = await UserFavorability.reassign_legacy_persona(P_A)
        async with engine.begin() as conn:
            after = (await conn.execute(text("SELECT COUNT(*) FROM userfavorability"))).scalar_one()
            empty_p = (
                await conn.execute(text("SELECT COUNT(*) FROM userfavorability WHERE persona_name=''"))
            ).scalar_one()
            idx = await conn.execute(text("PRAGMA index_list('userfavorability')"))
            uniques = []
            for r in idx.fetchall():
                if r[2]:
                    cols = [c[2] for c in (await conn.execute(text(f"PRAGMA index_info('{r[1]}')"))).fetchall()]
                    uniques.append(sorted(cols))
            has3 = sorted(["user_id", "bot_id", "persona_name"]) in uniques
            has2 = sorted(["user_id", "bot_id"]) in uniques
        # 第二人格同 (user,bot) 可插
        async with engine.begin() as conn:
            row = (await conn.execute(text("SELECT user_id, bot_id FROM userfavorability LIMIT 1"))).fetchone()
            ins_ok = False
            if row:
                try:
                    await conn.execute(
                        text(
                            "INSERT INTO userfavorability (user_id,bot_id,persona_name,favorability,"
                            "interaction_count,last_interaction_time,memory_count) "
                            "VALUES (:u,:b,:p,0,0,0,0)"
                        ),
                        {"u": row[0], "b": row[1], "p": "e2e_persona_x"},
                    )
                    ins_ok = True
                    await conn.execute(text("DELETE FROM userfavorability WHERE persona_name='e2e_persona_x'"))
                except Exception:
                    ins_ok = False
        # 再跑一遍应无变化
        moved2, deleted2 = await UserFavorability.reassign_legacy_persona(P_A)
        note = (
            f"before={before} after={after} moved={moved} merged_del={deleted} empty={empty_p} "
            f"3col={has3} 2col={has2} ins2={ins_ok} rerun=({moved2},{deleted2})"
        )
        ok = (
            has3
            and not has2
            and empty_p == 0
            and ins_ok
            and moved2 == 0
            and deleted2 == 0
            and after == before - deleted
        )
        return ok, note
    finally:
        base_models.engine, base_models.async_maker = old_engine, old_maker
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
