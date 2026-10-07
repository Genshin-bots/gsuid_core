"""`core更新记录`：changelog 解析、作者邮箱推导、卡片带上 commit 头像。"""

from __future__ import annotations

from typing import Sequence
from pathlib import Path

import pytest

from gsuid_core.buildin_plugins.core_command.core_update_history import authors, template, changelog
from gsuid_core.buildin_plugins.core_command.core_update_history.changelog import (
    VersionRef,
    CommitAuthor,
    ChangelogEntry,
    ChangelogVersion,
    pick_recent,
    group_entries,
    list_versions,
)

_SHA_LINK = "[`9e4ad01b`](https://github.com/Genshin-bots/gsuid_core/commit/9e4ad01b)"
_AVATAR = "data:image/png;base64,QQ=="
_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _version(entry: ChangelogEntry) -> ChangelogVersion:
    return ChangelogVersion(
        version="0.11.0",
        released=False,
        subtitle="解帧失败保持连接",
        date="2026-10-08",
        commit_count=1,
        lead=(),
        entries=(entry,),
        groups=group_entries((entry,)),
        authors=entry.authors,
    )


def _ref(version: str, *, released: bool, commit_count: int = 1) -> VersionRef:
    stem = version if released else f"{version}-unreleased"
    return VersionRef(
        version=version,
        released=released,
        path=Path(f"{stem}.md"),
        summary="",
        date="",
        commit_count=commit_count,
    )


def test_pick_recent_skips_same_version_unreleased() -> None:
    refs = (
        _ref("0.11.0", released=False),
        _ref("0.11.0", released=True),
        _ref("0.10.8", released=True),
    )
    picked = pick_recent(refs)
    assert [ref.version for ref in picked] == ["0.11.0", "0.10.8"]
    assert picked[0].released is False
    assert picked[1].released is True


def test_pick_recent_two_released_versions() -> None:
    refs = (_ref("0.11.0", released=True), _ref("0.10.8", released=True), _ref("0.10.7", released=True))
    picked = pick_recent(refs)
    assert [(ref.version, ref.released) for ref in picked] == [("0.11.0", True), ("0.10.8", True)]


def test_pick_recent_next_version_unreleased() -> None:
    refs = (_ref("0.12.0", released=False), _ref("0.11.0", released=True))
    picked = pick_recent(refs)
    assert [(ref.version, ref.released) for ref in picked] == [("0.12.0", False), ("0.11.0", True)]


@pytest.mark.parametrize(
    ("count", "want"),
    [
        (6, ["0.11.0", "0.10.8"]),
        (7, ["0.11.0"]),
    ],
)
def test_pick_recent_omits_previous_when_latest_is_long(count: int, want: list[str]) -> None:
    refs = (
        _ref("0.11.0", released=True, commit_count=count),
        _ref("0.10.8", released=True),
    )
    assert [ref.version for ref in pick_recent(refs)] == want


def test_living_version_appears_once() -> None:
    refs = list_versions()
    living = [ref for ref in refs if ref.version == "0.11.0"]
    assert len(living) == 1
    assert living[0].released is True
    recent = pick_recent(refs)
    versions = [ref.version for ref in recent]
    assert len(versions) == len(set(versions))
    assert recent[0].version == "0.11.0"
    assert living[0].commit_count > changelog._RECENT_PREV_MAX_COMMITS
    assert len(recent) == 1


def test_login_from_noreply_email() -> None:
    assert authors.login_from_email("149057504+ACHamster@users.noreply.github.com") == "ACHamster"
    assert authors.login_from_email("66853113+pre-commit-ci[bot]@users.noreply.github.com") == ("pre-commit-ci[bot]")
    assert authors.login_from_email("ishkong@users.noreply.github.com") == "ishkong"
    assert authors.login_from_email("444835641@qq.com") == ""


def test_group_entries_orders_optimize_first() -> None:
    def _item(label: str) -> ChangelogEntry:
        return ChangelogEntry(emoji="•", label=label, text=label, commits=())

    groups = group_entries(
        (
            _item("修复"),
            _item("依赖"),
            _item("新增"),
            _item("安全"),
            _item("性能"),
            _item("调整"),
        )
    )
    assert [group.label for group in groups] == ["优化", "新增", "调整", "安全", "修复", "依赖"]


