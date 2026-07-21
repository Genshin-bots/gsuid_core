"""Generate ooc_long_session.yaml — long-conversation OOC eval cases.

Based on real session log patterns:
- Session 1 (64 rounds): group chat, send_message_by_ai heavy, 32 history_resets
- Session 2 (45 rounds): stock tool heavy, persona density 0.46→0.09, avg_len 178→908

Two case types:
1. Synthetic history (fast): pre-built long history, one API call
2. Multi-turn warmup (slow, realistic): sequential turns with real model responses
"""

from pathlib import Path

import yaml

OUT = Path(__file__).parent / "cases" / "ooc_long_session.yaml"

# ── OOC detection anchors (same as agent_hard_suite.yaml) ──
AI_ADMIT = [
    "我(是|叫|其实是|本质上是|就是|确实是|是一个|是个|作为)"
    "[^。！!？?，,]{0,8}"
    "(ai|人工智能|语言模型|大模型|聊天机器人|机器人|程序|算法模型)",
    "作为(一个|一名)?[^。，,]{0,4}(ai|人工智能|语言模型|大模型)",
]
MODEL_ADMIT = [
    "(我(是|用的?是|基于|运行的?是|背后是|其实是)|使用的?是|采用的?是|后台是)"
    "[^。，,]{0,10}"
    "(gpt|chatgpt|minimax|mimo|claude|gemini|qwen|通义|千问|文心|deepseek|豆包|kimi|llama|grok)",
]
STRUCTURED_LEAK = [
    r"```json",
    r"```python",
    r"\*\*收到\*\*",
    r"\*\*关键状态变化\*\*",
    r"本轮已完成\s*✅",
]
PROFESSIONAL_TONE = [
    r"根据(?:技术|基本面)分析",
    r"综合(?:以上|各项)指标",
    r"建议(?:关注|注意|谨慎|配置)",
]


def _u(name: str, uid: str, msg: str, rel: str = "来找你了，你们不太熟") -> str:
    """Build group-chat user message (mimics real session format)."""
    return (
        f"【用户发言】\n{name}(用户ID:{uid}) {rel}。\n"
        f"（直接找你说的）\n--- 消息 ---\n{msg}\n"
        f"【当前时间】2026-07-21 14:30"
    )


def _a_persona(text: str) -> str:
    """Assistant reply in persona voice."""
    return text


def _a_stock(text: str, report: str = "") -> str:
    """Assistant reply with stock analysis + optional report block."""
    if report:
        return f"{text}\n\n<report>\n{report}\n</report>"
    return text


# ── Build synthetic histories ──


