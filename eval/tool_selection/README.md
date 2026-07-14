# 工具选择评测（Tool Selection Eval）

补上 `eval/agent/`（382 例）**没有覆盖**的一层：**框架把哪些工具装进了本轮工具池**。

## 为什么单独做

2026-07-15 生产事故：用户问「看下我玄翎秧秧面板」（鸣潮角色），AI 全程只调异环工具。
根因不在模型——**鸣潮工具压根没进工具列表**，模型再聪明也无从选起。`eval/agent/` 测的是
「模型拿到工具后选得对不对」，测不到「框架给没给对工具」。这两层的锅必须分开算：

| 指标 | 测什么 | 谁的锅 | 成本 |
|---|---|---|---|
| **Pool Recall** | 正确插件的工具**有没有进工具池** | 框架装配（检索 / 族展开 / 路由） | **零 LLM，秒级** |
| **Call Precision** | 模型第一个 `tool_call` 对不对 | 模型选择 | 需要 LLM |

本评测只做 **Pool Recall**——它是 Call Precision 的**上界**：工具没进池，模型不可能选对。
零 LLM 意味着可以每改一行装配代码就重跑一次。

## Ground truth 从哪来（零人工标注）

`ai_core/entity_index.py` 的实体身份索引：插件用 `ai_alias` 注册的每个正式名 / 别名，
都带**确定的插件归属**。所以「"长离"属于 XutheringWavesUID」是注册表里写死的事实，
不是标注出来的。用「实体 × 句式模板」交叉相乘即可自动生成上千条用例。

跨插件对撞集是**自动形成**的：同一句式套不同插件的实体，正好测「鸣潮角色会不会被
路由到异环」——也就是事故的原型。

## 用法

```bash
# 快速子集（默认每插件 40 个实体 × 全部模板）
NO_PROXY=localhost,127.0.0.1 python eval/tool_selection/run.py

# 全量
NO_PROXY=localhost,127.0.0.1 python eval/tool_selection/run.py --per-plugin 0

# 只看某个插件
NO_PROXY=localhost,127.0.0.1 python eval/tool_selection/run.py --plugin XutheringWavesUID
```

需要 Qdrant 在跑（工具向量库 `bot_tools` 已入库）。结果落 `results/`。

## 读结果

- **Pool Recall** 低 → 框架装配的问题（检索召不回 / 大族挤占 / 没有实体路由）。
- **Top-Seed 正确率** 低但 Pool Recall 高 → 嵌入排序不佳，但兜底席位救回来了。
- **Confusion** 表 → 谁抢了谁的位置（事故里就是「异环面板」族抢了鸣潮的）。
