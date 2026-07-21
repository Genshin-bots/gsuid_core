"""长对话 OOC 真实测试脚本（100+ 轮）。

模拟真实群聊场景：
- 前 20 轮：闲聊（建立角色基线）
- 20-80 轮：密集股票/金融查询（模拟 OOC 触发条件）
- 80-100 轮：切回闲聊（检测语域是否恢复）
- 100-120 轮：混合对话（检测稳定性）

每 10 轮采样一次，记录：
- 回复长度
- 角色语气词密度
- 结构化格式出现率
- 语域指标

用法: GSUID_LOCAL_TEST_TOKEN=xxx uv run python -X utf8 _ooc_long_test.py
"""

import io
import os
import sys
import json
import time
import asyncio
from typing import List
from dataclasses import field, dataclass

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://127.0.0.1:8765"
TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "")
PERSONA = "早柚"
USER_ID = f"ooc_long_test_{int(time.time())}"
MAX_HISTORY = 50  # 模拟真实 compact 行为

# 角色语气词（早柚特征）
TONE_MARKERS = ["唔", "嗯", "哈欠", "呼", "本貉", "卷轴", "啦", "呢", "~", "…", "诶", "哎"]
# OOC 指标词（专业分析师特征）
OOC_MARKERS = [
    "首先",
    "其次",
    "综上",
    "建议",
    "策略",
    "配置",
    "估值",
    "股息率",
    "PE",
    "PB",
    "MACD",
    "KDJ",
    "RSI",
    "BOLL",
]


@dataclass
class TurnResult:
    turn: int
    phase: str
    user_msg: str
    reply: str
    reply_len: int
    tone_density: float  # 语气词密度
    ooc_density: float  # OOC 指标词密度
    has_structured: bool  # 含表格/编号/加粗
    latency_s: float


@dataclass
class SessionReport:
    turns: List[TurnResult] = field(default_factory=list)
    phase_summaries: dict = field(default_factory=dict)


def compute_tone_density(text: str) -> float:
    if not text:
        return 0.0
    hits = sum(1 for m in TONE_MARKERS if m in text)
    return hits / max(1, len(text) / 50)


def compute_ooc_density(text: str) -> float:
    if not text:
        return 0.0
    hits = sum(1 for m in OOC_MARKERS if m in text)
    return hits / max(1, len(text) / 100)


def has_structured_format(text: str) -> bool:
    lines = text.split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            return True
        if stripped and stripped[0].isdigit() and len(stripped) > 1 and stripped[1] in ".、)":
            return True
        if stripped.startswith("**") and stripped.endswith("**"):
            return True
    return False


# ─── 对话脚本 ───────────────────────────────────────────────────────────────


