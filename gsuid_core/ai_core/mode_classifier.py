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

# 完全禁用 jieba 的所有日志输出
jieba_logger = logging.getLogger("jieba")
jieba_logger.setLevel(logging.CRITICAL)  # 只显示 CRITICAL 级别的日志
jieba_logger.propagate = False

# 同时重定向 stdout/stderr 来捕获 jieba 的直接输出
_old_stdout = sys.stdout
_old_stderr = sys.stderr


class DevNull:
    def write(self, msg):
        pass

    def flush(self):
        pass


# 临时重定向到空设备
sys.stdout = DevNull()
sys.stderr = DevNull()

import jieba  # noqa: E402
import jieba.posseg as pseg  # noqa: E402

# 恢复 stdout/stderr
sys.stdout = _old_stdout
sys.stderr = _old_stderr

AI_PATH = get_res_path("ai_core")
MODEL_PATH = AI_PATH / "intent_classifier.joblib"


ACTION_VERBS = {
    "查",
    "看",
    "找",
    "搜",
    "查询",
    "搜索",
    "分析",
    "生成",
    "打开",
    "计算",
    "推荐",
    "翻译",
    "解释",
    "写",
    "做",
    "画",
    "来",
    "查查",
    "看看",
    "搜搜",
    "测",
    "估算",
    "监控",
    "显示",
    "列举",
}

FUNCTIONAL_NOUNS = {
    "面板",
    "数据",
    "属性",
    "排行",
    "排行榜",
    "榜单",
    "攻略",
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
    "信息",
    "情况",
    "状态",
    "数值",
    "倍率",
    "概率",
    "掉落",
    "成本",
    "收益",
}

NEGATION_WORDS = {"不", "没", "无", "非", "莫", "别", "不要", "不用", "休想", "禁止", "别去", "休"}

STATE_WORDS = {
    "麻",
    "麻了",
    "亏",
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
    "垃圾",
    "坑",
    "药丸",
    "崩",
    "崩了",
    "水",
    "难",
    "好难",
    "太难",
    "不行",
    "一般",
    "差",
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
    "拉胯",
    "怪",
    "寄",
    "晦气",
    "谢",
    "谢了",
    " thanks",
    "ok",
    "懂",
    "明白",
    "理解",
    "清楚",
    "知道",
    "迷糊",
    "晕",
    "懵",
    "疑惑",
}

QUERY_WORDS = {"怎么", "多少", "什么", "谁", "哪里", "几", "吗", "呢", "啥", "咋", "如何", "为什么"}


# 初始化 Jieba
def init_jieba():
    for w in FUNCTIONAL_NOUNS:
        jieba.add_word(w, tag="n_prop")
    for w in NEGATION_WORDS:
        jieba.add_word(w, tag="d_neg")
    for w in STATE_WORDS:
        jieba.add_word(w, tag="a_state")
    for w in ACTION_VERBS:
        jieba.add_word(w, tag="v_act")
    for w in QUERY_WORDS:
        jieba.add_word(w, tag="r_query")


init_jieba()


class ItemSelector(BaseEstimator, TransformerMixin):
    """用于在 Pipeline 中选择字典数据的特定 Key"""

    def __init__(self, key):
        self.key = key

    def fit(self, x, y=None):
        return self

    def transform(self, data_dict):
        return data_dict[self.key]


