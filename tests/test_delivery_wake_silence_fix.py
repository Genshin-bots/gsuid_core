from gsuid_core.ai_core.agent_run.settle import (
    _looks_like_report_speech,
    _correction_is_deliverable,
)
from gsuid_core.ai_core.control.corrections import framework_idle_deliver_directive
from gsuid_core.ai_core.agent_run.speech_policy import (
    claims_premature_delivery,
    looks_like_deliver_progress,
    should_block_user_visible_text,
)


def test_claims_premature_delivery_with_and_without_credentials() -> None:
    # 历史防御仍有效：无任何产物/工具凭据时，吹嘘已完成仍然拦截
    assert claims_premature_delivery("查完了，结果如下")
    assert claims_premature_delivery("弄好了，请查看")
    assert claims_premature_delivery("做好了，你看")

    # 凭据豁免：有回灌唤醒/事实包/本轮已调工具时放行真实汇报
    assert not claims_premature_delivery("查完了，结果如下", is_delivery_wake=True)
    assert not claims_premature_delivery("弄好了，请查看", has_fact_pack=True)
    assert not claims_premature_delivery("做好了，你看", tool_called_this_run=True)


def test_framework_deliver_short_fact_vs_dense_report() -> None:
    # 场景1：轻量短事实（长度超过40字，但不是密集大报表），新规则必须放行
    short_version_fact = "查到了，当前运行环境组件版本是 0.12.3（编译自 x86_64 Linux），装在系统目录。"
    assert len(short_version_fact) > 40
    blk, why = should_block_user_visible_text(
        "framework_deliver",
        short_version_fact,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert not blk
    assert why == "ok"

    # 场景2：进度废话/敷衍拖延句，即便未超长也必须拦截
    progress_excuse = "还在弄，别催我了……"
    blk, why = should_block_user_visible_text(
        "framework_deliver",
        progress_excuse,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk
    assert why == "deliver_before_send"

    # 场景3：密集多列大表格（含密集竖线），必须拦截以促使出图
    dense_table = "| 列A | 列B | 列C |\n| :--- | :--- | :--- |\n| 数据1 | 数据2 | 数据3 |\n| 数据4 | 数据5 | 数据6 |\n"
    blk, why = should_block_user_visible_text(
        "framework_deliver",
        dense_table,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk
    assert why == "deliver_before_send"


def test_correction_is_deliverable_delivery_wake_awareness() -> None:
    text = "查完了，已为你确认环境组件版本为 0.12.3。"
    # 普通状态下带有「查完了」不可交付
    assert not _correction_is_deliverable(text, is_delivery_wake=False)
    # 回灌状态下带有「查完了」属正常交付
    assert _correction_is_deliverable(text, is_delivery_wake=True)


def test_framework_idle_deliver_directive_content() -> None:
    d = framework_idle_deliver_directive()
    assert d.kind == "correction"
    assert d.reason_code == "framework_idle_deliver"
    # 不得向模型硬性下发输出 SILENCE 指令，应明确区分简短事实与复杂长表格
    assert "<SILENCE>" not in d.observation
    assert "简短事实" in d.observation
    assert "render_agent" in d.observation


def test_settle_decision_features_on_short_fact() -> None:
    # 模拟任务回灌交付文本（约60字）
    sample_fact = "查到了，当前系统版本为 3.12.13，服务运行状态正常。"
    assert len(sample_fact) > 20
    is_dense_report = (_looks_like_report_speech(sample_fact) and len(sample_fact.strip()) >= 160) or (
        sample_fact.count("|") >= 6 and "\n" in sample_fact
    )
    is_progress_meta = looks_like_deliver_progress(sample_fact)

    # 既不是密集报表又不是进度元话语，结算时判定为合法短事实直接放行
    assert not is_dense_report
    assert not is_progress_meta


def test_settle_decision_features_on_dense_table_and_progress() -> None:
    progress_text = "还在弄呢别催我了……"
    assert looks_like_deliver_progress(progress_text)

    dense_table = "| 列1 | 列2 | 列3 |\n| --- | --- | --- |\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |\n"
    is_dense_report = (_looks_like_report_speech(dense_table) and len(dense_table.strip()) >= 160) or (
        dense_table.count("|") >= 6 and "\n" in dense_table
    )
    assert is_dense_report
