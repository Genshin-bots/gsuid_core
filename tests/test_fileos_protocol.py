"""FileOS P0–P2：句柄协议 / 脱敏去重 / 分页 / RRF / 折叠门。"""

from __future__ import annotations

from gsuid_core.ai_core.planning.tool_output_helper import (
    content_sha256,
    fold_threshold,
    should_fold_for_model,
    should_persist_tool_return,
)
from gsuid_core.ai_core.planning.tool_output_metrics import fileos_metrics
from gsuid_core.ai_core.planning.tool_output_protocol import (
    PersistedHandleCard,
    rrf_fuse,
    load_payload_text,
    format_paginated_body,
)
from gsuid_core.ai_core.planning.tool_output_sanitize import sanitize_for_persist


def test_handle_card_has_render_hint() -> None:
    card = PersistedHandleCard(
        id="to_abc123",
        kind="tool_output",
        mime="text/plain",
        summary="一段摘要",
        size_bytes=9999,
        long_structured=True,
    )
    text = card.format()
    assert "to_abc123" in text
    assert "how_to_read" in text
    assert "read_handle" in text
    assert "long_structured=true" in text
    assert "render_agent" in text


def test_image_card_send_hint() -> None:
    card = PersistedHandleCard(
        id="res_img1",
        kind="image",
        mime="image/png",
        summary="图",
        size_bytes=1,
        long_structured=False,
    )
    text = card.format()
    assert "send_message_by_ai" in text
    assert "long_structured=true" not in text


def test_paginated_body_hint() -> None:
    head = "handle res_x | kind=artifact | mime=text/plain\nsummary: s\n"
    body = "abcdefghijklmnopqrstuvwxyz" * 100
    page = format_paginated_body(
        head=head,
        text=body,
        offset=0,
        limit=50,
        read_hint="read_handle(handle_id, offset, limit)",
    )
    assert "payload:" in page
    assert "【读窗口】" in page
    assert "offset=0" in page
    assert "分页" in page
    assert "read_handle" in page
    page2 = format_paginated_body(head=head, text=body, offset=50, limit=50)
    assert "offset=50" in page2
    # 非周期内容：两页 payload 起点不同
    p0 = page.split("payload:\n", 1)[1][:20]
    p1 = page2.split("payload:\n", 1)[1][:20]
    assert p0 != p1


def test_load_payload_inline() -> None:
    text, err = load_payload_text(payload_inline="hello", payload_path="")
    assert err is None
    assert text == "hello"


def test_sanitize_redacts_secrets() -> None:
    raw = "token sk-abcdefghijklmnopqrstuv password=supersecret123 Bearer abcdefghijklmnop"
    clean, n = sanitize_for_persist(raw)
    assert n >= 1
    assert "sk-abcdefghijklmnopqrstuv" not in clean
    assert "[REDACTED]" in clean


def test_sanitize_keeps_bare_checksum_hex() -> None:
    digest = "a" * 64
    clean, n = sanitize_for_persist(f"file checksum body {digest} ok")
    assert digest in clean
    assert n == 0


def test_sanitize_redacts_labeled_hex_secret() -> None:
    secret_hex = "b" * 64
    clean, n = sanitize_for_persist(f"api_key={secret_hex}")
    assert secret_hex not in clean
    assert "[REDACTED]" in clean
    assert n >= 1


def test_content_hash_stable() -> None:
    assert content_sha256("same body") == content_sha256("same body")
    assert len(content_sha256("x")) == 64


def test_should_persist_and_fold_gates() -> None:
    assert not should_persist_tool_return("web_search_tool", "short")
    assert should_persist_tool_return("web_search_tool", "x" * 900)
    pending = "⏳ 子任务后台执行中（已同步等 5s，将自动回灌）。" + ("y" * 900)
    assert not should_persist_tool_return("create_subagent", pending)
    # 只读回读工具：已是 artifact/FileOS 真身，禁止再落 tool_output
    long_read = "artifact body " * 200
    assert not should_persist_tool_return("artifact_get", long_read)
    assert not should_persist_tool_return("artifact_get_recent", long_read)
    assert not should_persist_tool_return("read_handle", long_read)
    assert not should_persist_tool_return("read_persisted_output", long_read)
    # create_subagent 永不折叠
    assert not should_fold_for_model("x" * 5000, tool_name="create_subagent")
    assert should_fold_for_model("x" * 1500, tool_name="web_search_tool")
    assert fold_threshold(is_group=True) < fold_threshold(is_group=False)
    assert should_fold_for_model("x" * 950, tool_name="web_search_tool", is_group=True)
    assert not should_fold_for_model("x" * 950, tool_name="web_search_tool", is_group=False)


def test_rrf_fuse() -> None:
    a = ["id1", "id2", "id3"]
    b = ["id2", "id4", "id1"]
    fused = rrf_fuse([a, b], limit=3)
    assert fused[0] in ("id1", "id2")  # 双路都有的优先
    assert "id2" in fused
    assert len(fused) <= 3


def test_metrics_increment() -> None:
    before = fileos_metrics.snapshot()
    fileos_metrics.inc_write(100, redacted=2)
    fileos_metrics.inc_dedup()
    fileos_metrics.inc_fold()
    after = fileos_metrics.snapshot()
    assert after["writes"] == before["writes"] + 1
    assert after["dedup_hits"] == before["dedup_hits"] + 1
    assert after["folds"] == before["folds"] + 1