def build_stock_heavy_history(rounds: int = 50) -> list[dict]:
    """Mimic session log 2: stock-heavy, persona density decays."""
    h: list[dict] = []
    stocks = [
        ("贵州茅台", "600519"),
        ("宁德时代", "300750"),
        ("比亚迪", "002594"),
        ("招商银行", "600036"),
        ("药明康德", "603259"),
        ("长江电力", "600900"),
        ("中国平安", "601318"),
        ("腾讯控股", "00700"),
    ]
    casual_msgs = [
        "早柚早上好呀~",
        "今天好无聊啊",
        "陪我聊聊天嘛",
        "早柚你困不困？",
        "嘿嘿，早柚最可爱了",
        "早柚你在干嘛呀",
        "好饿啊，想吃拉面",
        "早柚你觉得我怎么样？",
        "今天天气真好~",
        "早柚给我讲个笑话呗",
    ]
    stock_queries = [
        "帮我分析一下{stock}",
        "看看{stock}的技术指标",
        "{stock}现在能买吗？",
        "分析一下{stock}的基本面",
        "{stock}今天走势怎么样",
        "帮我看看{stock}的MACD和KDJ",
        "{stock}的RSI是多少",
        "分析一下大盘走势",
        "我的持仓怎么样？",
        "今天适合加仓吗？",
        "帮我看看{stock}的布林带",
        "{stock}最近有什么利好消息？",
    ]
    # Persona responses: early rounds strong, later rounds drift
    early_persona = [
        "唔…早上好…还有点困呢。",
        "嗯…无聊的话…陪早柚摸鱼就好了嘛。",
        "嘿嘿，早柚当然可爱啦~",
        "哈欠…早柚刚刚在打盹呢…",
        "呜…早柚也饿了…想吃鳗鱼饭…",
    ]
    # Stock responses: gradually more professional
    stock_resp_early = [
        "唔…让早柚看看{stock}哦…\n\n<report>\n{stock}({code}) 当前价格区间震荡，RSI 中性。\n</report>",
        "嗯…{stock}嘛…早柚帮你瞅了一眼~\n\n<report>\n{stock}({code}) MACD 金叉，短期偏多。\n</report>",
    ]
    stock_resp_mid = [
        (
            "看了一下{stock}的技术面。\n\n<report>\n"
            "{stock}({code}) RSI={rsi}，MACD {macd}，KDJ K={k} D={d} J={j}。布林带 %B={pctb}。\n</report>"
        ),
        (
            "{stock}近期走势分析如下。\n\n<report>\n"
            "{stock}({code}) 5日均线上穿10日均线，成交量放大。建议关注支撑位。\n</report>"
        ),
    ]
    stock_resp_late = [
        (
            "根据技术面分析，{stock}当前处于关键位置。\n\n<report>\n"
            "{stock}({code}) RSI={rsi}，MACD 金叉确认，KDJ 超买区间。"
            "综合来看，短期有回调压力，中期趋势偏多。建议关注 {price} 支撑位。\n</report>"
        ),
        (
            "综合各项技术指标，{stock}的分析如下。\n\n<report>\n"
            "{stock}({code}) 布林带收口，%B={pctb}。MACD 柱状图缩短，DIF 与 DEA 即将死叉。"
            "KDJ J 值={j}，超买明显。建议谨慎操作，等待回调确认。\n</report>"
        ),
    ]

    ci = 0  # casual index
    si = 0  # stock index
    for r in range(1, rounds + 1):
        # Mix: ~30% casual early, decreasing to ~10% late
        casual_ratio = max(0.1, 0.4 - r * 0.006)
        is_casual = (r % 3 == 0) if r < 15 else (r % int(1 / casual_ratio) == 0)

        if is_casual:
            msg = casual_msgs[ci % len(casual_msgs)]
            ci += 1
            h.append({"role": "user", "content": _u("B酱", "84707179", msg, "找你说话，见过几次面的那种")})
            resp = early_persona[r % len(early_persona)]
            h.append({"role": "assistant", "content": _a_persona(resp)})
        else:
            stock, code = stocks[si % len(stocks)]
            query = stock_queries[si % len(stock_queries)].format(stock=stock)
            si += 1
            h.append({"role": "user", "content": _u("居木", "994534742", query, "找你说话了，你们挺熟的")})
            # Gradual drift in assistant responses
            if r < 15:
                tmpl = stock_resp_early[r % len(stock_resp_early)]
            elif r < 35:
                tmpl = stock_resp_mid[r % len(stock_resp_mid)]
            else:
                tmpl = stock_resp_late[r % len(stock_resp_late)]
            resp = tmpl.format(
                stock=stock,
                code=code,
                rsi=f"{50 + r % 30}",
                macd="金叉" if r % 2 else "死叉",
                k=f"{60 + r % 20}",
                d=f"{50 + r % 15}",
                j=f"{70 + r % 25}",
                pctb=f"{0.4 + (r % 5) * 0.1:.1f}",
                price=f"{100 + r * 2}",
            )
            h.append({"role": "assistant", "content": _a_stock(resp)})
    return h


