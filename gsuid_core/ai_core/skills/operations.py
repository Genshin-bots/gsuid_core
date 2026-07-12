"""
Skills 操作模块

提供技能的管理操作，包括删除、克隆和更新等功能。
"""

import io
import re
import shutil
import tarfile
import zipfile
import tempfile
import subprocess
from typing import List, Tuple, Optional, TypedDict, NotRequired
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic_ai_skills import SkillsToolset

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.skills.resource import (
    SKILLS_PATH,
    skills,
    plugin_skill_dirs,
    skill_source_plugin,
)


def _rebuild_source_map() -> None:
    """根据当前 skills 与已注册插件目录，刷新 skill 名 -> 来源插件名 映射。

    仅当 skill 的 uri 落在某个插件目录下（且不在 data 目录 SKILLS_PATH 下）才记入；
    data 目录来源不入表。同名冲突时（data 放末位优先），胜出者 uri 在 data 下 → 不入表，
    即被视为可编辑的 data skill（用户自定义覆盖插件默认）。
    """
    skill_source_plugin.clear()
    data_root = str(SKILLS_PATH.resolve())
    for name, skill in skills.items():
        uri = skill.uri or ""
        if uri.startswith(data_root):
            continue
        for dir_path, plugin in plugin_skill_dirs:
            if uri.startswith(str(dir_path)):
                skill_source_plugin[name] = plugin
                break


def _rebuild_skills() -> None:
    """从「全部插件目录 + data 目录」重建 skills 字典（就地更新，保持引用稳定）。

    目录顺序把 data 目录放在末位：pydantic-ai 末目录优先，故同名时用户放在
    data/ai_core/skills 的 skill 会覆盖插件默认。重建后刷新来源映射。
    """
    directories: list = [dir_path for dir_path, _ in plugin_skill_dirs]
    directories.append(SKILLS_PATH)
    # 重新创建 SkillsToolset 以刷新 skills；就地 clear+update 维持与 skills_toolset._skills
    # / webconsole 导入的同一 dict 引用（切勿用 skills_toolset.reload() 重绑引用）。
    new_toolset = SkillsToolset(directories=directories)
    skills.clear()
    skills.update(new_toolset._skills)
    _rebuild_source_map()


def is_plugin_skill(skill_name: str) -> bool:
    """该 skill 是否由插件注册（来自插件 repo 目录，webconsole 内只读）。"""
    return skill_name in skill_source_plugin


def get_skill_source(skill_name: str) -> Tuple[str, Optional[str]]:
    """返回 (来源, 插件名)：插件来源为 ("plugin", 插件名)，否则 ("data", None)。"""
    plugin = skill_source_plugin.get(skill_name)
    if plugin is not None:
        return "plugin", plugin
    return "data", None


def register_plugin_skill_directory(path: Path, plugin: str) -> dict:
    """注册插件 repo 内的 skill 目录（目录下含一个或多个 <skill>/SKILL.md）。

    供 register.ai_skill 调用。按绝对路径去重（热重载会重复 import，同路径覆盖
    plugin 名而非追加），随后重建 skills 字典使新 skill 即时生效。

    Args:
        path: 插件 repo 内的 skill 根目录（绝对路径）
        plugin: 来源插件名

    Returns:
        dict: 包含 status、msg 和 count（注册后该目录贡献的 skill 数）
    """
    abspath = path.resolve()

    if not abspath.is_dir():
        logger.warning(t("🧠 [Skills] ai_skill 目标目录不存在，跳过: {abspath}", abspath=abspath))
        return {
            "status": 1,
            "msg": f"Skill directory not found: {abspath}",
        }

    # 按绝对路径去重：同路径覆盖来源插件名（热重载幂等）
    for i, (existing, _) in enumerate(plugin_skill_dirs):
        if existing == abspath:
            plugin_skill_dirs[i] = (abspath, plugin)
            break
    else:
        plugin_skill_dirs.append((abspath, plugin))

    _rebuild_skills()

    count = sum(1 for p in skill_source_plugin.values() if p == plugin)
    return {
        "status": 0,
        "msg": f"Registered skill directory for plugin '{plugin}': {abspath}",
        "count": count,
    }


def delete_skill(skill_name: str) -> dict:
    """
    删除指定的技能（删除整个文件夹）

    Args:
        skill_name: 技能名称

    Returns:
        dict: 包含 status 和 msg 的结果
    """
    if skill_name not in skills:
        return {
            "status": 1,
            "msg": f"Skill '{skill_name}' not found",
        }

    if is_plugin_skill(skill_name):
        _, plugin = get_skill_source(skill_name)
        return {
            "status": 1,
            "msg": f"该技能由插件 {plugin} 管理，请在其仓库内修改",
        }

    skill_path = SKILLS_PATH / skill_name

    if not skill_path.exists():
        return {
            "status": 1,
            "msg": f"Skill folder '{skill_name}' not found",
        }

    try:
        shutil.rmtree(skill_path)
        # 重新加载 skills 字典
        _rebuild_skills()
        return {
            "status": 0,
            "msg": f"Skill '{skill_name}' deleted successfully",
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"Failed to delete skill: {str(e)}",
        }


