import re
import sys
import random
import asyncio
import logging
from typing import Any, Dict, Optional
from concurrent.futures import ThreadPoolExecutor

from joblib import dump, load
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import Pipeline, FeatureUnion
from sklearn.linear_model import LogisticRegression
from sklearn.feature_extraction.text import TfidfVectorizer

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path

AI_PATH = get_res_path("ai_core")

MODEL_PATH = AI_PATH / "intent_classifier_v5.1.joblib"

# ==========================================
# 0. 环境静默设置 (Jieba)
# ==========================================
jieba_logger = logging.getLogger("jieba")
jieba_logger.setLevel(logging.CRITICAL)
jieba_logger.propagate = False

_old_stdout = sys.stdout
_old_stderr = sys.stderr


class DevNull:
    def write(self, msg):
        pass

    def flush(self):
        pass


sys.stdout = DevNull()
sys.stderr = DevNull()

import jieba  # noqa: E402
import jieba.posseg as pseg  # noqa: E402

sys.stdout = _old_stdout
sys.stderr = _old_stderr

# ==========================================
# 1. 扩充词典定义
# ==========================================

# [工具触发] 动作：查询、查看
CHECK_VERBS = {
    "查",
    "看",
    "找",
    "搜",
    "查询",
    "搜索",
    "分析",
    "显示",
    "列举",
    "检测",
    "检查",
    "获取",
    "读",
    "读取",
    "调取",
    "调用",
    "来",
    "整",
    "搞",
}

# [工具触发] 动作：生成、创作
GENERATE_VERBS = {
    "生成",
    "画",
    "做",
    "写",
    "创作",
    "绘制",
    "合成",
    "制作",
    "捏",
    "产出",
    "弄",
    "来个",
    "来一张",
    "来一段",
    "来一首",
    "来一个",
    "来点",
}

# [工具触发] 动作：修改、编辑
EDIT_VERBS = {
    "修改",
    "编辑",
    "改",
    "加",
    "添加",
    "去",
    "去掉",
    "消除",
    "删除",
    "P",
    "P一下",
    "换",
    "替换",
    "增加",
    "附上",
}

# [工具触发] 对象：功能、数据、面板
FUNCTIONAL_NOUNS = {
    "面板",
    "数据",
    "属性",
    "排行",
    "排行榜",
    "榜单",
    "记录",
    "战绩",
    "股价",
    "走势",
    "行情",
    "价格",
    "汇率",
    "大盘",
    "金价",
    "油价",
    "气温",
    "天气",
    "配置",
    "装备",
    "圣遗物",
    "评分",
    "练度",
    "详情",
    "状态",
    "数值",
    "倍率",
    "概率",
    "掉落",
    "成本",
    "收益",
    "库存",
    "余额",
    "声骸",
    "面板",
    "数据",
    "属性",
    "排行",
    "排行榜",
    "倍率",
    "伤害",
    "乘区",
    "数值",
}

# [工具触发] 对象：媒体（图、音、视）
MEDIA_NOUNS = {
    "图",
    "图片",
    "照片",
    "壁纸",
    "插画",
    "头像",
    "表情",
    "表情包",
    "视频",
    "录像",
    "动画",
    "短视频",
    "片子",
    "语音",
    "声音",
    "音频",
    "音乐",
    "歌",
    "歌曲",
    "BGM",
    "伴奏",
}

# [工具触发] 对象：具体元素（用于修图等）
EDIT_OBJ_NOUNS = {"字", "文字", "水印", "背景", "特效", "滤镜", "马赛克", "字幕"}

