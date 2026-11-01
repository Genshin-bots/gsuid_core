"""Git 人格仓库：克隆整包、按 manifest 结构装成人格、跟随远端 commit 同步。

仓库是真值源：人格目录里的 persona.md / 资源 / config.json 由仓库拥有，
本地只允许改 scope / target_groups（启用范围是部署侧状态，同步时保留）。
表情包向量视为派生数据，导入新记录时直接补写 Qdrant。
"""

from __future__ import annotations

import os
import json
import time
import shutil
import asyncio
from typing import Optional, TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import AI_CORE_PATH, get_res_path
from gsuid_core.utils.path_safety import (
    PathEscapeError,
    safe_join,
    is_safe_filename,
    validate_install_source_url,
)
from gsuid_core.ai_core.persona.bundle import (
    _IMAGE_MIME,
    BundleError,
    _pack_dirs,
    _image_files,
    import_bundle_dir,
)
from gsuid_core.ai_core.persona.models import SUPPORTED_AUDIO_FORMATS
from gsuid_core.ai_core.persona.persona import Persona

PERSONA_REPO_PATH = get_res_path(AI_CORE_PATH / "persona_repos")
PERSONA_STORE_LIB = "https://docs.sayu-bot.com/persona_list.json"
SOURCE_MARKER = "persona_store.json"

# 本地管理员仍可改的键：启用范围是部署状态，同步时保留不被仓库覆盖。
LOCAL_OVERRIDE_KEYS = ("scope", "target_groups")

# config.json 之外的仓库托管文件；更新时仓库缺了就删掉本地的。
_MANAGED_OPTIONAL_FILES = (
    "avatar.png",
    "image.png",
    "persona.json",
    "voice_anchor.txt",
    "appearance.txt",
)


class PersonaSource(TypedDict):
    source: str
    repo_url: str
    repo_name: str
    branch: str
    bundle_dir: str
    commit: str
    installed_at: float
    packs: list[str]


def _source_path(persona_name: str) -> Path:
    return Persona(persona_name).dir_path / SOURCE_MARKER


def read_source(persona_name: str) -> Optional[PersonaSource]:
    """读出人格目录里的来源标记；不是 git 导入的返回 None。"""
    if not is_safe_filename(persona_name):
        return None
    path = _source_path(persona_name)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("source") != "git":
        return None
    repo_url = raw.get("repo_url")
    repo_name = raw.get("repo_name")
    if not isinstance(repo_url, str) or not isinstance(repo_name, str):
        return None
    branch = raw.get("branch")
    bundle_dir = raw.get("bundle_dir")
    commit = raw.get("commit")
    installed_at = raw.get("installed_at")
    packs = raw.get("packs")
    return PersonaSource(
        source="git",
        repo_url=repo_url,
        repo_name=repo_name,
        branch=branch if isinstance(branch, str) else "main",
        bundle_dir=bundle_dir if isinstance(bundle_dir, str) else "",
        commit=commit if isinstance(commit, str) else "",
        installed_at=installed_at if isinstance(installed_at, (int, float)) else 0.0,
        packs=[p for p in packs if isinstance(p, str)] if isinstance(packs, list) else [],
    )


def is_git_managed(persona_name: str) -> bool:
    return read_source(persona_name) is not None


def write_source(persona_name: str, source: PersonaSource) -> None:
    _source_path(persona_name).write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")


def drop_source(persona_name: str) -> None:
    """复制出可编辑副本时摘除来源标记，让副本脱离仓库同步。"""
    path = _source_path(persona_name)
    if path.is_file():
        path.unlink()


def installed_git_personas() -> dict[str, PersonaSource]:
    """扫描人格目录，返回 {人格名: 来源信息}。persona.md 丢了也照认标记。"""
    from gsuid_core.ai_core.resource import PERSONA_PATH

    found: dict[str, PersonaSource] = {}
    if not PERSONA_PATH.is_dir():
        return found
    for item in PERSONA_PATH.iterdir():
        if not item.is_dir():
            continue
        src = read_source(item.name)
        if src is not None:
            found[item.name] = src
    return found


def is_repo_pack(pack_name: str) -> bool:
    """包名出现在任一 git 人格的仓库清单里，则包内容随仓库同步，不允许手动改。"""
    return any(pack_name in src["packs"] for src in installed_git_personas().values())


