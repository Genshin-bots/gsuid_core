"""Exa 搜索不传 type。配置里有没有 search_type 都不影响请求。"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest
from exa_py.api import Result, ContentStatus, SearchResponse

import gsuid_core.ai_core.web_fetch as fetch_mod
from gsuid_core.ai_core.web_search import search as search_mod, exa_search as exa_mod
from gsuid_core.ai_core.configs.ai_config import AI_CONFIG, EXA_CONFIG
from gsuid_core.utils.plugins_config.models import GsIntConfig, GsStrConfig, GsListStrConfig


def test_exa_empty_highlights_and_missing_date_stay_blank() -> None:
    blank = Result(url="https://e.example", id="1", highlights=["", "  "], published_date=None)
    assert exa_mod._highlight_text(blank) == ""
    assert exa_mod._published_date(blank.published_date) == ""
    padded = Result(url="https://e.example", id="2", highlights=None, published_date=" 2026-01-02 ")
    assert exa_mod._highlight_text(padded) == ""
    assert exa_mod._published_date(padded.published_date) == "2026-01-02"


def test_exa_falls_back_to_text_when_highlights_empty() -> None:
    no_hl = Result(url="https://e.example", id="1", highlights=None, text="fallback body")
    assert exa_mod._extract_hit_content(no_hl) == "fallback body"
    empty_hl = Result(url="https://e.example", id="2", highlights=["", "  "], text=" fallback ")
    assert exa_mod._extract_hit_content(empty_hl) == "fallback"
    both_empty = Result(url="https://e.example", id="3", highlights=None, text=None)
    assert exa_mod._extract_hit_content(both_empty) == ""


def test_exa_config_omits_search_type() -> None:
    assert "search_type" not in EXA_CONFIG


def test_exa_is_webfetch_option() -> None:
    provider = AI_CONFIG["webfetch_provider"]
    fallback = AI_CONFIG["webfetch_fallback_order"]
    assert isinstance(provider, GsStrConfig)
    assert isinstance(fallback, GsListStrConfig)
    assert "Exa" in provider.options
    assert "Exa" in fallback.options


class _Hit:
    def __init__(self) -> None:
        self.title = "T"
        self.url = "https://e.example"
        self.text = "body"
        self.highlights = ["摘录甲", " 摘录乙 "]
        self.published_date = "2026-01-02"
        self.score = 0.5


class _Resp:
    def __init__(self) -> None:
        self.results = [_Hit()]


class _AsyncExa:
    calls: list[dict[str, object]] = []

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def search(
        self,
        query: str,
        *,
        num_results: int,
        contents: Mapping[str, object],
        **extra: object,
    ) -> _Resp:
        highlights = contents["highlights"]
        assert highlights is True
        _AsyncExa.calls.append(
            {
                "query": query,
                "num_results": num_results,
                "highlights": highlights,
                "extra": extra,
            }
        )
        return _Resp()


class _Cfg:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.writes: list[tuple[str, str]] = []

    def get_config(self, key: str) -> GsStrConfig | GsListStrConfig | GsIntConfig:
        self.reads.append(key)
        if key == "api_key":
            return GsListStrConfig("密钥", "", ["exa-test"])
        if key == "max_results":
            return GsIntConfig("条数", "", 10)
        return GsStrConfig("搜索模式", "", "deep")

    def set_config(self, key: str, value: str) -> bool:
        self.writes.append((key, value))
        return True


@pytest.mark.anyio
async def test_exa_search_ignores_stored_search_type(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _Cfg()
    _AsyncExa.calls = []
    monkeypatch.setattr(exa_mod, "exa_config", cfg)
    monkeypatch.setattr(exa_mod, "AsyncExa", _AsyncExa)
    hits = await exa_mod.exa_search("查询")
    assert hits[0]["title"] == "T"
    assert hits[0]["content"] == "摘录甲\n摘录乙"
    assert hits[0]["published_date"] == "2026-01-02"
    assert cfg.reads == ["api_key", "max_results"]
    assert cfg.writes == []
    assert _AsyncExa.calls == [
        {"query": "查询", "num_results": 10, "highlights": True, "extra": {}},
    ]


def _first_key(pool: list[str]) -> str | None:
    if not pool:
        return None
    return pool[0]


def _contents(text: str | None, status: str) -> SearchResponse[Result]:
    page = Result("https://e.example/a", "https://e.example/a", title="T", text=text)
    return SearchResponse(
        [page],
        None,
        None,
        statuses=[ContentStatus("https://e.example/a", status, "cached")],
    )


class _ContentsExa:
    calls: list[dict[str, object]] = []
    fail_keys: set[str] = set()
    tried: list[str] = []

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def get_contents(self, urls: list[str], *, text: Mapping[str, object]) -> SearchResponse[Result]:
        _ContentsExa.calls.append({"api_key": self.api_key, "urls": urls, "text": dict(text)})
        if self.api_key in _ContentsExa.fail_keys:
            raise httpx.ConnectError("down")
        return _contents("正文", "success")

    async def search(self, query: str, *, num_results: int, contents: Mapping[str, object], **extra: object) -> _Resp:
        _ContentsExa.tried.append(self.api_key)
        if self.api_key in _ContentsExa.fail_keys:
            raise httpx.ConnectError("down")
        return _Resp()


class _TwoKeyCfg(_Cfg):
    def get_config(self, key: str) -> GsStrConfig | GsListStrConfig | GsIntConfig:
        self.reads.append(key)
        if key == "api_key":
            return GsListStrConfig("密钥", "", ["exa-bad", "exa-ok"])
        if key == "max_results":
            return GsIntConfig("条数", "", 10)
        return GsStrConfig("其他", "", "")


@pytest.mark.anyio
async def test_exa_search_retries_next_key_on_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _TwoKeyCfg()
    _ContentsExa.calls = []
    _ContentsExa.tried = []
    _ContentsExa.fail_keys = {"exa-bad"}
    monkeypatch.setattr(exa_mod, "exa_config", cfg)
    monkeypatch.setattr(exa_mod, "AsyncExa", _ContentsExa)
    monkeypatch.setattr(exa_mod, "_select_api_key", _first_key)
    hits = await exa_mod.exa_search("查询")
    assert hits[0]["url"] == "https://e.example"
    assert _ContentsExa.tried == ["exa-bad", "exa-ok"]


class _Box:
    def __init__(self, data: object) -> None:
        self.data = data


class _AiCfg:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def get_config(self, key: str, default: object | None = None) -> _Box:
        if key in self._mapping:
            return _Box(self._mapping[key])
        if default is not None:
            return _Box(default)
        return _Box("")


@pytest.mark.anyio
async def test_web_search_switches_provider_on_exa_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_mod.ai_config,
        "get_config",
        _AiCfg(
            {
                "websearch_provider": "Exa",
                "websearch_lb_strategy": "error_switch",
                "websearch_fallback_order": ["AnySearch"],
            }
        ).get_config,
    )
    monkeypatch.setattr(search_mod, "_provider_configured", lambda _name: True)

    async def _exa(query: str, max_results: int | None = None) -> list[dict[str, str]]:
        raise httpx.ConnectError("down")

    async def _any(query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        return [{"title": "备用", "url": "https://a.example", "content": "ok", "score": 1.0}]

    monkeypatch.setattr(search_mod, "exa_search", _exa)
    monkeypatch.setattr(search_mod, "anysearch_search", _any)
    hits = await search_mod.web_search("查询")
    assert hits[0]["url"] == "https://a.example"


@pytest.mark.anyio
async def test_exa_fetch_returns_text_and_caps_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _Cfg()
    _ContentsExa.calls = []
    _ContentsExa.fail_keys = set()
    monkeypatch.setattr(exa_mod, "exa_config", cfg)
    monkeypatch.setattr(exa_mod, "AsyncExa", _ContentsExa)
    text = await exa_mod.exa_fetch_markdown("https://e.example/a", 40)
    assert text == "正文"
    assert _ContentsExa.calls == [
        {
            "api_key": "exa-test",
            "urls": ["https://e.example/a"],
            "text": {"max_characters": 40},
        }
    ]


@pytest.mark.anyio
async def test_exa_fetch_empty_page_does_not_try_next_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _TwoKeyCfg()

    class _EmptyExa(_ContentsExa):
        async def get_contents(self, urls: list[str], *, text: Mapping[str, object]) -> SearchResponse[Result]:
            _ContentsExa.calls.append({"api_key": self.api_key, "urls": urls, "text": dict(text)})
            return _contents(None, "error")

    _ContentsExa.calls = []
    monkeypatch.setattr(exa_mod, "exa_config", cfg)
    monkeypatch.setattr(exa_mod, "AsyncExa", _EmptyExa)
    monkeypatch.setattr(exa_mod, "_select_api_key", _first_key)
    with pytest.raises(ValueError):
        await exa_mod.exa_fetch_markdown("https://e.example/a", 100)
    assert len(_ContentsExa.calls) == 1


@pytest.mark.anyio
async def test_fetch_falls_back_to_local_when_exa_connect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fetch_mod.ai_config,
        "get_config",
        _AiCfg(
            {
                "webfetch_provider": "exa",
                "webfetch_lb_strategy": "error_switch",
                "webfetch_fallback_order": ["local"],
            }
        ).get_config,
    )

    async def _exa(url: str, max_chars: int) -> str:
        raise httpx.ConnectError("down")

    async def _local(url: str, timeout: int | None = None, max_length: int | None = None) -> str:
        return "local-body"

    monkeypatch.setattr(fetch_mod, "exa_fetch_markdown", _exa)
    monkeypatch.setattr(fetch_mod, "_fetch_via_local", _local)
    text = await fetch_mod.fetch_webpage_as_markdown("https://e.example/a", timeout=5, max_length=1000)
    assert text == "local-body"
