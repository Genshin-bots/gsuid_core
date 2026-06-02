"""RAG 远程 Embedding / Qdrant Upsert 413 退避重试策略的单元测试"""

import pytest

from gsuid_core.ai_core.rag.base import (
    _is_413_error,
    embed_texts_with_backoff,
    upsert_points_with_backoff,
)

# ======================== 辅助 fixture ========================


@pytest.fixture(autouse=True)
def _reset_global_cache():
    """每个测试前后重置全局缓存，防止测试间污染"""
    import gsuid_core.ai_core.rag.base as _base

    _base._cached_embed_bs = 0
    _base._cached_upsert_bs = 0
    yield
    _base._cached_embed_bs = 0
    _base._cached_upsert_bs = 0


# ======================== _is_413_error ========================


class _Fake413Error(Exception):
    """模拟 httpx.HTTPStatusError 的 413 结构化异常"""

    def __init__(self, msg: str = "", status_code: int | None = None, response_status: int | None = None):
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code
        if response_status is not None:

            class _Resp:
                def __init__(self, code):
                    self.status_code = code

            self.response = _Resp(response_status)


class TestIs413Error:
    """_is_413_error 的识别测试"""

    def test_status_code_int(self):
        assert _is_413_error(_Fake413Error(status_code=413))
        assert not _is_413_error(_Fake413Error(status_code=500))

    def test_status_code_str(self):
        e = Exception("bad request")
        e.code = "413"  # type: ignore[attr-defined]
        assert _is_413_error(e)

    def test_http_status_attr(self):
        e = Exception("too large")
        e.http_status = 413  # type: ignore[attr-defined]
        assert _is_413_error(e)

    def test_httpx_response(self):
        assert _is_413_error(_Fake413Error(response_status=413))
        assert not _is_413_error(_Fake413Error(response_status=500))

    def test_text_payload_too_large(self):
        assert _is_413_error(Exception("Payload Too Large"))

    def test_text_request_entity_too_large(self):
        assert _is_413_error(Exception("Request Entity Too Large"))

    def test_text_413_in_message(self):
        assert _is_413_error(Exception("HTTP 413 error from API"))

    def test_text_context_length(self):
        assert _is_413_error(Exception("context length exceeded"))

    def test_text_too_many_tokens(self):
        assert _is_413_error(Exception("too many tokens"))

    def test_non_413_error(self):
        assert not _is_413_error(Exception("connection timeout"))
        assert not _is_413_error(Exception("500 Internal Server Error"))
        assert not _is_413_error(RuntimeError("dimension mismatch"))

    def test_case_insensitive(self):
        assert _is_413_error(Exception("PAYLOAD TOO LARGE"))
        assert _is_413_error(Exception("Request Entity Too Large"))


# ======================== embed_texts_with_backoff ========================


class TestEmbedTextsWithBackoff:
    """embed_texts_with_backoff 的退避逻辑测试"""

    @pytest.mark.anyio
    async def test_empty_texts(self):
        result = await embed_texts_with_backoff([], None)  # type: ignore[arg-type]
        assert result == []

    @pytest.mark.anyio
    async def test_single_batch_success(self):
        async def fake_embed(texts):
            return [[1.0, 2.0] for _ in texts]

        texts = ["a", "b", "c"]
        result = await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=10)
        assert len(result) == 3
        assert all(v is not None for v in result)
        assert result[0] == [1.0, 2.0]

    @pytest.mark.anyio
    async def test_413_halves_batch_size(self):
        """遇到 413 后批大小减半直到成功"""
        call_counts: list[int] = []

        async def fake_embed(texts):
            call_counts.append(len(texts))
            if len(texts) > 2:
                raise Exception("413 Payload Too Large")
            return [[1.0] for _ in texts]

        texts = ["a", "b", "c", "d"]
        result = await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=4, log_tag="Test")
        # 第一次 4 条 → 413 → 减半到 2 → 成功两次
        assert len(result) == 4
        assert all(v is not None for v in result)
        assert call_counts == [4, 2, 2]

    @pytest.mark.anyio
    async def test_413_down_to_one(self):
        """连续 413 直到批大小为 1"""
        call_counts: list[int] = []

        async def fake_embed(texts):
            call_counts.append(len(texts))
            if len(texts) > 1:
                raise Exception("413 Payload Too Large")
            return [[float(i)] for i in range(len(texts))]

        texts = ["a", "b", "c"]
        result = await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=3, log_tag="Test")
        # 3→413→1.5→1, 1, 1
        assert len(result) == 3
        assert all(v is not None for v in result)
        assert call_counts == [3, 1, 1, 1]

    @pytest.mark.anyio
    async def test_bs1_still_413_skips(self):
        """bs=1 仍 413 → 返回 None 并继续"""
        call_indices: list[str] = []

        async def fake_embed(texts):
            call_indices.extend(texts)
            if texts[0] == "b":
                raise Exception("413 Payload Too Large")
            return [[1.0] for _ in texts]

        texts = ["a", "b", "c"]
        result = await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=1, log_tag="Test")
        assert len(result) == 3
        assert result[0] == [1.0]
        assert result[1] is None  # "b" 被跳过
        assert result[2] == [1.0]

    @pytest.mark.anyio
    async def test_non_413_raises(self):
        """非 413 异常直接抛出，不退避"""

        async def fake_embed(texts):
            raise RuntimeError("connection timeout")

        texts = ["a", "b"]
        with pytest.raises(RuntimeError, match="connection timeout"):
            await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=10, log_tag="Test")

    @pytest.mark.anyio
    async def test_result_order_matches_input(self):
        """结果顺序与输入一致"""
        values = iter([[1.0], [2.0], [3.0]])

        async def fake_embed(texts):
            return [next(values) for _ in texts]

        texts = ["a", "b", "c"]
        result = await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=2)
        assert result == [[1.0], [2.0], [3.0]]

    @pytest.mark.anyio
    async def test_return_length_mismatch_raises(self):
        """embed 函数返回数量不匹配时抛出 RuntimeError"""

        async def fake_embed(texts):
            return [[1.0]]  # 只返回 1 个

        texts = ["a", "b", "c"]
        with pytest.raises(RuntimeError, match="批量嵌入返回数量异常"):
            await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=10, log_tag="Test")

    @pytest.mark.anyio
    async def test_413_caches_reduced_batch_size(self):
        """413 减半后，全局缓存应记录新的批大小"""
        import gsuid_core.ai_core.rag.base as _base

        call_counts: list[int] = []

        async def fake_embed(texts):
            call_counts.append(len(texts))
            if len(texts) > 2:
                raise Exception("413 Payload Too Large")
            return [[1.0] for _ in texts]

        texts = ["a", "b", "c", "d"]
        await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=4, log_tag="Test")
        # 413 后减半到 2，全局缓存应为 2
        assert _base._cached_embed_bs == 2

    @pytest.mark.anyio
    async def test_cached_bs_used_on_next_call(self):
        """后续调用应使用缓存的批大小，无需再次 413"""
        import gsuid_core.ai_core.rag.base as _base

        _base._cached_embed_bs = 2

        call_counts: list[int] = []

        async def fake_embed(texts):
            call_counts.append(len(texts))
            return [[1.0] for _ in texts]

        texts = ["a", "b", "c", "d"]
        # 不传 initial_batch_size，应使用缓存值 2
        result = await embed_texts_with_backoff(texts, fake_embed, log_tag="Test")
        assert len(result) == 4
        assert call_counts == [2, 2]

    @pytest.mark.anyio
    async def test_explicit_bs_overrides_cache(self):
        """显式传入 initial_batch_size 应覆盖缓存"""
        import gsuid_core.ai_core.rag.base as _base

        _base._cached_embed_bs = 2

        call_counts: list[int] = []

        async def fake_embed(texts):
            call_counts.append(len(texts))
            return [[1.0] for _ in texts]

        texts = ["a", "b", "c"]
        result = await embed_texts_with_backoff(texts, fake_embed, initial_batch_size=10, log_tag="Test")
        assert len(result) == 3
        assert call_counts == [3]  # 用 10 但只有 3 条，所以一次 3 条