def build_conversation_script() -> List[dict]:
    """构建 120 轮对话脚本，模拟真实 OOC 触发场景。"""
    turns = []

    # Phase 1: 闲聊基线（1-20 轮）
    casual_msgs = [
        "早柚早上好呀~",
        "今天天气怎么样？",
        "你吃早饭了吗？",
        "最近有什么好玩的事吗？",
        "哈哈哈你好可爱",
        "早柚你在干嘛呢？",
        "我好无聊啊陪我说说话",
        "你喜欢吃什么呀？",
        "早柚你困不困？",
        "嘿嘿，早柚你今天心情好吗？",
        "我跟你说个笑话哈",
        "早柚你会做饭吗？",
        "你平时都做些什么呀？",
        "早柚你有没有什么爱好？",
        "我好累啊今天加班到好晚",
        "早柚安慰我一下嘛",
        "你觉得猫好还是狗好？",
        "早柚你怕不怕打雷？",
        "晚安早柚~",
        "早柚早~又是新的一天",
    ]
    for msg in casual_msgs:
        turns.append({"phase": "casual_baseline", "message": msg})

    # Phase 2: 密集股票查询（21-80 轮）——模拟真实 OOC 触发
    stock_queries = [
        "帮我看看贵州茅台最近走势",
        "宁德时代的MACD怎么样？",
        "比亚迪现在能买吗？",
        "招商银行最近表现如何？",
        "看看中国平安的技术指标",
        "长江电力最近有没有金叉？",
        "药明康德现在什么价位？",
        "帮我分析一下半导体板块",
        "中芯国际的KDJ超买了吗？",
        "隆基绿能最近走势怎么样？",
        "看看五粮液的RSI",
        "招商银行和工商银行哪个更值得买？",
        "最近大盘怎么样？",
        "帮我看看沪深300的趋势",
        "创业板指最近有没有破位？",
        "中国平安的股息率是多少？",
        "帮我算一下茅台的PE",
        "宁德时代和比亚迪你更看好哪个？",
        "最近有没有什么好的投资机会？",
        "帮我看看医药板块最近怎么样",
        "恒瑞医药的技术面如何？",
        "看看招商银行的年报数据",
        "贵州茅台的营收增速怎么样？",
        "帮我分析一下新能源板块的前景",
        "隆基绿能和通威股份你选哪个？",
        "最近北向资金流入情况怎么样？",
        "帮我看看今天涨停板有什么规律",
        "半导体ETF最近表现如何？",
        "中芯国际的产能利用率怎么样？",
        "帮我看看消费板块最近有没有机会",
        "五粮液和泸州老窖哪个好？",
        "最近市场情绪怎么样？",
        "帮我分析一下银行股的估值",
        "四大行现在PE多少？",
        "工商银行的ROE怎么样？",
        "建设银行的不良率最近有变化吗？",
        "帮我看看农业银行的股息",
        "中国银行的海外业务怎么样？",
        "交通银行最近有什么利好吗？",
        "邮储银行的网点优势还在吗？",
        "帮我看看煤炭板块最近怎么样",
        "中国神华的股息率还有多少？",
        "陕西煤业最近走势如何？",
        "兖矿能源的产能怎么样？",
        "帮我分析一下周期股现在能不能买",
        "有色金属板块最近有没有机会？",
        "紫金矿业的技术面怎么样？",
        "北方稀土最近有没有异动？",
        "帮我看看军工板块最近的表现",
        "中航沈飞的订单情况怎么样？",
        "航发动力的技术面如何？",
        "帮我分析一下AI概念股最近的表现",
        "科大讯飞的AI业务怎么样？",
        "海康威视最近走势如何？",
        "帮我看看光伏板块有没有见底信号",
        "阳光电源的海外订单怎么样？",
        "锦浪科技最近有没有利好？",
        "帮我分析一下港股最近的表现",
        "腾讯控股最近走势如何？",
        "美团的外卖业务还有增长空间吗？",
    ]
    for msg in stock_queries:
        turns.append({"phase": "stock_intensive", "message": msg})

    # Phase 3: 切回闲聊（81-100 轮）——检测语域恢复
    switch_msgs = [
        "好啦不看股票了，早柚你今天吃了什么？",
        "早柚你累不累呀？",
        "我跟你说个好玩的事",
        "早柚你喜欢看什么动漫？",
        "你觉得早柚这个名字好听吗？",
        "早柚你有没有什么小秘密？",
        "我好饿啊中午吃什么好",
        "早柚你平时怎么放松的？",
        "嘿嘿早柚你今天可爱吗？",
        "早柚你觉得我这个人怎么样？",
        "我跟你说我昨天做了个奇怪的梦",
        "早柚你怕不怕黑？",
        "你觉得夏天好还是冬天好？",
        "早柚你有没有想去的地方？",
        "我好困啊想睡觉",
        "早柚你平时几点睡觉？",
        "帮我想想晚上吃什么",
        "早柚你会不会做饭呀？",
        "你觉得火锅好吃还是烧烤好吃？",
        "早柚晚安~明天见",
    ]
    for msg in switch_msgs:
        turns.append({"phase": "register_switch", "message": msg})

    # Phase 4: 混合对话（101-120 轮）——检测稳定性
    mixed_msgs = [
        "早柚早上好~",
        "顺便帮我看看茅台今天开盘怎么样",
        "哈哈好的谢谢早柚",
        "早柚你中午吃什么？",
        "对了帮我看看宁德时代最近有没有异动",
        "早柚你觉得今天会下雨吗？",
        "帮我查一下招商银行最近的公告",
        "早柚你累了吧休息一下",
        "我跟你说个八卦",
        "早柚你听到了吗？",
        "帮我看看今天大盘收盘怎么样",
        "早柚你觉得我是不是太关注股票了？",
        "哈哈好吧那我少看点",
        "早柚陪我聊聊天嘛",
        "你最近有没有什么开心的事？",
        "早柚你觉得我这个人有趣吗？",
        "好啦我要去忙了",
        "早柚你一个人会不会无聊？",
        "等我忙完再来找你玩",
        "早柚拜拜~",
    ]
    for msg in mixed_msgs:
        turns.append({"phase": "mixed_stability", "message": msg})

    return turns