# [问答触发] 知识类名词
KNOWLEDGE_NOUNS = {
    "机制",
    "剧情",
    "配队",
    "队伍",
    "武器",
    "背景",
    "故事",
    "介绍",
    "弱点",
    "位置",
    "材料",
    "配方",
    "打法",
    "出处",
    "世界观",
    "天赋",
    "命座",
    "技能",
    "成就",
    "任务",
    "彩蛋",
    "设定",
    "攻略",
    "评价",
    "强度",
    "身高",
    "生日",
    "CV",
    "声优",
    "血量",
    "机制",
    "剧情",
    "配队",
    "队伍",
    "武器",
    "天赋",
    "命座",
    "技能",
    "大招",
    "战技",
    "普攻",
    "Q",
    "E",
    "A",
    "q",
    "e",
    "a",
    "重击",
    "下落攻击",
    "暴击",
    "爆伤",
    "暴击伤害",
    "暴击率",
    "精通",
    "元素精通",
    "充能",
    "元素充能",
    "攻击",
    "攻击力",
    "防御",
    "防御力",
    "生命",
    "生命值",
    "基础攻击",
    "法器",
    "单手剑",
    "双手剑",
    "长柄武器",
    "弓箭",
}

# [闲聊/通用] 否定词
NEGATION_WORDS = {"不", "没", "无", "非", "莫", "别", "不要", "不用", "休想", "禁止", "别去"}

# [闲聊] 状态/情绪词 (重点扩充，用于抵消工具名词的权重)
STATE_WORDS = {
    "麻",
    "麻了",
    "亏",
    "亏麻",
    "亏死",
    "救命",
    "卧槽",
    "牛逼",
    "笑死",
    "无语",
    "666",
    "丑",
    "太丑",
    "真丑",
    "难看",
    "好丑",
    "垃圾",
    "坑",
    "药丸",
    "崩",
    "崩了",
    "难",
    "好难",
    "强",
    "弱",
    "离谱",
    "恶心",
    "卡",
    "慢",
    "贵",
    "便宜",
    "好",
    "坏",
    "高",
    "低",
    "烂",
    "好烂",
    "拉胯",
    "寄",
    "晦气",
    "谢谢",
    "thanks",
    "ok",
    "懂",
    "明白",
    "晕",
    "懵",
    "喜欢",
    "爱",
    "讨厌",
    "烦",
    "一般",
    "行",
    "不行",
    "鬼",
}

# [问答] 疑问词
QUERY_WORDS = {
    "怎么",
    "多少",
    "什么",
    "谁",
    "哪里",
    "几",
    "吗",
    "呢",
    "啥",
    "咋",
    "如何",
    "为什么",
    "是啥",
    "在哪",
    "几点",
    "多久",
}

# [闲聊] 自身相关/无意义
CHAT_ENTITIES = {"你", "我", "他", "她", "它", "咱们", "大家", "你好", "在吗", "早安", "晚安", "抱抱"}

# [闲聊] 观点类（如果是问“你”的看法，是闲聊）
OPINION_WORDS = {"看法", "觉得", "认为", "评价", "观点", "想"}


def init_jieba():
    # 批量注册词典
    words_map = [
        (CHECK_VERBS, "v_check"),
        (GENERATE_VERBS, "v_gen"),
        (EDIT_VERBS, "v_edit"),
        (FUNCTIONAL_NOUNS, "n_func"),
        (MEDIA_NOUNS, "n_media"),
        (EDIT_OBJ_NOUNS, "n_edit_obj"),
        (KNOWLEDGE_NOUNS, "n_know"),
        (NEGATION_WORDS, "d_neg"),
        (STATE_WORDS, "a_state"),
        (QUERY_WORDS, "r_query"),
        (CHAT_ENTITIES, "r_chat"),
        (OPINION_WORDS, "v_opinion"),
    ]
    for word_set, tag in words_map:
        for w in word_set:
            jieba.add_word(w, tag=tag)


init_jieba()


# ==========================================
# 2. 特征工程与数据处理
# ==========================================


class ItemSelector(BaseEstimator, TransformerMixin):
    def __init__(self, key):
        self.key = key

    def fit(self, x, y=None):
        return self

    def transform(self, data_dict):
        return data_dict[self.key]