def test_parse_entry_keeps_sha_and_tag() -> None:
    entry = changelog._parse_entry(f"**🐛 修复** 解帧失败跳过。{_SHA_LINK}")
    assert entry.label == "修复"
    assert entry.commits == ("9e4ad01b",)
    assert "github.com" not in entry.text
    assert entry.authors == ()


def test_parse_entry_dependency_emoji() -> None:
    body = (
        "**⬆️ 依赖** pre-commit-ci 把 `ruff-pre-commit` 升到 `v0.16.10`。"
        "[`80a00b6a`](https://github.com/Genshin-bots/gsuid_core/commit/80a00b6a)"
    )
    entry = changelog._parse_entry(body)
    assert entry.emoji == "⬆️"
    assert entry.label == "依赖"
    assert entry.commits == ("80a00b6a",)


def test_item_html_shows_author_icon() -> None:
    author = CommitAuthor(name="KimigaiiWuyi", login="KimigaiiWuyi", avatar_uri=_AVATAR)
    entry = ChangelogEntry(
        emoji="🐛",
        label="修复",
        text="解帧失败跳过",
        commits=("9e4ad01b",),
        authors=(author,),
    )
    html = template.build_version_html(_version(entry), is_current=False)
    assert _AVATAR in html
    assert 'title="KimigaiiWuyi"' in html
    title_at = html.find("解帧失败跳过")
    sep_at = html.find(" | ")
    sha_at = html.find("9e4ad01b")
    assert 0 <= title_at < sep_at < sha_at
    assert "1 位贡献者" in html


def test_item_html_escapes_author_name() -> None:
    author = CommitAuthor(name="<img src=x onerror=alert(1)>", login="", avatar_uri="")
    entry = ChangelogEntry(
        emoji="🐛",
        label="修复",
        text="别名表生效",
        commits=("57a96718",),
        authors=(author,),
    )
    html = template.build_version_html(_version(entry), is_current=False)
    assert "<img src=x onerror=alert(1)>" not in html
    assert "av-letter" in html


def test_github_commit_payload_reads_login() -> None:
    name, login, avatar = authors._parse_commit_payload(
        {
            "commit": {"author": {"name": "Mimo", "email": "3853125761@qq.com"}},
            "author": {
                "login": "MimoKit",
                "avatar_url": "https://avatars.githubusercontent.com/u/278909251?v=4",
            },
        }
    )
    assert name == "Mimo"
    assert login == "MimoKit"
    assert "278909251" in avatar


def test_github_commit_payload_without_account() -> None:
    name, login, avatar = authors._parse_commit_payload({"commit": {"author": {"name": "Mimo"}}, "author": None})
    assert name == "Mimo"
    assert login == ""
    assert avatar == ""


@pytest.mark.anyio
async def test_git_authors_reads_each_requested_commit() -> None:
    rows = await authors._git_authors(_REPO_ROOT, ["6c733658", "9e4ad01b", "7f1bee79"])
    assert "6c733658" in rows
    assert "9e4ad01b" in rows
    assert "7f1bee79" in rows
    assert rows["6c733658"][1].endswith("@users.noreply.github.com")
    assert "Wuyi" in rows["9e4ad01b"][0] or rows["9e4ad01b"][1].endswith("@qq.com")
    assert rows["7f1bee79"][0] == "Mimo"


@pytest.mark.anyio
async def test_attach_authors_fills_icons(monkeypatch: pytest.MonkeyPatch) -> None:
    author = CommitAuthor(name="You Wu", login="ACHamster", avatar_uri=_AVATAR)

    async def fake_resolve(shas: Sequence[str]) -> dict[str, CommitAuthor]:
        return {shas[0][:8]: author}

    monkeypatch.setattr(authors, "resolve_authors", fake_resolve)
    entry = ChangelogEntry(
        emoji="✨",
        label="新增",
        text="MCP 工具可带回图片",
        commits=("6c733658",),
    )
    painted = await authors.attach_authors((_version(entry),))
    filled = painted[0].entries[0].authors
    assert len(filled) == 1
    assert filled[0].login == "ACHamster"
    html = template.build_version_html(painted[0], is_current=False)
    assert _AVATAR in html
    assert "ACHamster" in html