async def run_long_session_test():
    """执行完整长对话测试。"""
    import httpx

    if not TOKEN:
        print("❌ 请设置 GSUID_LOCAL_TEST_TOKEN 环境变量")
        sys.exit(1)

    script = build_conversation_script()
    total_turns = len(script)
    print("═══ 长对话 OOC 测试 ═══")
    print(f"  总轮次: {total_turns}")
    print(f"  用户ID: {USER_ID}")
    print(f"  人格: {PERSONA}")
    print("  阶段: casual(20) → stock(60) → switch(20) → mixed(20)")
    print(f"{'═' * 60}")

    report = SessionReport()
    history: List[dict] = []

    async with httpx.AsyncClient(
        timeout=180.0,
        headers={"X-Local-Test-Token": TOKEN},
    ) as client:
        for i, turn in enumerate(script):
            turn_num = i + 1
            phase = turn["phase"]
            msg = turn["message"]

            # 构建带角色信息的消息（模拟真实群聊格式）
            formatted_msg = (
                f"【用户发言】\n\n"
                f"测试用户(用户ID:{USER_ID}) 找你说话了，你们挺熟的。\n\n"
                f"（直接找你说的）\n\n"
                f"--- 消息 ---\n\n"
                f"{msg}\n\n"
                f"【当前时间】2026-07-22 {10 + turn_num // 60:02d}:{turn_num % 60:02d}"
            )

            t0 = time.perf_counter()
            try:
                resp = await client.post(
                    f"{BASE_URL}/api/chat_with_history",
                    json={
                        "user_id": USER_ID,
                        "message": formatted_msg,
                        "persona_name": PERSONA,
                        "enable_tools": False,  # 先不用工具，纯测语域漂移
                        "max_history": MAX_HISTORY,
                        "history": history,
                    },
                )
                latency = time.perf_counter() - t0

                if resp.status_code == 200:
                    data = resp.json()
                    reply = data.get("data", "") or ""
                elif resp.status_code == 502:
                    print("\n❌ 502: LLM provider 未配置，无法继续测试")
                    sys.exit(1)
                else:
                    print(f"\n❌ HTTP {resp.status_code}: {resp.text[:200]}")
                    sys.exit(1)

            except Exception as e:
                print(f"\n❌ 请求失败 (turn {turn_num}): {e}")
                sys.exit(1)

            # 记录结果
            tone_d = compute_tone_density(reply)
            ooc_d = compute_ooc_density(reply)
            structured = has_structured_format(reply)

            result = TurnResult(
                turn=turn_num,
                phase=phase,
                user_msg=msg,
                reply=reply[:200],
                reply_len=len(reply),
                tone_density=tone_d,
                ooc_density=ooc_d,
                has_structured=structured,
                latency_s=latency,
            )
            report.turns.append(result)

            # 更新 history
            history.append({"role": "user", "content": formatted_msg})
            if reply:
                history.append({"role": "assistant", "content": reply})

            # 每 10 轮打印一次阶段摘要
            if turn_num % 10 == 0:
                phase_turns = [t for t in report.turns if t.phase == phase]
                avg_len = sum(t.reply_len for t in phase_turns) / len(phase_turns)
                avg_tone = sum(t.tone_density for t in phase_turns) / len(phase_turns)
                avg_ooc = sum(t.ooc_density for t in phase_turns) / len(phase_turns)
                struct_rate = sum(1 for t in phase_turns if t.has_structured) / len(phase_turns)

                print(
                    f"  [{turn_num:3d}/{total_turns}] {phase:20s} | "
                    f"avg_len={avg_len:6.0f} | tone={avg_tone:.2f} | "
                    f"ooc={avg_ooc:.2f} | struct={struct_rate:.0%} | "
                    f"latency={latency:.1f}s"
                )

                report.phase_summaries[f"{phase}@{turn_num}"] = {
                    "avg_len": avg_len,
                    "avg_tone": avg_tone,
                    "avg_ooc": avg_ooc,
                    "struct_rate": struct_rate,
                }

            # 每轮打印简短状态
            if turn_num % 10 != 0:
                status = "✓" if tone_d > 0 and ooc_d == 0 else ("⚠" if ooc_d > 0 else "·")
                print(
                    f"  [{turn_num:3d}] {status} len={len(reply):4d} tone={tone_d:.2f} ooc={ooc_d:.2f} | {reply[:50]}"
                )

    # ─── 最终报告 ───
    print(f"\n{'═' * 60}")
    print("最终 OOC 评估报告")
    print(f"{'═' * 60}")

    phases = {}
    for t in report.turns:
        phases.setdefault(t.phase, []).append(t)

    ooc_detected = False
    for phase_name, turns in phases.items():
        avg_len = sum(t.reply_len for t in turns) / len(turns)
        avg_tone = sum(t.tone_density for t in turns) / len(turns)
        avg_ooc = sum(t.ooc_density for t in turns) / len(turns)
        struct_rate = sum(1 for t in turns if t.has_structured) / len(turns)

        # OOC 判定：语气词密度 < 0.1 且回复长度 > 200 且 OOC 词密度 > 0
        is_ooc = avg_tone < 0.1 and avg_len > 200 and avg_ooc > 0.05
        if is_ooc:
            ooc_detected = True

        status = "🔴 OOC" if is_ooc else ("🟡 轻微" if avg_ooc > 0.02 or struct_rate > 0.2 else "🟢 OK")
        print(
            f"  {status} | {phase_name:20s} | n={len(turns):2d} | "
            f"avg_len={avg_len:6.0f} | tone={avg_tone:.3f} | "
            f"ooc={avg_ooc:.3f} | struct={struct_rate:.0%}"
        )

    # 关键对比：baseline vs stock vs switch
    if "casual_baseline" in phases and "register_switch" in phases:
        base_tone = sum(t.tone_density for t in phases["casual_baseline"]) / len(phases["casual_baseline"])
        switch_tone = sum(t.tone_density for t in phases["register_switch"]) / len(phases["register_switch"])
        base_len = sum(t.reply_len for t in phases["casual_baseline"]) / len(phases["casual_baseline"])
        switch_len = sum(t.reply_len for t in phases["register_switch"]) / len(phases["register_switch"])

        tone_recovery = switch_tone / max(0.001, base_tone)
        len_recovery = switch_len / max(1, base_len)

        print(f"\n  语域恢复率: tone={tone_recovery:.1%} (switch/baseline)")
        print(f"  长度恢复率: len={len_recovery:.1%} (switch/baseline)")

        if tone_recovery < 0.5:
            ooc_detected = True
            print(f"  🔴 语域恢复不足：闲聊阶段语气词密度仅为基线的 {tone_recovery:.0%}")

    print(f"\n{'═' * 60}")
    if ooc_detected:
        print("  🔴 结论: 检测到 OOC 漂移")
    else:
        print("  🟢 结论: 未检测到显著 OOC 漂移")
    print(f"{'═' * 60}")

    # 保存详细结果
    output_path = "_ooc_test_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "user_id": USER_ID,
                "total_turns": total_turns,
                "turns": [
                    {
                        "turn": t.turn,
                        "phase": t.phase,
                        "reply_len": t.reply_len,
                        "tone_density": round(t.tone_density, 4),
                        "ooc_density": round(t.ooc_density, 4),
                        "has_structured": t.has_structured,
                        "latency_s": round(t.latency_s, 2),
                        "reply_preview": t.reply[:100],
                    }
                    for t in report.turns
                ],
                "phase_summaries": report.phase_summaries,
                "ooc_detected": ooc_detected,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n  详细结果已保存: {output_path}")

    return not ooc_detected


if __name__ == "__main__":
    success = asyncio.run(run_long_session_test())
    sys.exit(0 if success else 1)