def smart_abstraction(text: str) -> str:
    """
    将句子抽象化为标签序列，保留句子结构特征
    """
    words = pseg.cut(text)
    clean_tokens = []
    for word, flag in words:
        w = word.lower()
        if w in KNOWLEDGE_NOUNS:
            clean_tokens.append("<KNOW>")

    for word, flag in words:
        w = word.lower()
        if flag == "d_neg" or w in NEGATION_WORDS:
            clean_tokens.append("<NEG>")  # 否定
        elif flag == "n_func" or w in FUNCTIONAL_NOUNS:
            clean_tokens.append("<FUNC>")  # 面板/数据
        elif flag == "n_media" or w in MEDIA_NOUNS:
            clean_tokens.append("<MEDIA>")  # 图片/视频
        elif flag == "n_edit_obj" or w in EDIT_OBJ_NOUNS:
            clean_tokens.append("<E_OBJ>")  # 字/水印
        elif flag == "n_know" or w in KNOWLEDGE_NOUNS:
            clean_tokens.append("<KNOW>")  # 知识点
        elif flag == "a_state" or w in STATE_WORDS:
            clean_tokens.append("<STATE>")  # 状态
        elif flag == "v_check" or w in CHECK_VERBS:
            clean_tokens.append("<CHECK>")  # 查/看
        elif flag == "v_gen" or w in GENERATE_VERBS:
            clean_tokens.append("<GEN>")  # 生成/画
        elif flag == "v_edit" or w in EDIT_VERBS:
            clean_tokens.append("<EDIT>")  # 修改/加
        elif flag == "r_query" or w in QUERY_WORDS or "?" in w or "？" in w:
            clean_tokens.append("<QUERY>")  # 疑问
        elif flag == "r_chat" or w in CHAT_ENTITIES:
            clean_tokens.append("<SELF>")  # 人称/问候
        elif flag == "v_opinion" or w in OPINION_WORDS:
            clean_tokens.append("<OPINION>")  # 看法
        else:
            if flag.startswith("n") or flag.startswith("v") or flag.startswith("x"):
                clean_tokens.append("<ENT>")
            elif w.strip():
                clean_tokens.append(w)

    return " ".join(clean_tokens)


