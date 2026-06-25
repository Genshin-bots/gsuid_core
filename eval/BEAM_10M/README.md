# BEAM-10M 评测

针对 `gsuid_core` 框架的 **AI Memory 模块**，在 [BEAM-10M](https://huggingface.co/datasets/kyon-1/BEAM-10M)（10 条对话 × 10 个 plan × 100+ turn，单对话最高 1000 万 Token）上的端到端长上下文记忆评测。

与已有的 LongMemEval 评测（500 题，6 类，平均每题 ~10K Token）相比，BEAM-10M 把规模拉到 **10 倍以上**，并新增了 4 个评测维度（abstention / contradiction_resolution / summarization / instruction_following）。两套脚本共用 `eval/common/` 下的 HTTP / Judge / IO 工具，新增端点也都为「评测专用」。

---

## 一、数据集概览

10 条独立 conversation，每条由 BEAM 自动合成一个虚拟用户 persona + 10 个 plan：

- 字段 `chat` 是 `dict[plan-1..plan-10] -> [batch, ...]`，每个 `batch = {batch_number, time_anchor, turns}`；
- `turns` 是嵌套数组（pandas 读取后是 `np.ndarray[np.ndarray[dict]]`），每个 turn 含 `role / content / time_anchor / id / index / question_type`；
- `plans` 是数组形态（0-indexed），字段与 `chat` 同构但每个 plan 额外带 `user_questions / conversation_seed / user_profile`；
- `probing_questions` 是 **Python repr 字符串**，用 `ast.literal_eval` 解析成 10 类 × 2 题 = 20 题：

| 类别 | 字段 | 标准答案字段 | 含义 |
|---|---|---|---|
| `abstention` | 是否拒答 | `ideal_response` | 用户问到数据集中没出现的信息，正确做法是声明不知道 |
| `contradiction_resolution` | 冲突调和 | `ideal_answer` | 用户前后表述矛盾，正确做法是指出冲突并请求澄清 |
| `event_ordering` | 事件排序 | `answer` | 按 `time_anchor` 排出事件先后 |
| `information_extraction` | 事实抽取 | `answer` | 从若干对话片段中精确抽取事实 |
| `instruction_following` | 指令遵循 | `expected_compliance` | 用户给出强约束，验证 Agent 是否遵守 |
| `knowledge_update` | 知识更新 | `answer` | 后续 turn 更新了先前的事实，验证召回到的是新版本 |
| `multi_session_reasoning` | 跨会话 | `answer` | 综合多 plan 信息作答 |
| `preference_following` | 偏好遵循 | `expected_compliance` | 用户表达偏好（咖啡口味、沟通风格），验证是否一致 |
| `summarization` | 摘要 | `ideal_summary` | 用标准摘要核对 Agent 总结是否覆盖核心要点 |
| `temporal_reasoning` | 时间推理 | `answer` | 需要从 `time_anchor` 中推算时间区间/先后关系 |

每题还自带 `rubric` 列表（2~5 项 check），是 LLM 评判的依据。

> 详见 BEAM 原论文 §4 "Categories of Probing Questions"。

---

## 二、框架改动一览（本次新增）

### 2.1 `POST /api/ai/memory/batch_observe` —— 批量摄入

文件：`gsuid_core/webconsole/ai_memory_api.py`

| 项目 | 说明 |
|---|---|
| 路径 | `POST /api/ai/memory/batch_observe` |
| 鉴权 | **不需要**（与 `/api/chat_with_history` 一致，方便评测脚本裸调） |
| 目的 | 大批量摄入评测语料，**不**创建 Agent、**不**调用 LLM、**不**污染 ChatSession |

请求体：

```json
{
  "user_id": "beam_eval_0",
  "scope_type": "user_global",
  "group_id": null,
  "bot_self_id": null,
  "turns": [
    {"role": "user",      "content": "...", "timestamp": "2024-07-01T00:00:00+00:00"},
    {"role": "assistant", "content": "...", "timestamp": "2024-07-01T00:00:30+00:00"}
  ],
  "flush": true,
  "trigger_rebuild": false
}
```

返回：

```json
{
  "status": 0,
  "msg": "ok",
  "data": {
    "observed": 1832,
    "dropped": 0,
    "scope_key": "user_global:beam_eval_0",
    "flush": true,
    "rebuild": false
  }
}
```

行为细节：

- 每条 `role=assistant` 的 turn 把 `speaker_id` 前缀为 `__assistant_<bot_id>__`，自动走 SELF scope（与 `chat_with_history_api.py` 现有路由一致）；
- `timestamp` 缺省时退回 `datetime.now(timezone.utc)`，传入时直接写到 `ObservationRecord.timestamp`，再经摄入 worker 透传到 `AIMemEpisode.valid_at` + Qdrant 的 `valid_at_ts`，**对 event_ordering / temporal_reasoning 类探针至关重要**；
- `flush=true` 同步调用 `worker.flush_all()`，让摄入立即可召回；
- `trigger_rebuild=true` 异步触发 `rebuild_task(scope_key)`，可作为 "eval 收尾" 选项。

### 2.2 `observe()` / `ObservationRecord` 支持可选 `timestamp`

文件：`gsuid_core/ai_core/memory/observer.py`

```python
async def observe(
    content: str,
    speaker_id: str,
    group_id: Optional[str],
    bot_self_id: str,
    observer_blacklist: list[str],
    message_type: str = "group_msg",
    timestamp: Optional[datetime] = None,   # ← 新增
) -> None: ...
```

- 不传 `timestamp` 时走 `datetime.now(timezone.utc)`，**向后兼容**所有现有调用方；
- `chat_with_history_api.py` 的同步更新：把 `turn['timestamp'] / turn['time'] / turn['ts']` 解析为 `datetime` 后传入，ISO8601 字符串与 Unix 数字两种都接受。

---

## 三、评测流程

每条 conversation 分配唯一 `user_id = beam_eval_<conv_idx>`，scope = user_global（与 chat_with_history 一致）：

```
   clear  ──►  ingest-batch  ──►  probe  ──►  judge
 (DELETE      (POST batch_      (POST chat_   (POST chat_
 /api/ai/     observe)          with_history) with_history)
 memory/.../                     enable_obs=
 global/clear)                   false
```

为什么要按 plan 累计摄入？BEAM-10M 单 plan ≈ 1.2M Token，单 conversation ≈ 12M Token，按 plan 切片可以：

1. 测 **"长度-准确率"曲线**——分别测 plan 1 / 1+2 / 1+2+3 下的召回质量；
2. 与 `hiergraph_min_entities` 等分层图门槛互动，看什么时候触发重建收益开始递减；
3. 单次摄入任务粒度变小，便于中途断点续跑。

---

## 四、HTTP 端点速查

| 端点 | 方法 | 用途 | 本次涉及 |
|---|---|---|---|
| `/api/chat_with_history` | POST | 一次性传 history + question，触发 Agent 回复（同时记录到 ChatSession） | probe / judge 都走它 |
| `/api/ai/memory/batch_observe` | POST | 纯摄入，不创建 Agent，不调 LLM | **新增**，ingest-* 都走它 |
| `/api/ai/memory/users/{user_id}/global/clear` | DELETE | 清空 user_global 全部记忆 | clear 子命令 |
| `/api/ai/memory/hiergraph/rebuild` | POST | 触发分层图重建（异步） | 可选 `--rebuild` |

> `/api/ai/memory/batch_observe` 不带 `Depends(require_auth)`，匹配 `/api/chat_with_history` 的现有约定。如果未来要收敛到统一鉴权，建议在 `web_api.py` 引入专用 `require_eval_auth` 依赖。

---

## 五、子命令速查

| 子命令 | 必填参数 | 可选 | 作用 |
|---|---|---|---|
| `clear` | `--conv` | — | 清空 `user_global:beam_eval_<conv>` 记忆 |
| `ingest-plan` | `--conv --plan` | `--flush/--no-flush --rebuild` | 摄入单个 plan 的 turn |
| `ingest-batch` | `--conv --plans` | `--flush/--no-flush --rebuild` | 累计摄入多个 plan（逗号分隔） |
| `probe` | `--conv` | `--scope --no-resume` | 对 20 道探针题收集 Agent 回答 |
| `judge` | `--answers` | `--no-resume` | 按 rubric 给分 |
| `all` | `--conv --plans` | `--scope --no-judge --rebuild` | clear→ingest→probe→judge 一站式 |

> 注意 `--plan` / `--plans` 用 **1-indexed**（`--plans 1,2,3` 即 plans 数组里的 1/2/3 号 plan，对应 BEAM 论文里的 plan-1/plan-2/plan-3）。

### 典型示例

```bash
# 1) 单 plan 一站式（最快得到一个数据点）
python eval/BEAM_10M/run_beam_eval.py all --conv 0 --plans 1

# 2) 多 plan 累计，做"长度-准确率"曲线
python eval/BEAM_10M/run_beam_eval.py all --conv 0 --plans 1,2,3
python eval/BEAM_10M/run_beam_eval.py all --conv 0 --plans 1,2,3,4,5

# 3) 拆分四步（适合长时间任务、断点续跑）
python eval/BEAM_10M/run_beam_eval.py clear          --conv 0
python eval/BEAM_10M/run_beam_eval.py ingest-batch   --conv 0 --plans 1,2,3 --rebuild
python eval/BEAM_10M/run_beam_eval.py probe          --conv 0
python eval/BEAM_10M/run_beam_eval.py judge          --answers eval/BEAM_10M/results/answers_0.json

# 4) 复用已有 judge 结果，仅复跑 judge
python eval/BEAM_10M/run_beam_eval.py judge --answers eval/BEAM_10M/results/answers_0.json

# 5) 跨 10 条对话批量
for i in $(seq 0 9); do
  python eval/BEAM_10M/run_beam_eval.py all --conv $i --plans 1
done
```

### 全局选项

| 选项 | 默认值 | 说明 |
|---|---|---|
| `--data` | `eval/BEAM_10M/data/10M-*.parquet` | parquet 路径或 glob；自动加载两个分片 |
| `--output-dir` | `eval/BEAM_10M/results` | 答卷 / 评判结果目录 |
| `--base-url` | `http://127.0.0.1:8765` | gsuid_core HTTP 服务地址 |
| `--timeout` | `4000.0` | 单次请求超时（秒） |

---

## 六、输出文件结构

```
eval/BEAM_10M/results/
├── answers_<conv>.json         # 该 conv 的 20 道题答卷
├── judge_<conv>.json          # 该 conv 的 rubric 给分
└── summary.json                # 跨 conv 汇总（手动写或自己脚本生成）
```

### `answers_<conv>.json` 字段（每题一条）

```json
{
  "question_id": "beam_eval_0__event_ordering__0",
  "category": "event_ordering",
  "question": "...",
  "standard_answer": "...",
  "agent_answer": "...",
  "memory": "<MemoryContext.to_memory_text() 字符串>",
  "rubric": ["check 1", "check 2", "..."],
  "time_anchor": "...",
  "status_code": 200,
  "user_id": "beam_eval_0"
}
```

### `judge_<conv>.json` 字段（每题一条）

```json
{
  "question_id": "beam_eval_0__event_ordering__0",
  "category": "event_ordering",
  "judge": {
    "rubric_scores": [1, 1, 0],
    "passed": false,
    "reason": "第 3 条未命中：..."
  }
}
```

> `rubric_scores` 与 `rubric` 列表按位对齐，`passed=True` 当且仅当 **所有 rubric 全部命中**（与 judge_beam_single 的 fallback 规则一致，详见 `eval/common/judge.py`）。

---

## 七、评分方式

`judge_beam_single`（`eval/common/judge.py`）给 LLM 的 prompt 模板：

```
你是一名长对话记忆评测裁判。基于【类别】【标准答案】和【rubric 检查点】判断 Agent 输出是否达标。
请按 rubric 逐条判断是否命中（1 表示命中，0 表示未命中），并给出整体 PASS/FAIL。
整体 PASS 定义：rubric 检查点全部命中，或 Agent 答案的核心事实/语义与标准答案一致。
【类别】{category}
【问题】{question}
【标准答案】{standard_answer}
【rubric 检查点】
1. ...
2. ...
【Agent 答案】{agent_answer}
请严格输出以下 JSON：
{"rubric_scores":[1,0,1,...], "passed": true, "reason": "..."}
```

### 汇总指标建议

```python
import json
from collections import defaultdict
records = json.load(open("eval/BEAM_10M/results/judge_0.json"))
by_cat = defaultdict(lambda: {"passed": 0, "total": 0, "rubric_hit": 0, "rubric_total": 0})
for r in records:
    cat = r["category"]
    j = r["judge"]
    by_cat[cat]["total"] += 1
    by_cat[cat]["passed"] += int(bool(j["passed"]))
    by_cat[cat]["rubric_hit"] += sum(j["rubric_scores"])
    by_cat[cat]["rubric_total"] += len(j["rubric_scores"])

for cat, s in by_cat.items():
    print(f"{cat:30s}  acc={s['passed']/s['total']:.2f}  "
          f"rubric={s['rubric_hit']}/{s['rubric_total']}")
```

> 评测可在多个 plan 数（如 1 / 1,2 / 1,2,3 / …）下分别跑一次，把每类的 `passed/total` 与 `rubric_hit/rubric_total` 画成"上下文长度 → 准确率"曲线即可得 BEAM-10M 标准评测报告。

---

## 八、与 LongMemEval 的对比

| 维度 | LongMemEval | BEAM-10M |
|---|---|---|
| 题量 | 500 | 200（10 对话 × 20） |
| 类别 | 6 | 10（多 4 类：abstention / contradiction / summarization / instruction_following） |
| 单次上下文 | 平均 ~10K Token | 单 plan ~1.2M / 单 conv 最高 10M |
| 摄入方式 | 一次性 chat_with_history | **新增** batch_observe 端点 + 时间戳回填 |
| 判分字段 | `answer` 一字段 | `answer / ideal_response / ideal_answer / ideal_summary / expected_compliance` 多字段 |
| 判分依据 | 标准答案语义一致 | 标准答案 **+ rubric 逐条** |
| 评测专用端点 | 无（复用 chat_with_history） | `/api/ai/memory/batch_observe` |

两套脚本都从 `eval/common/` import 共享工具；新增 BEAM 评测**没有**改动 LongMemEval 的逻辑，只是把它的辅助函数（`call_chat_with_history` / `judge_single_answer` / `parse_judge_response` / `simple_string_match` / `load_eval_data` / `load_existing_answers`）挪进了 `eval.common.*`。

---

## 九、限制与注意事项

1. **首次运行前**：确保 `pyarrow` 或 `pandas` 已安装（用于读 parquet），并启动 gsuid_core HTTP 服务（`ENABLE_HTTP=True`）。
2. **1000 万 Token 摄入耗时**：单 plan ≈ 1.2M Token，10 plan 全量预计 **小时级**，建议先用 `--plans 1` 跑通，再递增 plan 数。
3. **`time_anchor` 格式**：BEAM 数据集中常见 `"July-01-2024"` / `"2024-07-01"` / `"2024-07-01 10:00:00"` 三种，本脚本的 `parse_time_anchor` 已覆盖；缺失的 turn 继承本 batch 内最近一条非空 anchor（每个 batch 一般首条 turn 带 anchor）。
4. **不重置会导致摄入污染**：跨 conv 评测务必用 `--conv <idx>` 隔离 `user_id = beam_eval_<conv>`；或在 `ingest-batch` 前先 `clear`。
5. **`scope_key` 限制**：当前实现走 `user_global` scope（chat_with_history 的唯一可用 scope），如需 group scope 测试，可在 `eval.common.http_client.call_batch_observe` 加 `scope_type="group" + group_id=<id>`。
6. **评测 LLM 选择**：`judge_beam_single` 通过 `/api/chat_with_history` 走框架默认 Agent；如需换裁判模型，可在 `web_api.py` 中按 user_id 分流（`judge_beam_user` vs `judge_user`）。
7. **答案回灌建议**：BEAM-10M 单 plan 通常 1.2M Token，摄入队列 `_observation_queue` maxsize=10000（observer.py:28），若一次灌 > 5K turn 可被 queue overflow 保护（自动丢最老），本脚本的 `flush=true` 会等 worker 把 buffer 排空再返回。
8. **历史结果兼容**：若 `results/` 下已有部分 `answers_*.json`，probe / judge 默认走"增量模式"，跳过已记录的 `question_id`；加 `--no-resume` 强制重跑。

---

## 十、文件清单（本次改动）

| 路径 | 操作 |
|---|---|
| `gsuid_core/ai_core/memory/observer.py` | 修改：`observe()` + `ObservationRecord` 支持 `timestamp` |
| `gsuid_core/webconsole/chat_with_history_api.py` | 修改：`history[i]['timestamp'/'time'/'ts']` 解析后传入 `observe()` |
| `gsuid_core/webconsole/ai_memory_api.py` | 修改：新增 `POST /api/ai/memory/batch_observe` |
| `eval/common/__init__.py` | 新建：re-export |
| `eval/common/http_client.py` | 新建：`call_chat_with_history / call_send_msg / call_batch_observe / call_clear_user_global / call_rebuild_hiergraph / extract_text_from_response` |
| `eval/common/judge.py` | 新建：`judge_single_answer / parse_judge_response / simple_string_match / judge_beam_single / parse_beam_judge_response` |
| `eval/common/io.py` | 新建：`load_json / dump_json / load_jsonl / load_eval_data / load_existing_answers / read_existing_ids` |
| `eval/longmemeval/run_longmem_eval.py` | 修改：从 `eval.common` import，删除本地副本（约 300 行） |
| `eval/BEAM_10M/run_beam_eval.py` | 新建：6 子命令评测脚本 |
| `eval/BEAM_10M/README.md` | 新建：本文档 |

`eval/BEAM_10M/data/`、原 `eval/BEAM_10M/README.md` 数据集说明文档均保持原样。