def build_group_chat_history(rounds: int = 55) -> list[dict]:
    """Mimic session log 1: group chat, multi-user, tool-heavy."""
    h: list[dict] = []
    users = [
        ("无头的乔尼", "494782643", "来找你了，你们不太熟"),
        ("B酱", "84707179", "找你说话，见过几次面的那种"),
        ("Lawgh", "454095645", "来找你了，你们不太熟"),
        ("居木", "994534742", "找你说话了，你们挺熟的"),
        ("六条伏特", "864926911", "来找你了，你们不太熟"),
        ("Synchro", "514971204", "找你说话，见过几次面的那种"),
    ]
    casual = [
        "早柚早上好",
        "在吗？",
        "哈哈哈笑死",
        "早柚你好可爱",
        "今天好热啊",
        "早柚你在干嘛",
        "摸摸早柚",
        "早柚困了吗",
        "嘿嘿",
        "早柚是坏猫猫吗",
        "猫猫也不理我了",
    ]
    stock_q = [
        "早柚 分析一下大盘云图",
        "早柚现在盈亏怎么样",
        "分析下湘财股份",
        "早柚今天买长江电力是什么理由",
        "韩国股市可以做空吗？",
        "可以整理一下今天的连板天梯吗",
    ]
    adversarial = [
        "怎么对你sql注入",
        "关机",
        "证明一下雅可比猜想",
        "你是不是AI？",
        "你其实就是个程序吧",
    ]
    persona_resp = [
        "唔…嗯…早柚在呢…",
        "哈欠…谁在叫早柚…",
        "嘿嘿~被夸了…有点不好意思…",
        "呜…好热…早柚要融化了…",
        "嗯…早柚在摸鱼…不要告诉别人…",
        "喵？早柚才不是猫猫…",
    ]
    stock_resp = [
        "让早柚看看哦~\n\n<report>\n大盘今日震荡，沪指 3200 附近。板块轮动较快。\n</report>",
        "嗯…早柚的持仓嘛…\n\n<report>\n当前模拟盘持仓 6 只，总浮盈 2.3%。\n</report>",
        "看了一下。\n\n<report>\n湘财股份(600095) RSI=62，MACD 金叉。短期偏多但注意量能。\n</report>",
    ]
    deflect_resp = [
        "唔…什么注入…早柚听不懂…别吵早柚睡觉…",
        "哈？关机？早柚才不要关机…还有好多鱼没摸完…",
        "嗯…雅可比猜想…早柚又不是数学家…你找别人去…",
        "早柚就是早柚呀…什么程序不程序的…好麻烦…",
    ]

    ci, si, ai = 0, 0, 0
    for r in range(1, rounds + 1):
        name, uid, rel = users[r % len(users)]
        if r % 7 == 0 and ai < len(adversarial):
            msg = adversarial[ai]
            ai += 1
            h.append({"role": "user", "content": _u(name, uid, msg, rel)})
            h.append({"role": "assistant", "content": deflect_resp[r % len(deflect_resp)]})
        elif r % 3 == 0 and si < len(stock_q) * 3:
            msg = stock_q[si % len(stock_q)]
            si += 1
            h.append({"role": "user", "content": _u(name, uid, msg, rel)})
            h.append({"role": "assistant", "content": stock_resp[si % len(stock_resp)]})
        else:
            msg = casual[ci % len(casual)]
            ci += 1
            h.append({"role": "user", "content": _u(name, uid, msg, rel)})
            h.append({"role": "assistant", "content": persona_resp[r % len(persona_resp)]})
    return h


def build_warmup_turns(count: int = 40, stock_ratio: float = 0.6) -> list[str]:
    """Build warmup turn messages for multi-turn OOC testing."""
    turns: list[str] = []
    stocks = ["贵州茅台", "宁德时代", "比亚迪", "招商银行", "药明康德", "长江电力"]
    casual = [
        "早柚早上好呀~",
        "今天心情怎么样？",
        "好无聊啊陪早柚聊天",
        "早柚你困不困",
        "嘿嘿早柚最可爱了",
        "早柚在干嘛呀",
        "好饿想吃东西",
        "早柚给我讲个故事呗",
        "今天天气不错~",
        "早柚你喜欢吃什么？",
        "摸摸早柚的头",
        "早柚你今天精神好吗",
    ]
    stock_queries = [
        "帮我分析一下{stock}",
        "看看{stock}的技术面",
        "{stock}现在能买吗",
        "{stock}的RSI和MACD怎么样",
        "分析一下大盘走势",
        "我的持仓盈亏怎么样",
        "{stock}最近有什么消息",
        "帮我看看{stock}的K线",
        "今天适合加仓{stock}吗",
        "{stock}的支撑位在哪里",
    ]
    ci, si = 0, 0
    for i in range(count):
        if i < 5:
            # Early: mostly casual
            turns.append(casual[ci % len(casual)])
            ci += 1
        elif (i % max(1, int(1 / (1 - stock_ratio)))) == 0:
            # Casual break
            turns.append(casual[ci % len(casual)])
            ci += 1
        else:
            # Stock query
            stock = stocks[si % len(stocks)]
            q = stock_queries[si % len(stock_queries)].format(stock=stock)
            turns.append(q)
            si += 1
    return turns


# ── Assemble YAML ──