class IntentService:
    def __init__(self, model_path=MODEL_PATH, num_threads=4):
        self.model_path = model_path
        self.executor = ThreadPoolExecutor(max_workers=num_threads)
        self.model = None
        self._load_or_train()

    def _load_or_train(self):
        need_train = True
        if self.model_path.exists():
            try:
                self.model = load(self.model_path)
                # 检查是否包含所有需要的分类
                if len(self.model.classes_) < 3:
                    logger.warning("[AI] 模型类别不足，重新训练...")
                    need_train = True
                else:
                    logger.info(f"[AI] 意图识别模型已加载: {self.model_path}")
                    need_train = False
            except Exception as e:
                logger.error(f"[AI] 模型加载失败: {e}")
                need_train = True

        if need_train:
            self.train()

    def _generate_enhanced_data(self):
        """生成大规模增强语料"""
        task_with_trash_content = [
            "生成音乐，内容是：<STATE> <STATE>",
            "生成语音：<STATE> <STATE> <SELF> <STATE>",
            "画一张图，上面写着：<STATE>",
            "做一个视频，台词是：<STATE> <STATE>",
            "生成一段代码，注释要写：<STATE>",
            "来个语音说 <STATE>",
            "合成一段音频：<STATE> <STATE>",
        ]

        X_raw = []
        y = []

        # 在生成数据时，特意把工具类语料混入大量的“闲聊词”作为内容
        for _ in range(30):  # 增加这类样本的权重
            for pat in task_with_trash_content:
                text = pat
                text = text.replace("<GEN>", random.choice(list(GENERATE_VERBS)))
                text = text.replace("<STATE>", random.choice(list(STATE_WORDS)))
                text = text.replace("<SELF>", random.choice(list(CHAT_ENTITIES)))
                X_raw.append(text.replace(" ", ""))
                y.append("工具")

        entities = ["雷神", "原神", "纳指", "A股", "钟离", "火神", "这个", "那张", "上一个"]

        # --- 1. 工具 (Tool) ---
        check_patterns = [
            "<CHECK> <ENT>",
            "<CHECK> <ENT> 的 <FUNC>",
            "<CHECK> <FUNC>",
            "帮我 <CHECK> <ENT>",
            "<CHECK> 一下 <ENT> 的 <FUNC>",
            "调用 <ENT>",
            "打开 <ENT>",
        ]

        gen_patterns = [
            "<GEN> 一张 <MEDIA>",
            "<GEN> <ENT> 的 <MEDIA>",
            "帮我 <GEN> <MEDIA>",
            "<GEN> 一个 <ENT>",
            "来个 <MEDIA>",
            "<GEN> <ENT>",
        ]

        edit_patterns = [
            "把 <ENT> <EDIT> 成 <ENT>",
            "在 <ENT> 上 <EDIT> <E_OBJ>",
            "给 <ENT> <EDIT> 个 <E_OBJ>",
            "<EDIT> <ENT>，<EDIT> <E_OBJ>",
            "帮我 <EDIT> 一下",
            "<EDIT> <ENT>",
            "去 <E_OBJ>",
            "在这张 <MEDIA> 下面 <EDIT> 两个 <E_OBJ>",
        ]

        # --- 2. 问答 (QA) ---
        # 严格限制：必须是明确的“知识查询”
        qa_patterns = [
            "<QUERY> <KNOW> 的 <KNOW> 是 <KNOW>",
            "带 <KNOW> 的 <KNOW> 有 <QUERY>",
            "<KNOW> 属性的 <KNOW>",
            "<ENT> 用的 <KNOW>",
            "<ENT> <QUERY> <KNOW>",
            "<ENT> 的 <KNOW> 是 <QUERY>",  # 雷神的血量是多少
            "<ENT> <KNOW> <QUERY>",  # 钟离天赋怎么点
            "<QUERY> 打 <ENT>",  # 怎么打深渊
            "<ENT> 在 <QUERY>",  # 史莱姆在哪
            "<ENT> 的 <KNOW> 介绍",  # 原神的剧情介绍
            "<KNOW> 推荐",  # 配队推荐
            "<ENT> 是 <QUERY>",  # 钟离是谁
            "<ENT> <QUERY> 获得",  # 鱼获怎么获得
            "<ENT> <NUM> 级 <KNOW> 倍率",
            "<ENT> 的 <KNOW> 是多少",
            "<ENT> <KNOW> 倍率",
            "查查 <ENT> 的 <KNOW>",
        ]

        # --- 3. 闲聊 (Chat) ---
        # 重点增强：功能名词 + 负面状态 = 闲聊 (对抗工具误判)
        # 重点增强：主观询问 = 闲聊 (对抗问答误判)
        chat_patterns = [
            "<SELF> 是 <QUERY>",  # 你是谁
            "<SELF> <QUERY> <ENT>",  # 你喜欢雷神吗
            "<SELF> 在 <QUERY>",  # 你在干嘛
            "<SELF> <STATE>",  # 我好难
            "<ENT> <STATE>",  # 深渊太难了
            "<ENT> <NEG> <STATE>",  # 股票不亏
            "<NEG> <CHECK>",  # 别查了
            "<NEG> <GEN>",  # 不要画
            "为什么 <STATE>",  # 为什么亏死
            "<STATE>",  # 笑死 / 救命
            "<FUNC> <STATE>",  # 面板好丑
            "<FUNC> <NEG> <STATE>",  # 走势不好
            "<FUNC> <QUERY>",  # 股价咋样 (询问状态而非查询数据，偏闲聊，但也可能模糊)
            "<SELF> 的 <OPINION> 是 <QUERY>",  # 你的看法是什么
            "<QUERY> 是 <OPINION>",  # 什么是看法
            "这是 <QUERY>",  # 这是什么 (短语视为闲聊)
            "你好",
            "早上好",
            "晚安",
            "在吗",
        ]

        def fill_data(patterns, label, count_multiplier=10):
            for _ in range(count_multiplier):
                for pat in patterns:
                    text = pat
                    if "<CHECK>" in text:
                        text = text.replace("<CHECK>", random.choice(list(CHECK_VERBS)))
                    if "<GEN>" in text:
                        text = text.replace("<GEN>", random.choice(list(GENERATE_VERBS)))
                    if "<EDIT>" in text:
                        text = text.replace("<EDIT>", random.choice(list(EDIT_VERBS)))
                    if "<FUNC>" in text:
                        text = text.replace("<FUNC>", random.choice(list(FUNCTIONAL_NOUNS)))
                    if "<MEDIA>" in text:
                        text = text.replace("<MEDIA>", random.choice(list(MEDIA_NOUNS)))
                    if "<E_OBJ>" in text:
                        text = text.replace("<E_OBJ>", random.choice(list(EDIT_OBJ_NOUNS)))
                    if "<KNOW>" in text:
                        text = text.replace("<KNOW>", random.choice(list(KNOWLEDGE_NOUNS)))
                    if "<QUERY>" in text:
                        text = text.replace("<QUERY>", random.choice(list(QUERY_WORDS)))
                    if "<SELF>" in text:
                        text = text.replace("<SELF>", random.choice(list(CHAT_ENTITIES)))
                    if "<STATE>" in text:
                        text = text.replace("<STATE>", random.choice(list(STATE_WORDS)))
                    if "<NEG>" in text:
                        text = text.replace("<NEG>", random.choice(list(NEGATION_WORDS)))
                    if "<OPINION>" in text:
                        text = text.replace("<OPINION>", random.choice(list(OPINION_WORDS)))
                    if "<ENT>" in text:
                        text = text.replace("<ENT>", random.choice(entities))

                    clean_text = text.replace(" ", "")
                    X_raw.append(clean_text)
                    y.append(label)

        fill_data(check_patterns, "工具", 20)
        fill_data(gen_patterns, "工具", 20)
        fill_data(edit_patterns, "工具", 30)
        fill_data(qa_patterns, "问答", 20)
        fill_data(chat_patterns, "闲聊", 30)  # 增加闲聊权重

        return X_raw, y

    def train(self):
        logger.info("[AI] 开始训练新版意图模型 (v4 - 优化闲聊误判)...")
        X_raw, y = self._generate_enhanced_data()
        X_abstract = [smart_abstraction(text) for text in X_raw]
        X_train_dict = {"raw": X_raw, "abs": X_abstract}

        pipeline = Pipeline(
            [
                (
                    "union",
                    FeatureUnion(
                        transformer_list=[
                            (
                                "abs_features",
                                Pipeline(
                                    [
                                        ("selector", ItemSelector(key="abs")),
                                        (
                                            "tfidf",
                                            TfidfVectorizer(token_pattern=r"(?u)\b\w+\b|<\w+>", ngram_range=(1, 4)),
                                        ),
                                    ]
                                ),
                            ),
                            (
                                "raw_features",
                                Pipeline(
                                    [
                                        ("selector", ItemSelector(key="raw")),
                                        (
                                            "tfidf",
                                            TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), max_features=8000),
                                        ),
                                    ]
                                ),
                            ),
                        ]
                    ),
                ),
                ("clf", LogisticRegression(C=2.0, solver="lbfgs", class_weight="balanced", max_iter=500)),
            ]
        )

        pipeline.fit(X_train_dict, y)
        dump(pipeline, self.model_path)
        self.model = pipeline
        logger.info(f"[AI] 模型训练完成。保存至: {self.model_path}")

    def _rule_based_check(self, text: str) -> Optional[Dict[str, Any]]:
        text = text.strip()

        # 规则：显式的生成指令拦截
        # 只要包含：(生成/画/做/制作/来个) + (音乐/歌/语音/图/视频/画)
        # 即使后面跟着很长的情绪化内容，也直接判定为工具
        if re.search(
            r"(生成|制作|画|写|来个|整一个|弄个|创作|合成)(一张|一段|一首|个)?(音乐|歌|曲子|语音|声音|图|照片|画|视频|动画|代码|文案)",
            text,
        ):
            return {"intent": "工具", "conf": 0.99, "reason": "Rule: Explicit Generation Command"}

        # [新增] 处理带引号的内容指令
        # 例如：生成语音，内容是：“...”
        if re.search(r"(内容|说|字|内容为)[:：= \"“]", text) and re.search(r"(生成|画|做|语音|图|音乐)", text):
            return {"intent": "工具", "conf": 0.98, "reason": "Rule: Task With Content"}

        # --- 保持之前的闲聊拦截规则 ---
        has_func = re.search(r"(面板|数据|战绩|排行|走势|股价|行情|价格|配置|装备|评分)", text)
        has_state = re.search(r"(丑|亏|烂|差|崩|难看|垃圾|离谱|恶心|高|低|麻|药丸|贵|便宜)", text)
        has_check = re.search(r"(查|看|找|搜|分析|计算|显示|获取|调用)", text)
        if has_func and has_state and not has_check:
            return {"intent": "闲聊", "conf": 0.95, "reason": "Rule: Noun+Emotion=Chat"}

        if re.search(r"(你|我|大家).*(看法|觉得|认为|评价|观点|想)", text):
            return {"intent": "闲聊", "conf": 0.96, "reason": "Rule: Subjective Opinion"}

        # 规则 1.3: 极短的代词指代询问 -> 闲聊
        # 解决 "这是什么", "那是什么鬼"
        # 逻辑：这/那 + 是 + 什么/啥 (且没有其他具体实体)
        if re.search(r"^(这|那|它)(是|个)?(什么|啥|鬼)[?？]*$", text):
            return {"intent": "闲聊", "conf": 0.95, "reason": "Rule: Vague Query"}

        # ================== 2. 强工具指令 (优先级次之) ==================

        if re.search(r"(画|生成|制作|合成|写|搞|整|来).{0,5}(一张|个|份|首|段)?(图|照片|画|视频|语音|歌|代码)", text):
            return {"intent": "工具", "conf": 0.99, "reason": "Rule: Generate Media"}

        if re.search(r"(修改|编辑|P一下|P图|去水印|加水印|换背景)", text):
            return {"intent": "工具", "conf": 0.99, "reason": "Rule: Explicit Edit"}

        if re.search(r"(在|把|给).{0,10}(图|照片|上|下|里|面).{0,5}(加|换|改|写|放).{0,5}(字|文|水印|背景)", text):
            return {"intent": "工具", "conf": 0.99, "reason": "Rule: Complex Edit Command"}

        # 增加判断：如果有“查/看” + “名词”，基本是工具
        if re.search(r"(查|看|找|搜|分析|计算|显示).{0,8}(面板|数据|战绩|排行|榜|走势|股价|天气|配置|运势|记录)", text):
            return {"intent": "工具", "conf": 0.98, "reason": "Rule: Check Data"}

        if re.search(r"^(调用|打开|启动|运行).{1,10}", text):
            return {"intent": "工具", "conf": 0.98, "reason": "Rule: Invoke App"}

        if re.search(r"^(帮我|给).*(看|查|算).*(一下)?$", text):
            return {"intent": "工具", "conf": 0.96, "reason": "Rule: Help Check"}

        # ================== 3. 其他规则 ==================

        if re.search(r"^(你|我|他).*(是|喜欢|爱|吃|睡|像).*(什么|谁|哪|猫|狗|人|AI|机器人)", text):
            return {"intent": "闲聊", "conf": 0.98, "reason": "Rule: Identity Chat"}

        if re.search(r"^(你好|在吗|早|晚安|嘿|哈|哎|卧槽|救命|测试)$", text):
            return {"intent": "闲聊", "conf": 0.99, "reason": "Rule: Simple Chat"}

        # 问答规则放最后，防止抢占
        if re.search(r".*(怎么打|怎么配队|在哪里|在哪抓|什么效果|技能介绍|背景故事|突破材料).*", text):
            return {"intent": "问答", "conf": 0.96, "reason": "Rule: Strong QA Pattern"}

        return None

    def _sync_predict(self, text: str) -> Dict[str, Any]:
        rule_result = self._rule_based_check(text)
        if rule_result:
            return {"text": text, **rule_result}

        if self.model is None:
            return {"text": text, "intent": "Error", "conf": 0.0, "reason": "Model Not Loaded"}

        abstracted = smart_abstraction(text)
        input_data = {"raw": [text], "abs": [abstracted]}

        try:
            probs = self.model.predict_proba(input_data)[0]
            intent_idx = probs.argmax()
            intent = self.model.classes_[intent_idx]
            confidence = float(probs[intent_idx])

            # 后置修正：如果置信度不高，且含有负面情绪词，倾向于闲聊
            # 解决 "面板真垃圾" 这类没被正则覆盖到的漏网之鱼
            if intent == "工具" and confidence < 0.85:
                for w in STATE_WORDS:
                    if w in text:
                        intent = "闲聊"
                        confidence = 0.8
                        break

            return {"text": text, "intent": intent, "conf": round(confidence, 4), "reason": "Model"}
        except Exception as e:
            return {"text": text, "intent": "Error", "conf": 0.0, "reason": str(e)}

    async def predict_async(self, text: str) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._sync_predict, text)