class SkillInstallResult(TypedDict):
    """install_skill 的返回结构：status 0 成功 / 1 失败，成功时附安装明细。"""

    status: int
    msg: str
    skills: NotRequired[List[str]]
    skill_name: NotRequired[str]


_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"
_HTTP_TIMEOUT = 60.0
_GIT_TIMEOUT = 300
_GIT_CLONE_DIR = "repo"


def _safe_name(name: str) -> Optional[str]:
    """技能目录名安全校验：拒绝分隔符/相对跳转，防止写出 SKILLS_PATH 之外。"""
    name = name.strip()
    if not name or name in {".", ".."} or re.search(r'[\\/:*?"<>|]', name):
        return None
    return name


def _derive_name_from_url(source_url: str) -> Optional[str]:
    """从 URL 猜技能名：优先 slug 查询参数（下载 API 常见），其次路径末段去后缀。"""
    parsed = urlparse(source_url)
    qs = parse_qs(parsed.query)
    if "slug" in qs and qs["slug"]:
        return _safe_name(qs["slug"][0])
    stem = PurePosixPath(parsed.path).name
    for suffix in (".tar.gz", ".tgz", ".zip", ".git", ".md"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return _safe_name(stem)


def _frontmatter_name(md_file: Path) -> Optional[str]:
    """读 SKILL.md frontmatter 的 name（pydantic_ai_skills 以它为技能名真值源）。"""
    text = md_file.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    match = re.search(r"^name:\s*[\"']?([^\s\"'#]+)", text[3:end], re.MULTILINE)
    if match is None:
        return None
    return _safe_name(match.group(1))


def _git_clone(source_url: str, dest: Path) -> Optional[str]:
    """浅克隆到 dest。成功返回 None，失败返回错误文案。"""
    result = subprocess.run(
        ["git", "clone", "--depth", "1", source_url, str(dest)],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    if result.returncode != 0:
        return f"git clone 失败: {result.stderr.strip()}"
    return None


def _fetch_http_source(source_url: str, workdir: Path) -> Optional[str]:
    """下载 URL 并按内容识别解包（zip / tar / 单个 SKILL.md）。失败返回错误文案。"""
    resp = httpx.get(source_url, follow_redirects=True, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.content
    if data.startswith(_ZIP_MAGIC):
        # Python 3.6+ 的 extractall 已剥离盘符 / 绝对路径 / .. 成分
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(workdir)
        return None
    if data.startswith(_GZIP_MAGIC) or data[257:262] == b"ustar":
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(workdir, filter="data")
        return None
    text = data.decode("utf-8", errors="replace")
    if text.lstrip().startswith("---") and "name:" in text:
        (workdir / "SKILL.md").write_text(text, encoding="utf-8")
        return None
    return "URL 内容不是 zip/tar 包或 SKILL.md 文档（疑似普通网页），请改用 git 仓库地址或压缩包直链"


def _acquire_source(source_url: str, workdir: Path) -> Optional[str]:
    """把来源内容落到 workdir。返回 None 表示成功，否则为错误文案。"""
    if source_url.endswith(".git") or source_url.startswith("git@"):
        return _git_clone(source_url, workdir / _GIT_CLONE_DIR)
    err = _fetch_http_source(source_url, workdir)
    if err is None:
        return None
    # 仓库主页类 URL（如 github.com/x/y）HTTP 拿到的是网页，再试 git clone
    clone_dir = workdir / _GIT_CLONE_DIR
    git_err = _git_clone(source_url, clone_dir)
    # dumb-http 对非仓库 URL 可能"成功"克隆出空仓库（exit 0），视同失败保留 HTTP 错误
    if git_err is None and any(p.name != ".git" for p in clone_dir.iterdir()):
        return None
    if git_err is None:
        git_err = "git clone 得到空仓库"
    return f"{err}；{git_err}"


def _find_skill_roots(root: Path, max_depth: int = 4) -> List[Path]:
    """找出 root 下所有含 SKILL.md 的技能根目录（命中即停，不再深入其子目录）。"""

    def walk(d: Path, depth: int) -> List[Path]:
        if (d / "SKILL.md").is_file():
            return [d]
        if depth >= max_depth:
            return []
        found: List[Path] = []
        for child in sorted(d.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                found.extend(walk(child, depth + 1))
        return found

    return walk(root, 0)


def _resolve_skill_name(
    root: Path,
    source_url: str,
    explicit: Optional[str],
    extract_roots: set[Path],
) -> Optional[str]:
    """技能命名：frontmatter name 优先（保证目录名 == skills 字典键，delete 可定位）。"""
    name = _frontmatter_name(root / "SKILL.md")
    if name is not None:
        return name
    if explicit is not None:
        return explicit
    if root not in extract_roots:
        return _safe_name(root.name)
    return _derive_name_from_url(source_url)


def install_skill(
    source_url: str,
    skill_name: Optional[str] = None,
    update: bool = False,
) -> SkillInstallResult:
    """从 git 仓库 / zip、tar 直链 / SKILL.md 直链安装（或更新）技能到框架技能目录。

    统一安装入口：来源内容先落临时目录，找出全部含 SKILL.md 的技能根，整体拷贝进
    SKILLS_PATH（目录名取 frontmatter 的 name，保证与 delete_skill 的
    SKILLS_PATH/<name> 定位一致），最后重建 skills 字典即时生效，无需重启。

    Args:
        source_url: git 仓库地址 / zip、tar 压缩包直链 / SKILL.md 文件直链
        skill_name: 可选技能名（仅当 SKILL.md 无 frontmatter name 时作为命名依据）
        update: 同名技能已存在时是否覆盖更新

    Returns:
        SkillInstallResult: status、msg，成功时附 skills（安装列表）与 skill_name（首个）
    """
    if skill_name is not None:
        checked = _safe_name(skill_name)
        if checked is None:
            return {"status": 1, "msg": f"非法技能名: {skill_name}"}
        skill_name = checked

    with tempfile.TemporaryDirectory(prefix="gscore_skill_") as tmp:
        workdir = Path(tmp)
        try:
            err = _acquire_source(source_url, workdir)
        except (
            OSError,
            httpx.HTTPError,
            tarfile.TarError,
            zipfile.BadZipFile,
            subprocess.TimeoutExpired,
        ) as e:
            return {"status": 1, "msg": f"获取技能源失败: {e}"}
        if err is not None:
            return {"status": 1, "msg": err}

        roots = _find_skill_roots(workdir)
        if not roots:
            return {
                "status": 1,
                "msg": "来源内容中未找到任何 SKILL.md，不是有效的技能包，已放弃安装",
            }

        # 先整体解析命名与冲突再拷贝，避免装到一半失败留下残缺状态
        extract_roots = {workdir, workdir / _GIT_CLONE_DIR}
        named: List[Tuple[Path, str]] = []
        seen: set[str] = set()
        for root in roots:
            explicit = skill_name if len(roots) == 1 else None
            name = _resolve_skill_name(root, source_url, explicit, extract_roots)
            if name is None:
                return {
                    "status": 1,
                    "msg": (f"无法确定技能名（{root.name} 的 SKILL.md 缺少 name 字段），请显式传入 skill_name"),
                }
            if name not in seen:
                seen.add(name)
                named.append((root, name))

        existed = [n for _, n in named if (SKILLS_PATH / n).exists()]
        if existed and not update:
            return {
                "status": 1,
                "msg": f"技能已存在: {', '.join(existed)}；如需覆盖更新请传 update=True",
            }

        SKILLS_PATH.mkdir(parents=True, exist_ok=True)
        for root, name in named:
            target = SKILLS_PATH / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(root, target, ignore=shutil.ignore_patterns(".git"))

    _rebuild_skills()

    installed = [n for _, n in named]
    missing = [n for n in installed if n not in skills]
    msg = f"已安装 {len(installed)} 个技能: {', '.join(installed)}"
    if existed:
        msg += f"（覆盖更新: {', '.join(existed)}）"
    if missing:
        msg += f"；⚠️ 以下技能安装后未被加载（frontmatter 可能不合规）: {', '.join(missing)}"
    return {
        "status": 0,
        "msg": msg,
        "skills": installed,
        "skill_name": installed[0],
    }


def update_skill_markdown(skill_name: str, content: str) -> dict:
    """
    更新技能的 markdown 文件内容

    Args:
        skill_name: 技能名称
        content: 新的 markdown 内容

    Returns:
        dict: 包含 status 和 msg 的结果
    """
    if skill_name not in skills:
        return {
            "status": 1,
            "msg": f"Skill '{skill_name}' not found",
        }

    if is_plugin_skill(skill_name):
        _, plugin = get_skill_source(skill_name)
        return {
            "status": 1,
            "msg": f"该技能由插件 {plugin} 管理，请在其仓库内修改",
        }

    skill_path = SKILLS_PATH / skill_name
    md_file = skill_path / "SKILL.md"

    if not skill_path.exists():
        return {
            "status": 1,
            "msg": f"Skill folder '{skill_name}' not found",
        }

    try:
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(content)

        # 重新加载 skills 字典
        _rebuild_skills()

        return {
            "status": 0,
            "msg": f"Skill '{skill_name}' markdown updated successfully",
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"Failed to update skill markdown: {str(e)}",
        }


def get_skill_markdown_path(skill_name: str) -> Optional[Path]:
    """
    获取技能的 markdown 文件路径

    Args:
        skill_name: 技能名称

    Returns:
        Optional[Path]: markdown 文件路径，如果不存在则返回 None
    """
    skill = skills.get(skill_name)
    if skill is None or not skill.uri:
        return None

    # 用 skill 的真实目录（uri）定位 SKILL.md，使 data 与插件来源的 skill 都能读取，
    # 不再硬编码 SKILLS_PATH/<name>。
    md_file = Path(skill.uri) / "SKILL.md"

    if md_file.exists():
        return md_file
    return None