def smart_abstraction(text: str) -> str:
    """
    逻辑：将文本转化为抽象标签序列，例如 "查雷神面板" -> "<ACT> <ENT> <PROP>"
    """
    words = pseg.cut(text)
    clean_tokens = []

    for word, flag in words:
        w = word.lower()
        if flag == "d_neg" or w in NEGATION_WORDS:
            clean_tokens.append("<NEG>")
        elif flag == "n_prop" or w in FUNCTIONAL_NOUNS:
            clean_tokens.append("<PROP>")
        elif flag == "a_state" or w in STATE_WORDS:
            clean_tokens.append("<STATE>")
        elif flag == "v_act" or w in ACTION_VERBS:
            clean_tokens.append("<ACT>")
        elif flag == "r_query" or w in QUERY_WORDS or "?" in w or "？" in w:
            clean_tokens.append("<QUERY>")
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
        """尝试加载模型，如果不存在或加载失败则强制重新训练"""
        # 标记是否需要训练
        need_train = False

        if self.model_path.exists():
            try:
                # 尝试读取现有模型
                self.model = load(self.model_path)
                logger.debug(f"[Info] 模型已加载: {self.model_path}")
            except Exception as e:
                logger.warning(f"[Error] 模型加载失败 (版本不兼容或路径错误): {e}")
                logger.warning("[Info] 正在重新训练模型以修复此问题...")
                need_train = True
        else:
            logger.debug(f"[Warning] 模型文件 {self.model_path} 不存在。")
            need_train = True

        # 如果需要训练（文件不存在 或 加载报错）
        if need_train:
            self.train()

    def _generate_enhanced_data(self):
        tool_samples = []
        chat_samples = []
        entities = ["雷神", "茅台", "纳指", "王者荣耀", "原神", "这只股票", "今天", "A股"]

        tool_patterns = [
            "<ACT> <ENT>",
            "<ACT> <PROP>",
            "<ACT> <ENT> <PROP>",
            "<ENT> <PROP>",
            "<ENT> 的 <PROP>",
            "<ENT> <ACT> <PROP>",
            "<ACT> <ENT> <PROP> <QUERY>",
        ]

        chat_patterns = [
            "<NEG> <ACT>",
            "<NEG> <ACT> <ENT>",
            "<PROP> <STATE>",
            "<PROP> <NEG> <STATE>",
            "<ENT> <STATE>",
            "<ENT> <NEG> <STATE>",
            "<STATE>",
            "<ENT> <ACT> <STATE>",
            "<ACT> <NEG> <ACT>",
            "我 <NEG> 知道",
            "<ACT> <NEG> <STATE>",
            "<ACT> <NEG> <ENT>",
            "<ENT> <QUERY>",
        ]

        # 生成工具数据
        for pattern in tool_patterns:
            for ent in entities:
                text = pattern.replace("<ENT>", ent)
                if "<ACT>" in text:
                    text = text.replace("<ACT>", random.choice(list(ACTION_VERBS)))
                if "<PROP>" in text:
                    text = text.replace("<PROP>", random.choice(list(FUNCTIONAL_NOUNS)))
                if "<QUERY>" in text:
                    text = text.replace("<QUERY>", random.choice(list(QUERY_WORDS)))
                tool_samples.append(text)

        # 生成闲聊数据
        for pattern in chat_patterns:
            for ent in entities:
                text = pattern.replace("<ENT>", ent)
                if "<ACT>" in text:
                    text = text.replace("<ACT>", random.choice(list(ACTION_VERBS)))
                if "<PROP>" in text:
                    text = text.replace("<PROP>", random.choice(list(FUNCTIONAL_NOUNS)))
                if "<STATE>" in text:
                    text = text.replace("<STATE>", random.choice(list(STATE_WORDS)))
                if "<NEG>" in text:
                    text = text.replace("<NEG>", random.choice(list(NEGATION_WORDS)))
                chat_samples.append(text)

        extra_chats = [
            "这数据太真实了",
            "属性拉胯",
            "看不懂这个走势",
            "这是什么鬼攻略",
            "别给我看这些",
            "不要分析",
            "我不查",
            "算了吧",
        ]
        chat_samples.extend(extra_chats * 5)

        min_len = min(len(tool_samples), len(chat_samples))
        X = tool_samples[:min_len] + chat_samples[:min_len]
        y = ["工具"] * min_len + ["闲聊"] * min_len
        return X, y

    def train(self):
        """训练并保存模型"""
        logger.debug("[Info] 开始训练模型...")
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
                                            TfidfVectorizer(token_pattern=r"(?u)\b\w+\b|<\w+>", ngram_range=(1, 3)),
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
                                            TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), max_features=5000),
                                        ),
                                    ]
                                ),
                            ),
                        ]
                    ),
                ),
                ("clf", LogisticRegression(C=1.0, solver="liblinear", class_weight="balanced")),
            ]
        )

        pipeline.fit(X_train_dict, y)
        dump(pipeline, self.model_path)
        self.model = pipeline
        logger.debug(f"[Info] 模型训练完成并保存至: {self.model_path}")

    def _rule_based_check(self, text: str) -> Optional[Dict[str, Any]]:
        """优先执行的正则/逻辑规则"""

        # 规则 0: 询问大模型自身的问题 = 闲聊 (你是什么/你是谁/你使用什么模型)
        if re.search(
            r"^(我|你).*(是|使用|能|会).*(什么|谁|啥|怎么|多少|多大|名字|型号).*(模型|AI|助手|机器人|版本)", text
        ):
            return {"intent": "闲聊", "conf": 0.99, "reason": "Rule: SelfReference"}

        # 规则 1: 代词+疑问 = 闲聊 (这是什么/那是谁)
        if re.search(r"^(这|那|我|你|他|她|它|哪|谁).*(什么|咋|谁|哪|吗|呢)[?？]?$", text):
            return {"intent": "闲聊", "conf": 0.98, "reason": "Rule: Pronoun+Query"}

        # 规则 2: 纯疑问/情绪表达 = 闲聊 (为什么/咋回事/啊啊啊)
        if re.search(r"^(为什么|咋回事|啊|哎呀|呜呜|哼|呵呵|哈哈|哇|唉|哎哟)+[!?😭😭😢😱😡🙏]+.*$", text):
            return {"intent": "闲聊", "conf": 0.95, "reason": "Rule: PureEmotion"}

        # 规则 3: 询问观点/身份/模拟/建议 = 闲聊 (你对...看法/模拟.../你应该...)
        if re.search(r".*(你对.*看法|你觉得|你认为|模拟|是.*化身|你应该|你要|你每).*", text):
            return {"intent": "闲聊", "conf": 0.93, "reason": "Rule: OpinionOrSimulate"}

        # 规则 4: 动词+否定/状态 = 闲聊 (看不懂/做不到)
        if re.search(r"(查|看|搜|找|分析|算|听|说)(不|没|无法|不能)(懂|了|到|行|好|明白)", text):
            return {"intent": "闲聊", "conf": 0.97, "reason": "Rule: Act+Neg+State"}

        # 规则 5: 强否定 + 动作 = 闲聊 (不要查)
        if re.search(r"[不别没非][要]?.*?(查|看|搜|分析|算|测)", text):
            return {"intent": "闲聊", "conf": 0.99, "reason": "Rule: Negation+Action"}

        # 规则 6: 纯情绪/状态词主导
        has_state = any(s in text for s in STATE_WORDS)
        has_query = any(q in text for q in QUERY_WORDS)
        has_prop = any(p in text for p in FUNCTIONAL_NOUNS)

        # 如果包含状态词，且没有明确的疑问词
        if has_state and not has_query:
            if has_prop:
                return {"intent": "闲聊", "conf": 0.95, "reason": "Rule: Prop+State"}

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
            return {"text": text, "intent": intent, "conf": round(confidence, 4), "reason": "Model"}
        except Exception as e:
            return {"text": text, "intent": "Error", "conf": 0.0, "reason": str(e)}

    async def predict_async(self, text: str) -> Dict[str, Any]:
        """外部调用的异步接口"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, self._sync_predict, text)


# ==========================================
# 4. 测试与运行
# ==========================================


async def benchmark(service: IntentService):
    test_cases = [
        "查面板",
        "火神面板怎么提升",
        "帮我看看深渊记录",
        "查一下茅台股价",
        "看看英伟达走势",
        "打开空调",
        "帮我关灯",
        "面板太丑了",
        "深渊好难打",
        "数据不太好",
        "茅台跌得好惨",
        "股票亏麻了",
        "卧槽怎么回事",
        "这是什么",
        "看不懂",
        "不要查",
        "茅台跌了吗",
        "光线传媒最近六个月涨的怎么样",
        "你是使用什么模型？",
        "为什么😭",
        "为什么!",
        "请问你对小招喵是什么看法？",
        "你对抱抱的看法是？你是一个猫娘",
        "模拟小狗的叫声",
        "你既然是小招喵的化身，你没一句话的结尾应该加一个“喵”字",
    ]

    logger.debug(f"{'Input':<20} | {'Intent':<10} | {'Conf':<5} | {'Reason'}")
    logger.debug("-" * 65)

    tasks = [service.predict_async(t) for t in test_cases]
    results = await asyncio.gather(*tasks)

    for res in results:
        logger.debug(f"{res['text']:<20} | {res['intent']:<10} | {res['conf']:<5} | {res.get('reason', '-')}")


classifier_service = IntentService(model_path=MODEL_PATH)

if __name__ == "__main__":
    asyncio.run(benchmark(classifier_service))