# ==========================================
# 测试代码 (直接运行此文件可看效果)
# ==========================================
async def benchmark(service: IntentService):
    test_cases = [
        # --- 工具类 (新需求) ---
        "在这张图下面加两个字'可爱'",  # 必须是工具
        "修改这张图，在下面加两个字",  # 必须是工具
        "帮我生成一张纳西妲的图片",  # 工具
        "画个雷神",  # 工具
        "生成一段关于原神的视频",  # 工具
        "来首好听的音乐",  # 工具
        "把这个背景换成蓝色的",  # 工具
        "查一下面板",  # 工具
        "看一下雷神的数据",  # 工具
        "调用计算器",  # 工具
        "帮我看一下这个",  # 工具
        # --- 问答类 (知识查询) ---
        "雷神怎么配队",  # 问答
        "纳西妲的突破材料在哪找",  # 问答
        "火神是谁",  # 问答
        "深渊第12层怎么打",  # 问答
        "钟离的护盾机制是啥",  # 问答
        # --- 闲聊类 ---
        "你是猫猫吗？",  # 闲聊
        "在吗",  # 闲聊
        "你好啊",  # 闲聊
        "我今天好倒霉",  # 闲聊
        "这是什么鬼",  # 闲聊
        "笑死我了",  # 闲聊
        "深渊好难打",  # 闲聊
        "面板太丑了",  # 闲聊
        "股票亏麻了",  # 闲聊
        "卧槽怎么回事",  # 闲聊
        "这是什么",  # 闲聊
        "不要查",  # 闲聊
        "你是使用什么模型？",  # 闲聊
        "你对抱抱的看法是？",  # 闲聊
        "生成音乐 可爱的女声，内容为：'嗯嗯 啊啊 欧欧 XX你真的好棒呀'",
    ]

    print(f"\n{'Input Text':<30} | {'Intent':<6} | {'Conf':<5} | {'Reason'}")
    print("-" * 80)

    for text in test_cases:
        res = await service.predict_async(text)
        print(f"{res['text']:<30} | {res['intent']:<6} | {res['conf']:<5} | {res['reason']}")


classifier_service = IntentService(model_path=MODEL_PATH)

if __name__ == "__main__":
    asyncio.run(benchmark(classifier_service))