# ======================== upsert_points_with_backoff ========================


class TestUpsertPointsWithBackoff:
    """upsert_points_with_backoff 的退避逻辑测试"""

    @pytest.mark.anyio
    async def test_empty_points(self):
        result = await upsert_points_with_backoff([], None)  # type: ignore[arg-type]
        assert result == 0

    @pytest.mark.anyio
    async def test_single_batch_success(self):
        upserted: list[int] = []

        async def fake_upsert(batch):
            upserted.append(len(batch))

        points = list(range(5))
        result = await upsert_points_with_backoff(points, fake_upsert, initial_batch_size=10, log_tag="Test")
        assert result == 5
        assert upserted == [5]

    @pytest.mark.anyio
    async def test_413_halves_batch_size(self):
        call_counts: list[int] = []

        async def fake_upsert(batch):
            call_counts.append(len(batch))
            if len(batch) > 2:
                raise Exception("413 Payload Too Large")

        points = list(range(4))
        result = await upsert_points_with_backoff(points, fake_upsert, initial_batch_size=4, log_tag="Test")
        assert result == 4
        assert call_counts == [4, 2, 2]

    @pytest.mark.anyio
    async def test_bs1_still_413_skips(self):
        """bs=1 仍 413 → 跳过该条"""
        call_sizes: list[int] = []

        async def fake_upsert(batch):
            call_sizes.append(len(batch))
            if batch[0] == 1:  # point[1] 触发 413
                raise Exception("413 Payload Too Large")

        points = list(range(4))  # [0, 1, 2, 3]
        result = await upsert_points_with_backoff(points, fake_upsert, initial_batch_size=1, log_tag="Test")
        assert result == 3  # 0, 2, 3 成功

    @pytest.mark.anyio
    async def test_non_413_raises(self):
        async def fake_upsert(batch):
            raise RuntimeError("qdrant connection refused")

        points = list(range(3))
        with pytest.raises(RuntimeError, match="qdrant connection refused"):
            await upsert_points_with_backoff(points, fake_upsert, initial_batch_size=10, log_tag="Test")

    @pytest.mark.anyio
    async def test_413_caches_reduced_batch_size(self):
        """413 减半后，全局缓存应记录新的批大小"""
        import gsuid_core.ai_core.rag.base as _base

        async def fake_upsert(batch):
            if len(batch) > 2:
                raise Exception("413 Payload Too Large")

        points = list(range(4))
        await upsert_points_with_backoff(points, fake_upsert, initial_batch_size=4, log_tag="Test")
        assert _base._cached_upsert_bs == 2

    @pytest.mark.anyio
    async def test_cached_bs_used_on_next_call(self):
        """后续调用应使用缓存的批大小"""
        import gsuid_core.ai_core.rag.base as _base

        _base._cached_upsert_bs = 2

        call_counts: list[int] = []

        async def fake_upsert(batch):
            call_counts.append(len(batch))

        points = list(range(4))
        result = await upsert_points_with_backoff(points, fake_upsert, log_tag="Test")
        assert result == 4
        assert call_counts == [2, 2]