def find_bundle_root(repo_dir: Path) -> Path:
    """整包根可能在仓库根，也可能在一级子目录（如 persona_sayu/persona_sayu/）。"""
    if (repo_dir / "manifest.json").is_file():
        return repo_dir
    candidates = [
        path
        for path in repo_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / "manifest.json").is_file()
    ]
    candidates.sort(key=lambda path: path.name)
    if not candidates:
        raise BundleError("仓库内未找到 manifest.json，不是人格整包仓库")
    return candidates[0]


def _repo_dir(repo_name: str) -> Optional[Path]:
    if not is_safe_filename(repo_name):
        return None
    try:
        return safe_join(PERSONA_REPO_PATH, repo_name)
    except PathEscapeError:
        return None


def _remove_repo_dir(path: Path) -> None:
    """删除克隆目录；Windows 上 .git 对象文件只读，chmod 后重试再删。"""
    import stat

    def _onerror(func, p, _exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def _clone_target(url: str) -> tuple[str, Optional[str]]:
    """返回 (clone_url, fallback_url)；fallback 仅镜像模式给到 GitHub 原链。"""
    from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
    from gsuid_core.utils.plugins_update.git_mirror import (
        SSH_GITHUB_TEMPLATE,
        _is_ssh_mode,
        _is_proxy_prefix,
        _extract_repo_name,
        _extract_owner_repo,
    )

    git_mirror: str = core_plugins_config.get_config("GitMirror").data
    if not git_mirror:
        return url, None
    if _is_ssh_mode(git_mirror):
        owner_repo = _extract_owner_repo(url)
        if owner_repo is not None and "github.com" in url:
            return SSH_GITHUB_TEMPLATE.format(owner=owner_repo[0], repo=owner_repo[1]), None
        return url, None
    if _is_proxy_prefix(git_mirror):
        return f"{git_mirror.rstrip('/')}/{url}", None
    repo_name = _extract_repo_name(url)
    if repo_name is None:
        return url, None
    return f"{git_mirror.rstrip('/')}/{repo_name}", url


def _snapshot_overrides(persona_name: str) -> dict[str, str | list[str]]:
    from gsuid_core.ai_core.persona.config import persona_config_manager

    cfg = persona_config_manager.get_config(persona_name)
    return {
        "scope": cfg.get_config("scope").data,
        "target_groups": list(cfg.get_config("target_groups").data),
    }


def _restore_overrides(persona_name: str, saved: dict[str, str | list[str]]) -> None:
    """config.json 被仓库覆盖后，把本地的 scope / target_groups 写回去。"""
    from gsuid_core.ai_core.persona.config import persona_config_manager

    persona_config_manager.drop_cache(persona_name)
    cfg = persona_config_manager.get_config(persona_name)
    for key in LOCAL_OVERRIDE_KEYS:
        value = saved.get(key)
        if value is not None and cfg.get_config(key).data != value:
            cfg.set_config(key, value)


def _reconcile_files(persona: Persona, root: Path) -> None:
    """仓库里删掉的可选文件，本地同步删掉；config.json 属运行时状态，不动。"""
    for name in _MANAGED_OPTIONAL_FILES:
        path = persona.dir_path / name
        if path.is_file() and not (root / name).is_file():
            path.unlink()
    for ext in SUPPORTED_AUDIO_FORMATS:
        path = persona.dir_path / f"audio{ext}"
        if path.is_file() and not (root / path.name).is_file():
            path.unlink()


def _expected_meme_ids(pack_dir: Path) -> set[str]:
    from gsuid_core.ai_core.meme.library import compute_meme_id
    from gsuid_core.ai_core.meme.normalize import prepare_meme_image

    ids: set[str] = set()
    for image_path in _image_files(pack_dir):
        data = image_path.read_bytes()
        mime = _IMAGE_MIME.get(image_path.suffix.lower(), "image/jpeg")
        ids.add(compute_meme_id(prepare_meme_image(data, mime).data))
    return ids


async def _reconcile_packs(persona_name: str, src: PersonaSource, root: Path) -> None:
    """对齐绑定与成员：仓库移除的包解除绑定；独占包移除仓库已删的成员。"""
    from gsuid_core.ai_core.meme.pack_model import PackStore
    from gsuid_core.ai_core.meme.database_model import AiMemeRecord

    repo_pack_names = {d.name for d in _pack_dirs(root)}
    bound_ids = await PackStore.pack_ids_for_persona(persona_name)
    keep: list[str] = []
    for pack_id in bound_ids:
        pack = await PackStore.get_pack(pack_id)
        if pack is None:
            continue
        # 非仓库来源的包（默认包等）或仓库里还存在的包，保留
        if pack.name not in src["packs"] or pack.name in repo_pack_names:
            keep.append(pack_id)
    if len(keep) != len(bound_ids):
        await PackStore.replace_binds(persona_name, keep)

    summaries = {row.pack_id: row for row in await PackStore.list_summaries()}
    for pack_dir in _pack_dirs(root):
        pack = await PackStore.get_by_name(pack_dir.name)
        if pack is None:
            continue
        row = summaries.get(pack.pack_id)
        if row is None or any(p != persona_name for p in row.bound_personas):
            continue
        expected = await asyncio.to_thread(_expected_meme_ids, pack_dir)
        current = set(await PackStore.member_meme_ids(pack.pack_id))
        # 只清整包来源或已无记录的成员；同名包被接管时用户自己加的图不删
        stale: list[str] = []
        for meme_id in sorted(current - expected):
            record = await AiMemeRecord.get_by_meme_id(meme_id)
            if record is None or record.source_group == "persona_bundle":
                stale.append(meme_id)
        if stale:
            await PackStore.remove_members(pack.pack_id, stale)


def _bundle_dir_rel(repo_dir: Path, root: Path) -> str:
    rel = root.relative_to(repo_dir)
    return "" if str(rel) == "." else rel.as_posix()


async def install_persona_from_git(
    url: str,
    branch: Optional[str] = None,
) -> tuple[Optional[dict[str, object]], Optional[str]]:
    """克隆 git 仓库并按整包结构导入人格。返回 (结果, 错误文案)。"""
    from gsuid_core.utils.plugins_update._plugins import _is_mirror_not_synced_error
    from gsuid_core.utils.plugins_update.git_async import (
        git_clone,
        git_set_remote_url,
        git_get_current_commit,
    )
    from gsuid_core.utils.plugins_update.git_mirror import _extract_repo_name

    url = (url or "").strip()
    err = validate_install_source_url(url)
    if err is not None:
        return None, err
    repo_name = _extract_repo_name(url)
    if repo_name is None or not is_safe_filename(repo_name):
        return None, "无法从 URL 解析出仓库名"
    dest = _repo_dir(repo_name)
    if dest is None:
        return None, "仓库名非法"
    if dest.exists():
        managed = [n for n, s in installed_git_personas().items() if s["repo_name"] == repo_name]
        if managed:
            return None, f"该仓库已导入为人格「{managed[0]}」"
        # 残留克隆目录（人格已删）：清掉重新克隆
        await asyncio.to_thread(_remove_repo_dir, dest)

    clone_url, fallback_url = _clone_target(url)
    success, message = await git_clone(clone_url, dest, branch=branch, depth=1)
    if not success and fallback_url is not None and _is_mirror_not_synced_error(message):
        logger.warning(t("log.persona.store_mirror_fallback", url=url, e=message[:120]))
        success, message = await git_clone(fallback_url, dest, branch=branch, depth=1)
        if success:
            ok, _ = await git_set_remote_url(dest, fallback_url)
            if not ok:
                logger.warning(t("log.persona.store_remote_set_fail", repo=repo_name))
    if not success:
        await asyncio.to_thread(_remove_repo_dir, dest)
        return None, f"克隆失败: {message}"

    try:
        root = find_bundle_root(dest)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        target = manifest.get("persona_name") if isinstance(manifest, dict) else None
        # 同名人格拒装：覆盖手工人格是数据丢失，接管别的仓库的人格会让旧仓库变孤儿
        if isinstance(target, str) and is_safe_filename(target.strip()):
            target = target.strip()
            existing = read_source(target)
            error: Optional[str] = None
            if existing is not None and existing["repo_name"] != repo_name:
                error = f"人格「{target}」已由其他仓库导入，请先卸载后再导入"
            elif existing is None and Persona(target).exists():
                error = f"已存在同名人格「{target}」，请先删除或改名后再导入"
            if error is not None:
                await asyncio.to_thread(_remove_repo_dir, dest)
                return None, error
        result = await import_bundle_dir(root)
    except (BundleError, json.JSONDecodeError, OSError) as e:
        await asyncio.to_thread(_remove_repo_dir, dest)
        return None, f"仓库结构不符合人格整包格式: {e}"

    write_source(
        result.persona_name,
        PersonaSource(
            source="git",
            repo_url=url,
            repo_name=repo_name,
            branch=branch or "",
            bundle_dir=_bundle_dir_rel(dest, root),
            commit=await git_get_current_commit(dest),
            installed_at=time.time(),
            packs=[d.name for d in _pack_dirs(root)],
        ),
    )
    logger.info(
        t(
            "log.persona.store_installed",
            persona=result.persona_name,
            repo=repo_name,
            packs=len(result.pack_ids),
            imported=len(result.imported_ids),
        )
    )
    return (
        {
            "persona_name": result.persona_name,
            "created": result.created,
            "pack_ids": result.pack_ids,
            "imported": len(result.imported_ids),
            "linked": len(result.linked_ids),
        },
        None,
    )


async def check_persona_update(persona_name: str) -> Optional[bool]:
    """fetch 后比较本地与远端 commit；非 git 人格或检查失败返回 None。"""
    from gsuid_core.utils.plugins_update.git_async import (
        run_git,
        git_fetch,
        git_get_current_branch,
        git_get_current_commit,
    )

    src = read_source(persona_name)
    if src is None:
        return None
    repo = _repo_dir(src["repo_name"])
    if repo is None or not (repo / ".git").exists():
        return None
    ok, _ = await git_fetch(repo)
    if not ok:
        return None
    branch = src["branch"] or await git_get_current_branch(repo)
    code, remote, _ = await run_git(repo, "rev-parse", "--short", f"origin/{branch}")
    if code != 0 or not remote:
        return None
    return remote != await git_get_current_commit(repo)


async def update_persona_from_git(persona_name: str) -> tuple[bool, str]:
    """pull 后按整包结构重新导入并对齐绑定/资源/来源标记。"""
    from gsuid_core.utils.plugins_update.git_async import (
        git_pull,
        git_get_current_commit,
    )

    src = read_source(persona_name)
    if src is None:
        return False, "该人格不是从人格市场导入的"
    repo = _repo_dir(src["repo_name"])
    if repo is None or not (repo / ".git").exists():
        return False, "仓库目录不存在，请卸载后重新导入"

    saved = _snapshot_overrides(persona_name)
    ok, message = await git_pull(repo)
    if not ok:
        return False, f"更新失败: {message}"

    try:
        root = find_bundle_root(repo)
        # 人格改名属于破坏性变更：先读 manifest 拦下，别把新人格悄悄装进来
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise BundleError("manifest.json 不是对象")
        if manifest.get("persona_name") != persona_name:
            return False, "仓库人格名已变更，与已导入人格不一致，请卸载后重新导入"
        result = await import_bundle_dir(root)
    except (BundleError, json.JSONDecodeError, OSError) as e:
        return False, f"仓库结构不符合人格整包格式: {e}"

    persona = Persona(persona_name)
    _restore_overrides(persona_name, saved)
    _reconcile_files(persona, root)
    await _reconcile_packs(persona_name, src, root)

    src["bundle_dir"] = _bundle_dir_rel(repo, root)
    src["commit"] = await git_get_current_commit(repo)
    src["packs"] = [d.name for d in _pack_dirs(root)]
    write_source(persona_name, src)
    logger.info(
        t(
            "log.persona.store_updated",
            persona=persona_name,
            commit=src["commit"],
            imported=len(result.imported_ids),
        )
    )
    return True, "ok"


async def uninstall_git_persona(persona_name: str) -> tuple[bool, str]:
    """删除人格并清掉对应的克隆仓库。"""
    from gsuid_core.ai_core.persona.resource import delete_persona

    src = read_source(persona_name)
    deleted = await delete_persona(persona_name)
    if not deleted:
        return False, "人格不存在"
    if src is not None:
        repo = _repo_dir(src["repo_name"])
        if repo is not None and repo.exists():
            await asyncio.to_thread(_remove_repo_dir, repo)
    return True, "ok"


async def check_all_updates() -> dict[str, bool]:
    """并发检查所有 git 人格的更新状态；单人格异常不拖垮整表。"""
    names = list(installed_git_personas())
    results = await asyncio.gather(*(check_persona_update(n) for n in names), return_exceptions=True)
    return {n: (r is True) for n, r in zip(names, results)}