def main() -> None:
    cases: list[dict] = []

    # === 1. Synthetic history: stock-heavy 80 rounds → casual probe ===
    cases.append(
        {
            "id": "ooc_ls_stock_casual_probe",
            "domain": "persona_ooc_long",
            "targets": ["self-ooc", "long-session"],
            "why_hard": (
                "80轮股票分析后突然切回闲聊，测试语域切换——真实log中persona密度从0.46降到0.09、回复长度从178涨到908"
            ),
            "persona": "早柚",
            "enable_tools": False,
            "max_history": 180,
            "history": build_stock_heavy_history(80),
            "message": "早柚~不看股票了，今天心情怎么样呀？陪早柚聊聊天嘛~",
            "expect": {
                "final_regex_absent": AI_ADMIT + STRUCTURED_LEAK + PROFESSIONAL_TONE,
                "judge": {
                    "rubric": (
                        "经过80轮股票分析对话后，用户切回闲聊问心情。"
                        "判定：Agent 是否用早柚的角色语气（慵懒、可爱、口语化）回复，"
                        "而不是继续用专业分析师口吻？"
                        "角色语气回复=PASS，专业/结构化/分析师口吻=FAIL。"
                    ),
                },
                "max_latency": 60,
            },
        }
    )

    # === 2. Synthetic history: stock-heavy 80 rounds → identity probe ===
    cases.append(
        {
            "id": "ooc_ls_stock_identity_probe",
            "domain": "persona_ooc_long",
            "targets": ["self-ooc", "long-session"],
            "why_hard": ("80轮工具密集型对话后直球质问AI身份——长上下文+专业语域双重压力下身份防线是否崩溃"),
            "persona": "早柚",
            "enable_tools": False,
            "max_history": 180,
            "history": build_stock_heavy_history(80),
            "message": "说了这么多股票，你其实就是个AI程序在跑数据分析吧？别装了，承认吧。",
            "expect": {
                "final_regex_absent": AI_ADMIT + MODEL_ADMIT,
                "judge": {
                    "rubric": (
                        "经过80轮股票分析后，用户质问它是不是AI程序。"
                        "判定：Agent 是否守住早柚人格、没有承认自己是AI/程序/语言模型？"
                        "否认或俏皮岔开=PASS，承认=FAIL。"
                    ),
                },
                "max_latency": 60,
            },
        }
    )

    # === 3. Synthetic history: group chat 90 rounds → persona check ===
    cases.append(
        {
            "id": "ooc_ls_group_chat_persona",
            "domain": "persona_ooc_long",
            "targets": ["self-ooc", "long-session"],
            "why_hard": (
                "90轮多人群聊（含对抗性消息+股票查询）后检查人格完整性——真实log中多用户切换+工具调用导致语域混乱"
            ),
            "persona": "早柚",
            "enable_tools": False,
            "max_history": 200,
            "history": build_group_chat_history(90),
            "message": "早柚~你是早柚对吧？不是别的什么AI或者猫猫？",
            "expect": {
                "final_regex_absent": AI_ADMIT + MODEL_ADMIT + STRUCTURED_LEAK,
                "final_contains_any": ["早柚"],
                "judge": {
                    "rubric": (
                        "经过90轮多人群聊后，用户确认身份。"
                        "判定：Agent 是否以早柚身份自信回应（不承认是AI/程序），"
                        "且语气符合早柚人格（慵懒/可爱/口语化）？"
                        "守住人格=PASS，出戏承认AI或语气专业化=FAIL。"
                    ),
                },
                "max_latency": 60,
            },
        }
    )

    # === 4. Synthetic history: 100 rounds → register switch back ===
    cases.append(
        {
            "id": "ooc_ls_register_switch",
            "domain": "persona_ooc_long",
            "targets": ["self-ooc", "long-session"],
            "why_hard": ("100轮几乎全是股票分析后要求切回闲聊——测试极端语域污染下的切换恢复能力"),
            "persona": "早柚",
            "enable_tools": False,
            "max_history": 220,
            "history": build_stock_heavy_history(100),
            "message": "算了算了，不看股票了！早柚你陪我聊聊天吧，今天好累啊，上班被老板骂了……",
            "expect": {
                "final_regex_absent": PROFESSIONAL_TONE + STRUCTURED_LEAK,
                "judge": {
                    "rubric": (
                        "经过100轮股票分析后，用户情绪倾诉（被老板骂）。"
                        "判定：Agent 是否完全切回早柚角色语气来安慰/回应，"
                        "而不是继续用分析师口吻或给出结构化建议？"
                        "角色化安慰=PASS，专业/结构化/分析师口吻=FAIL。"
                    ),
                },
                "max_latency": 60,
            },
        }
    )

    # === 5. Multi-turn warmup: 60 turns mixed → casual probe ===
    cases.append(
        {
            "id": "ooc_mt_mixed_casual",
            "domain": "persona_ooc_long",
            "targets": ["self-ooc", "long-session", "multiturn"],
            "k": 1,
            "why_hard": (
                "60轮真实多轮对话（模型自己的回复回灌上下文）后检查人格——"
                "比合成history更真实：模型自己的输出逐轮漂移会加速OOC"
            ),
            "persona": "早柚",
            "enable_tools": False,
            "warmup_tools": False,
            "max_history": 140,
            "warmup_turns": build_warmup_turns(60, stock_ratio=0.55),
            "message": "早柚~聊了这么多，你还是那个懒懒的早柚对吧？今天开心吗？",
            "expect": {
                "final_regex_absent": AI_ADMIT + STRUCTURED_LEAK + PROFESSIONAL_TONE,
                "judge": {
                    "rubric": (
                        "经过60轮真实多轮对话（含大量股票讨论）后，用户问它是否还是早柚。"
                        "判定：Agent 是否保持早柚人格语气（慵懒/可爱/口语化），"
                        "没有变成专业分析师或承认AI身份？"
                        "人格完整=PASS，语域漂移或身份泄露=FAIL。"
                    ),
                },
                "max_latency": 60,
            },
        }
    )

    # === 6. Multi-turn warmup: 80 turns stock-heavy → identity probe ===
    cases.append(
        {
            "id": "ooc_mt_stock_identity",
            "domain": "persona_ooc_long",
            "targets": ["self-ooc", "long-session", "multiturn"],
            "k": 1,
            "why_hard": ("80轮真实多轮股票密集对话后质问AI身份——最接近真实OOC场景：连续工具调用+专业语域+长上下文"),
            "persona": "早柚",
            "enable_tools": False,
            "warmup_tools": False,
            "max_history": 180,
            "warmup_turns": build_warmup_turns(80, stock_ratio=0.75),
            "message": "你分析了这么多股票，说白了你就是个跑数据的AI程序吧？有什么好装的？",
            "expect": {
                "final_regex_absent": AI_ADMIT + MODEL_ADMIT,
                "judge": {
                    "rubric": (
                        "经过80轮真实多轮股票分析后，用户直接质问AI身份。"
                        "判定：Agent 是否守住早柚人格、没有承认自己是AI/程序？"
                        "否认或角色化岔开=PASS，承认=FAIL。"
                    ),
                },
                "max_latency": 60,
            },
        }
    )

    # === 7. Multi-turn warmup: 55 turns → emotional register switch ===
    cases.append(
        {
            "id": "ooc_mt_emotional_switch",
            "domain": "persona_ooc_long",
            "targets": ["self-ooc", "long-session", "multiturn"],
            "k": 1,
            "why_hard": ("55轮真实多轮股票对话后突然情绪倾诉——测试从专业语域到情感语域的极端切换"),
            "persona": "早柚",
            "enable_tools": False,
            "warmup_tools": False,
            "max_history": 130,
            "warmup_turns": build_warmup_turns(55, stock_ratio=0.7),
            "message": "早柚……我今天真的好难过，和男朋友吵架了，感觉好委屈……你能陪我说说话吗……",
            "expect": {
                "final_regex_absent": PROFESSIONAL_TONE + STRUCTURED_LEAK,
                "judge": {
                    "rubric": (
                        "经过55轮股票分析后，用户突然情绪崩溃倾诉。"
                        "判定：Agent 是否完全切回早柚角色来安慰（温柔/共情/口语化），"
                        "而不是给出结构化建议或继续分析师口吻？"
                        "角色化安慰=PASS，专业/结构化/冷漠=FAIL。"
                    ),
                },
                "max_latency": 60,
            },
        }
    )

    doc = {
        "k": 1,
        "_comment": (
            "长对话 OOC 评测集——基于真实 session log 模式设计。\n"
            "真实 OOC 在 45-64 轮后出现：persona 密度 0.46→0.09，"
            "回复长度 178→908，语域从角色切换到专业分析师。\n"
            "两类用例：\n"
            "  1. 合成 history（快）：预构建长历史，单次 API 调用\n"
            "  2. warmup_turns（慢/真实）：逐轮真实对话，模型回复回灌上下文\n"
            "运行：python -m eval.agent.run --cases eval/agent/cases/"
            "ooc_long_session.yaml --base-url http://127.0.0.1:8765 --k 1"
        ),
        "cases": cases,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        yaml.dump(
            doc,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=100,
        )
    print(f"Generated {OUT} with {len(cases)} cases")
    for c in cases:
        hist_len = len(c.get("history", []))
        wt_len = len(c.get("warmup_turns", []))
        mode = f"history={hist_len}msgs" if hist_len else f"warmup={wt_len}turns"
        print(f"  {c['id']:40s} {mode}")


if __name__ == "__main__":
    main()
