"""
插件开发工具模块

提供让 AI「插件开发代理」(``plugin_developer_agent``) 在**工作区**脚手架 / 自检，
并在主人审批通过后把插件落进 ``gsuid_core/plugins/<Name>/`` 热加载的能力。

## 设计要点（关键）

插件代码全程在工作区读写（用 ``file_manager`` 的 file 工具），本模块只负责脚手架、
语法自检、审批安装与热加载——只有 ``copy_to_plugin_dir`` / ``load_plugin_into_core``
碰真正的 ``plugins/``：

- 每个工具都用 ``check_pm`` 限定**仅主人 (PM=0)** 可触发（与 ``execute_shell_command``
  同级信任面——主人本就能 ``core重载插件`` / 跑 shell）。
- 落 ``plugins/`` 的路径强制限定在**单个插件目录**内（``_resolve_in_plugin`` 做
  ``resolve()`` 后的归属校验），杜绝路径穿越 / 跨插件写入。
- 工具单独归入 ``category="plugin_dev"``，并被 ``rag.tools.NON_SEARCHABLE_TOOL_CATEGORIES``
  登记为「永不可检索」：既**不进**主人格保底池(self/buildin)、也**永不**被任何 Agent
  （主人格 / 通用子代理 / 其它能力代理补充检索）的向量检索召回。只有
  ``plugin_developer_agent`` 画像在 ``tool_names`` 里显式引用时才装配
  （``runner._resolve_tools`` 走 ``get_all_tools`` 按名取、不经向量检索）。

热加载复用框架既有的 ``reload_plugin()``——它对全新插件目录同样有效（清理阶段对
新插件 no-op，随后 ``gss.load_plugin`` 从 ``PLUGIN_PATH`` 发现新目录并导入、重跑
``@on_core_start`` 钩子）。
"""

import shutil
import py_compile
from typing import Dict, Tuple, Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.check_func import check_pm

# 插件名长度上限（防滥造超长目录名）
_PLUGIN_NAME_MAX_LEN: int = 64

# 单次读取指南正文的长度上限（防把超大章节一次性灌回上下文）
_GUIDE_MAX_CHARS: int = 24000

# 框架插件开发指南（权威全文），供 read_plugin_dev_guide 按需读取。
_SKILL_PATH: Path = Path(__file__).resolve().parents[3] / "docs" / "skills" / "gscore-plugin-development" / "SKILL.md"


def _plugin_root() -> Path:
    """惰性返回 ``gsuid_core/plugins`` 根目录（与 ``server.PLUGIN_PATH`` 一致）。

    惰性 import 以避开启动期 ``server`` <-> ``ai_core`` 的潜在循环导入。
    """
    from gsuid_core.server import PLUGIN_PATH

    return PLUGIN_PATH


def _workspace_root() -> Optional[Path]:
    """当前能力代理的可写工作区根目录；不在任务上下文时为 None。"""
    from gsuid_core.ai_core.planning.runtime import get_plan_context

    ctx = get_plan_context()
    if ctx is None or ctx.artifact_workspace is None:
        return None
    return ctx.artifact_workspace


def _resolve_in_workspace(plugin_name: str, rel_path: str) -> Tuple[Optional[Path], str]:
    """把 ``rel_path`` 解析到 ``workspace/<plugin_name>/`` 之内，防越界。"""
    ok, err = _validate_plugin_name(plugin_name)
    if not ok:
        return None, err
    root = _workspace_root()
    if root is None:
        return None, "错误：当前不在工作区上下文，无法在工作区开发插件。"
    plugin_dir = (root / plugin_name).resolve()
    target = (plugin_dir / rel_path).resolve()
    if target != plugin_dir and plugin_dir not in target.parents:
        return None, f"错误：非法路径访问拒绝（越界）：{rel_path}"
    return target, ""


def _validate_plugin_name(plugin_name: str) -> Tuple[bool, str]:
    """校验插件名是否合法。返回 ``(是否合法, 错误信息)``。"""
    if not plugin_name:
        return False, "错误：插件名不能为空"
    if len(plugin_name) > _PLUGIN_NAME_MAX_LEN:
        return False, f"错误：插件名过长（上限 {_PLUGIN_NAME_MAX_LEN} 字符）"
    if plugin_name.startswith("_"):
        return False, "错误：插件名不能以下划线开头（下划线前缀是框架 buildin 约定，会被加载器跳过）"
    if not all(ch.isalnum() or ch == "_" for ch in plugin_name):
        return False, "错误：插件名只能包含字母、数字、下划线"
    return True, ""


def _resolve_in_plugin(plugin_name: str, rel_path: str) -> Tuple[Optional[Path], str]:
    """把 ``rel_path`` 解析到 ``plugins/<plugin_name>/`` 之内，防路径穿越。

    返回 ``(解析后的绝对路径, 错误信息)``；错误时路径为 ``None``。``rel_path`` 允许
    为空（指代插件根目录本身）。
    """
    ok, err = _validate_plugin_name(plugin_name)
    if not ok:
        return None, err

    plugin_dir = (_plugin_root() / plugin_name).resolve()
    target = (plugin_dir / rel_path).resolve()
    # target 必须等于插件根、或位于插件根之下
    if target != plugin_dir and plugin_dir not in target.parents:
        return None, f"错误：非法路径访问拒绝（越界）：{rel_path}"
    return target, ""


def _existing_plugin_dirname(plugin_name: str) -> str:
    """容错解析插件名：返回磁盘上与 ``plugin_name`` 同名（**大小写不敏感**）的真实目录名。

    LLM 常把插件名大小写写错（weather / Weather）。"操作已存在插件"的工具用它把名字
    归一到真实目录名——既避免大小写不符直接 not found，也避免在大小写敏感的文件系统
    （Linux）上误建大小写变体空目录。

    注意：**必须靠 iterdir 拿真实目录名**，不能用 ``(root / name).exists()`` 判断——Windows
    文件系统大小写不敏感，对任意大小写 exists() 都为 True，但 ``SV.self_plugin_name`` 取的是
    磁盘上真实的目录名，比对时大小写必须一致（否则 test_plugin_command 找不到触发器）。
    无同名目录时原样返回，交由后续校验报错。
    """
    if not plugin_name:
        return plugin_name
    root = _plugin_root()
    lower = plugin_name.lower()
    ci_match: Optional[str] = None
    try:
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name == plugin_name:
                return entry.name  # 精确命中，最优
            if ci_match is None and entry.name.lower() == lower:
                ci_match = entry.name
    except OSError:
        pass
    return ci_match or plugin_name


def _suggest_available_plugin_name(base: str) -> str:
    """脚手架重名时给一个尚未被占用的新插件名（大小写不敏感判重）。"""
    root = _plugin_root()
    existing: set = set()
    try:
        existing = {e.name.lower() for e in root.iterdir() if e.is_dir()}
    except OSError:
        pass
    for i in range(2, 100):
        cand = f"{base}{i}"
        if cand.lower() not in existing:
            return cand
    return f"{base}New"


def _skeleton_files(
    plugin_name: str,
    display_name: str,
    description: str,
    force_prefix: str,
    author: str,
) -> Dict[str, str]:
    """生成嵌套加载模式插件骨架的 ``{相对路径: 文件内容}`` 映射。

    参照 ZZZeroUID / SayuStock：外层插件包 + 内层同名 Python 包。
    """
    show_name = display_name or plugin_name
    desc = description or f"{show_name} —— 由插件开发代理生成的 GsCore 插件"
    author_name = author or "plugin_developer_agent"
    # 代码 / TOML 字符串内的引号、换行会破坏解析（实测 LLM 给的描述带引号 → Invalid TOML）。
    safe_show = show_name.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()
    desc_toml = desc.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()

    # 内层包入口：声明 Plugins 单例（触发该插件前缀 / 别名注册）
    if force_prefix:
        prefix_line = f'    force_prefix=["{force_prefix}"],\n    allow_empty_prefix=False,\n'
    else:
        prefix_line = "    allow_empty_prefix=True,\n"

    inner_init = (
        '"""init"""\n'
        "from gsuid_core.sv import Plugins\n\n"
        "# Plugins 是插件级单例：声明整个插件的前缀 / 权限 / 别名。\n"
        "# 同名 plugin_name 在框架内只创建一次，所有 SV 自动归属到它。\n"
        "Plugins(\n"
        f'    name="{plugin_name}",\n'
        f"{prefix_line}"
        f'    alias=["{safe_show}"],\n'
        ")\n\n"
        "# 业务代码放在本内层包的**子目录**里（每个子目录含 __init__.py，如已生成的\n"
        "# main/）。框架的嵌套加载会自动 import 这些子目录、触发其 @sv.on_xxx 注册，\n"
        "# 无需在此或 __full__.py 手动 from . import xxx；__full__.py 保持空即可。\n"
    )

    # 业务子模块骨架：放在内层包的 main/ 子目录下，**框架自动导入**，开箱即载。
    # 给开发代理一个"改这一个文件就行"的落点，避免它在同名嵌套目录里反复迷路。
    prefix_hint = f"（实际触发是插件前缀「{force_prefix}」+ 命令词，如「{force_prefix}示例」）" if force_prefix else ""
    stub_module = (
        f'"""{safe_show} 业务模块——在这里实现你的命令。\n\n'
        f"本目录是内层包 {plugin_name}/ 的一个子模块，框架（嵌套加载）会自动 import 它、\n"
        "触发下面的 @sv.on_xxx 注册，无需在别处手动 import。把示例命令替换成你的真实逻辑即可。\n"
        '"""\n'
        "from gsuid_core.sv import SV\n"
        "from gsuid_core.bot import Bot\n"
        "from gsuid_core.models import Event\n\n"
        "# SV 是一组功能的集合，名字随意（不必和插件同名）。\n"
        f'sv = SV("{safe_show}")\n\n\n'
        f"# 示例命令：命令词是「示例」{prefix_hint}；ev.text 是命令后面的参数。\n"
        '@sv.on_command("示例")\n'
        f"async def {plugin_name}_example(bot: Bot, ev: Event) -> None:\n"
        f'    await bot.send(f"{safe_show} 收到：{{ev.text}}")\n'
    )

    pyproject = (
        "[project]\n"
        f'name = "{plugin_name.lower()}"\n'
        'version = "1.0.0"\n'
        f'description = "{desc_toml}"\n'
        f'authors = [{{ name = "{author_name}" }}]\n'
        "dependencies = []\n"
        "\n"
        '# 启动时自动安装 dependencies 中声明的依赖（如需发 HTTP 请求加 "httpx"）。\n'
        "# python / fastapi / pydantic / gsuid-core / sqlmodel / apscheduler /\n"
        "# pydantic-ai 等框架基础依赖无需重复声明。\n"
    )

    readme = f"# {show_name}\n\n{desc}\n\n> 由 GsCore 插件开发代理 (`plugin_developer_agent`) 生成。\n"

    return {
        # ── 外层插件包 ──────────────────────────────
        "__init__.py": '"""init"""\n',
        "__nest__.py": "",  # 空文件：向框架声明启用嵌套加载
        "pyproject.toml": pyproject,
        "README.md": readme,
        # ── 内层同名 Python 包 ──────────────────────
        f"{plugin_name}/__init__.py": inner_init,
        f"{plugin_name}/__full__.py": "",  # 空文件：嵌套加载下不读其内容，保持空即可
        # ── 业务子模块骨架（直接编辑这个文件实现功能）────
        f"{plugin_name}/main/__init__.py": stub_module,
        f"{plugin_name}/version.py": f'{plugin_name}_version = "1.0.0"\n',
    }


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def scaffold_plugin(
    ctx: RunContext[ToolContext],
    plugin_name: str,
    display_name: str = "",
    description: str = "",
    force_prefix: str = "",
    author: str = "",
) -> str:
    """
    脚手架：在**工作区**里创建一个全新 GsCore 插件的标准嵌套加载骨架（不碰 plugins/）。

    生成外层插件包（__init__.py / __nest__.py / pyproject.toml / README.md）、内层同名
    Python 包（__init__.py 含 Plugins(...) / 空 __full__.py / version.py），以及一个可直接
    加载的业务示例 main/__init__.py。开发全程在工作区进行，最后用 copy_to_plugin_dir
    （走主人审批）才装进 plugins/。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名（同时是目录名与内层包名），只能含字母/数字/下划线，不能以下划线开头。
        display_name: 展示名（别名），留空则用 plugin_name。
        description: 一句话描述，写入 README / pyproject。
        force_prefix: 强制命令前缀，留空则允许无前缀触发。
        author: 作者名，留空默认 "plugin_developer_agent"。
    """
    plugin_dir, err = _resolve_in_workspace(plugin_name, "")
    if plugin_dir is None:
        return err
    if plugin_dir.exists() and any(plugin_dir.iterdir()):
        return (
            f"错误：工作区已存在插件目录 {plugin_name}（非空）。"
            "继续编辑用 write_file_content/read_file_content，别重复脚手架。"
        )

    # 已安装同名插件拦截：plugins/ 里已有同名插件、但工作区还没有它时，**绝不**用空骨架
    # 覆盖式重写——那会把主人现有的实现整个丢掉（实测会话 2df150：主人让"改"，代理却
    # 在全新空工作区里 scaffold 重写了一遍）。改成引导先 pull 进工作区在原代码上修改。
    installed_name = _existing_plugin_dirname(plugin_name)
    installed_dir = (_plugin_root() / installed_name).resolve()
    if installed_dir.exists() and _is_plugin_child(installed_dir):
        return (
            f"ℹ️ plugins/ 里已存在**已安装**的同名插件「{installed_name}」。\n"
            f'- 若你的任务是**修改 / 修复**它：先 pull_installed_plugin("{installed_name}") 把现有'
            "代码拉进工作区，在原代码基础上 read/write **改动**（**不要**重写），改完 "
            "validate_plugin → copy_to_plugin_dir（会走覆盖更新审批）→ load_plugin_into_core → "
            "test_plugin_command。\n"
            "- 若你确实要新建一个**不同的**插件：换一个不冲突的名字再 scaffold_plugin。"
        )

    files = _skeleton_files(plugin_name, display_name, description, force_prefix, author)
    try:
        for rel_path, content in files.items():
            target, sub_err = _resolve_in_workspace(plugin_name, rel_path)
            if target is None:
                return sub_err
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.exception(f"🧩 [PluginDev] 脚手架插件 {plugin_name} 失败: {e}")
        return f"错误：脚手架失败：{e}"

    logger.info(f"🧩 [PluginDev] 已在工作区脚手架插件 {plugin_name}（{len(files)} 个文件）")
    listing = "\n".join(f"  - {plugin_name}/{p}" for p in files)
    biz = f"{plugin_name}/{plugin_name}/main/__init__.py"
    return (
        f"已在工作区创建插件骨架（下列即 write_file_content/read_file_content 用的"
        f"**工作区根相对路径**，直接照抄，别自己构造）：\n{listing}\n\n"
        "【目录心智模型】\n"
        f"- 外层包是 {plugin_name}/，里面有**同名内层包** {plugin_name}/{plugin_name}/（这层双名是规范，不是错）。\n"
        f"- 业务示例已放好在 → **{biz}** ←（可直接加载，先编辑它）。\n"
        f"- 新功能在内层包 {plugin_name}/{plugin_name}/ 下再加子目录；__full__.py 保持空（框架自动遍历导入）。\n\n"
        f"下一步：编辑 {biz} 实现功能 → validate_plugin 自检 → copy_to_plugin_dir 安装（需主人审批）→ "
        "load_plugin_into_core 加载 → test_plugin_command 自测。"
    )


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def pull_installed_plugin(ctx: RunContext[ToolContext], plugin_name: str) -> str:
    """把一个**已安装**在 plugins/ 里的插件完整拷贝进当前工作区，用于在其**现有代码**上修改 / 修复。

    这是「改一个已经装好的插件」的正确起点——每次 create_subagent 的工作区都是空的，看不到
    已安装的实现，必须先 pull 进来再改，否则只能从零 scaffold 重写、把原实现整个丢掉。

    工作循环：pull_installed_plugin → read_file_content 看现状 → write_file_content 在原代码上
    改动（**不要重写**）→ validate_plugin → copy_to_plugin_dir（plugins/ 已有同名 → 覆盖更新审批）
    → load_plugin_into_core → test_plugin_command。

    Args:
        ctx: 工具执行上下文
        plugin_name: 已安装插件名（大小写不敏感，自动归一到磁盘真实目录名）
    """
    ok, err = _validate_plugin_name(plugin_name)
    if not ok:
        return err
    real_name = _existing_plugin_dirname(plugin_name)
    src, serr = _resolve_in_plugin(real_name, "")
    if src is None:
        return serr
    if not src.exists() or not _is_plugin_child(src):
        return f"错误：plugins/ 里没有已安装的插件 {plugin_name}，没东西可拉取。要从零新建请用 scaffold_plugin。"
    dest, derr = _resolve_in_workspace(real_name, "")
    if dest is None:
        return derr
    if dest.exists() and any(dest.iterdir()):
        return (
            f"工作区已存在 {real_name}（非空）——直接 read_file_content / write_file_content "
            "在现有代码上修改即可，不用重复拉取。"
        )

    root = _workspace_root()
    if root is None:
        return "错误：当前不在工作区上下文，无法拉取插件。"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except OSError as e:
        logger.exception(f"🧩 [PluginDev] 拉取已安装插件 {real_name} 失败: {e}")
        return f"错误：拉取已安装插件失败：{e}"

    files = sorted(str(p.relative_to(root)).replace("\\", "/") for p in dest.rglob("*") if p.is_file())
    logger.info(f"🧩 [PluginDev] 已把已安装插件 {real_name} 拉进工作区（{len(files)} 个文件）")
    listing = "\n".join(f"  - {p}" for p in files[:40])
    more = f"\n  …还有 {len(files) - 40} 个文件未列出" if len(files) > 40 else ""
    return (
        f"已把已安装插件 {real_name} 拉进工作区（下列即工作区根相对路径，直接照抄）：\n"
        f"{listing}{more}\n\n"
        "下一步：先 read_file_content 看清现有实现，再 write_file_content **在原代码上改动**"
        "（别重写、别 scaffold）→ validate_plugin → copy_to_plugin_dir（plugins/ 已有同名，"
        "会走覆盖更新审批）→ load_plugin_into_core → test_plugin_command 实跑核心命令。"
    )


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def validate_plugin(
    ctx: RunContext[ToolContext],
    plugin_name: str,
) -> str:
    """
    语法自检：对**工作区**里该插件所有 .py 文件做 py_compile，报告语法错误。

    在 copy_to_plugin_dir 安装之前调用，提前发现语法错误。注意它只查语法，发现不了
    import 错误或运行时错误——那些要靠 load_plugin_into_core 的返回信息发现。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名
    """
    plugin_dir, err = _resolve_in_workspace(plugin_name, "")
    if plugin_dir is None:
        return err
    if not plugin_dir.exists():
        return f"错误：工作区里没有插件目录 {plugin_name}，请先 scaffold_plugin。"

    py_files = sorted(plugin_dir.rglob("*.py"))
    if not py_files:
        return f"插件 {plugin_name} 下没有 .py 文件可检查"

    errors: list[str] = []
    for py_file in py_files:
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"  - {plugin_name}/{py_file.relative_to(plugin_dir)}: {e.msg.strip()}")

    if errors:
        return f"❌ 语法检查发现 {len(errors)} 处错误：\n" + "\n".join(errors)
    return f"✅ 语法检查通过（共 {len(py_files)} 个 .py 文件）"


def _is_plugin_child(p: Path) -> bool:
    """``p`` 是否正好是 ``plugins/`` 的**单层子目录**（resolve 后判定，连软链接逃逸一并防住）。"""
    root = _plugin_root().resolve()
    p = p.resolve()
    return p != root and p.parent == root


def _physical_install(src: Path, dest: Path) -> Optional[str]:
    """全量覆盖把工作区插件复制进 ``dest``（plugins/ 单层子目录）；成功返回 None，失败返回错误串。

    自带防御性自校验：dest 必须正好是 plugins/ 的**单层子目录**，否则拒绝——杜绝 rmtree
    误伤 plugins/ 本身、其它插件或目录外路径（不依赖调用方先校验）。**不**对 data/ 做任何
    特殊保留：插件运行期 data 的兼容性应由插件自身负责（插件本就会读写自己的 data 区），
    开发期不代为搬运 / 备份用户数据。
    """
    if not _is_plugin_child(dest):
        logger.error(f"🧩 [PluginDev] 安装目标越界，拒绝写入: {dest}")
        return f"错误：安装目标非法（必须是 plugins/ 的直接子目录）：{dest}"
    try:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    except OSError as e:
        logger.exception(f"🧩 [PluginDev] 安装插件 {dest.name} 失败: {e}")
        return f"错误：复制到 plugins/ 失败：{e}"
    return None


def _finalize_update(dest: Path, staged: Path) -> Optional[str]:
    """覆盖更新收尾：删旧目录(精确命中 dest) + 把暂存目录改回正式名。成功返回 None。

    dest / staged 都必须是 plugins/ 的单层子目录（自校验）——删除只会动 dest 这一个**记录
    在案**的旧目录，绝不波及其它插件；改名是同盘 rename，无额外副本。
    """
    if not _is_plugin_child(dest) or not _is_plugin_child(staged):
        logger.error(f"🧩 [PluginDev] 收尾路径越界，拒绝: dest={dest} staged={staged}")
        return "错误：收尾路径非法（dest / 暂存目录都必须是 plugins/ 的直接子目录）。"
    try:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(staged), str(dest))
    except OSError as e:
        logger.exception(f"🧩 [PluginDev] 收尾更新 {dest.name} 失败: {e}")
        return f"错误：删除旧目录 / 改名失败：{e}"
    return None


def _pick_staging_name(plugin_name: str) -> Optional[str]:
    """为"覆盖更新"挑一个 plugins/ 下尚未占用、且合法的临时插件目录名（大小写不敏感判重）。

    覆盖更新全程"先以临时名装新代码 → 审批后删旧目录 → 临时名改回真名"，绝不直接 rmtree
    已存在的同名插件；找不到可用名返回 None。
    """
    root = _plugin_root()
    existing: set = set()
    try:
        existing = {e.name.lower() for e in root.iterdir() if e.is_dir()}
    except OSError:
        pass
    candidates = [f"{plugin_name}_new"] + [f"{plugin_name}_new{i}" for i in range(2, 100)]
    for cand in candidates:
        ok, _ = _validate_plugin_name(cand)
        if ok and cand.lower() not in existing:
            return cand
    return None


# ── 安装状态账本 ────────────────────────────────────────────────────────────
# 安装是非阻塞的多步审批流程，跨"任务挂起 waiting_approval → 主人批准 → 重新调度"会
# 多次重入 copy_to_plugin_dir。状态必须持久化才能在重入时恢复——落在 Kanban 任务日志
# (event_type="decision") 里当作机器账本：发起每步审批前先写一条 req-* 标记，安装/暂存
# 成功后写 staged|/installed 标记。这些标记**不进**审批播报（审批文案另走 failure_reason）。
_LEDGER = "🧩dev|"


def _mark(plugin_name: str, kind: str) -> str:
    """构造该插件的一条账本标记内容。"""
    return f"{_LEDGER}{plugin_name}|{kind}"


async def _record(content: str) -> None:
    """把一条账本标记写进当前 Kanban 任务日志（只在成功推进时调用）。"""
    from gsuid_core.ai_core.planning.models import AIAgentTaskLog
    from gsuid_core.ai_core.planning.runtime import get_plan_context

    plan_ctx = get_plan_context()
    if plan_ctx is None or not plan_ctx.task_id:
        return
    await AIAgentTaskLog.add_log(plan_ctx.task_id, "decision", content)


async def _task_logs() -> list:
    """当前 Kanban 任务全部日志（按时间升序）；无任务上下文时返回空表。"""
    from gsuid_core.ai_core.planning.models import AIAgentTaskLog
    from gsuid_core.ai_core.planning.runtime import get_plan_context

    plan_ctx = get_plan_context()
    if plan_ctx is None or not plan_ctx.task_id:
        return []
    return await AIAgentTaskLog.get_for_task(plan_ctx.task_id)


def _is_installed(logs: list, plugin_name: str) -> bool:
    """本任务内该插件是否已完成安装（新建安装成功 / 覆盖更新收尾成功都会记此标记）。"""
    target = _mark(plugin_name, "installed")
    return any(lg.event_type == "decision" and lg.content == target for lg in logs)


def _install_state(logs: list, plugin_name: str) -> Tuple[str, Optional[str]]:
    """重放账本 + 审批事件，得出该插件安装状态机的当前阶段与暂存目录名。

    把每个"主人批准"严格绑定到**我自己**最近写下的 req-* 标记上（pending）：与本流程
    无关的审批（如重派达上限的放行）发生时 pending 为 None，会被忽略——杜绝"拿别的审批
    冒充安装审批"而跳过闸门。返回 ``(phase, staged_name)``：

    - ``none``          还没发起过任何安装审批（首次调用）
    - ``await_install`` 新建：已请求安装审批、待主人批准
    - ``do_install``    新建：审批已过、待执行复制
    - ``await_stage``   覆盖更新：已请求"以临时名安装"审批、待批准
    - ``do_stage``      覆盖更新：第一步审批已过、待以临时名安装新代码
    - ``await_delete``  覆盖更新：临时名已装好、已请求"删旧目录"审批、待批准
    - ``do_finalize``   覆盖更新：删旧审批已过、待收尾（删旧目录 + 临时名改回真名）
    - ``installed``     已安装完成
    """
    head = _mark(plugin_name, "")
    phase = "none"
    pending: Optional[str] = None  # 我最近一次"已发起、待裁决"的审批类型
    staged_name: Optional[str] = None
    for lg in logs:
        c = lg.content
        if lg.event_type == "decision" and c.startswith(head):
            kind = c[len(head) :]
            if kind == "req-install":
                pending, phase = "install", "await_install"
            elif kind == "req-stage":
                pending, phase = "stage", "await_stage"
            elif kind == "req-delete":
                pending, phase = "delete", "await_delete"
            elif kind.startswith("staged|"):
                staged_name = kind[len("staged|") :] or None
            elif kind == "installed":
                phase, pending = "installed", None
        elif lg.event_type == "approval":
            if c.startswith("主人批准") and pending is not None:
                phase = {"install": "do_install", "stage": "do_stage", "delete": "do_finalize"}[pending]
                pending = None
            elif c.startswith("主人拒绝"):
                pending = None
    return phase, staged_name


def _ledger_plugin_names(logs: list) -> list:
    """从任务账本里按首次出现顺序提取所有出现过的插件名。"""
    names: list = []
    for lg in logs:
        if lg.event_type == "decision" and lg.content.startswith(_LEDGER):
            name = lg.content[len(_LEDGER) :].split("|", 1)[0]
            if name and name not in names:
                names.append(name)
    return names


async def install_resume_hint_for_task(task_id: str) -> str:
    """重新调度插件开发代理时的「断点续作」提示；无进行中的安装流程时返回空串。

    安装是跨『审批挂起 → 主人批准 → 重新调度』的多步流程。重新调度时开发代理的对话
    history 是空的——它并不知道自己上一轮已经 scaffold + 写码 + 发起并通过了安装审批，
    于是从头重读指南、重新 scaffold（实测会话 fa7eef：批准后又把整套流程重跑一遍）。
    本函数读任务账本判断安装推进到了哪一步，给重新调度的代理一段**明确的续作指引**，
    让它直接接着 copy(实际落盘)/load/test，而不是从头再来。

    注意：新建插件审批通过后 phase=do_install，此时**必须再调一次 copy_to_plugin_dir**
    才会真正落盘并写下 installed 标记（直接 load 会被「请先 copy_to_plugin_dir」拦下），
    故提示里明确要求再 copy 一次（这次不再发起审批）。
    """
    from gsuid_core.ai_core.planning.models import AIAgentTaskLog

    logs = await AIAgentTaskLog.get_for_task(task_id)
    if not logs:
        return ""
    parts: list = []
    for name in _ledger_plugin_names(logs):
        phase, _staged = _install_state(logs, name)
        if phase in ("do_install", "do_stage", "do_finalize"):
            parts.append(
                f"- 插件「{name}」安装审批**已通过**、工作区代码已就绪。直接续作：调用 "
                f'copy_to_plugin_dir("{name}") 推进安装，并**照它的返回提示继续**（新建插件本次即落盘；'
                f'覆盖更新可能还需主人再批一次"删除旧目录"，那就再停手等主人）；装好后 '
                f'load_plugin_into_core("{name}") → test_plugin_command 实跑每条核心命令 → 交付。'
            )
        elif phase == "installed":
            parts.append(
                f"- 插件「{name}」已安装。若尚未自测，直接 "
                f'load_plugin_into_core("{name}") → test_plugin_command 实跑核心命令 → 交付。'
            )
    if not parts:
        return ""
    return (
        "【断点续作 · 重要】你**不是第一次**执行本任务：上一轮已完成开发并通过安装审批，"
        "现在是审批通过后的重新调度。**不要**重新 scaffold / 重写代码 / 重读开发指南，"
        "按下面的断点直接往下做、做完务必自测并交付：\n" + "\n".join(parts)
    )


def _delete_approval_prompt(plugin_name: str, staging_name: str) -> str:
    """覆盖更新第二步——删除旧目录的审批播报文案（人话、面向主人）。"""
    return (
        f"插件「{plugin_name}」的新代码已安全安装到临时目录 plugins/{staging_name}/（移动已完成、"
        f"记录在案，旧目录原封未动）。现请求**删除旧目录** plugins/{plugin_name}/——删除只会精确"
        f"命中这一个记录在案的旧目录，删除后会把临时目录改回正式名 {plugin_name}。"
    )


def _delete_waiting_reply(plugin_name: str, staging_name: str) -> str:
    """发起删旧目录审批后回给开发代理的提示。"""
    return (
        f"已发起第二步审批：删除旧目录 plugins/{plugin_name}/（新代码已暂存于 {staging_name}，旧目录未动）。"
        "请立即停止后续操作；主人同意后框架会自动重新调度你完成收尾与自测。"
    )


async def _request_overwrite_stage(task, plugin_name: str) -> str:
    """覆盖更新第一步：挑临时名 → 记 req-stage → 发起"以临时名安装"审批，返回等待提示。"""
    from gsuid_core.ai_core.planning.kanban import request_subtask_approval

    staging_name = _pick_staging_name(plugin_name)
    if staging_name is None:
        return f"错误：找不到可用的临时安装目录名（{plugin_name}_new* 均被占用）。"
    await _record(_mark(plugin_name, "req-stage"))
    await request_subtask_approval(
        task,
        f"插件「{plugin_name}」在 plugins/ 已有同名目录。将**先以临时名** plugins/{staging_name}/ 安装"
        f"新代码（不动旧目录），安装成功后再单独请求删除旧目录。请求批准这一步安装。",
    )
    logger.info(f"🧩 [PluginDev] 插件 {plugin_name}（覆盖更新）发起第一步安装审批，临时名 {staging_name}")
    return (
        f"已发起第一步审批：以临时名 {staging_name} 安装插件「{plugin_name}」新代码（暂不动旧目录）。"
        "请立即停止后续操作；主人同意后框架会自动重新调度你继续。"
    )


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def copy_to_plugin_dir(ctx: RunContext[ToolContext], plugin_name: str) -> str:
    """把工作区里开发好的插件装到 plugins/——**安装前必须经主人审批**，且绝不直接删同名旧目录。

    非阻塞：把当前 Kanban 任务挂为 waiting_approval 后立即返回（不等用户）；主人用
    ``respond_subtask_approval`` 同意后框架重新调度本任务，重入时推进到下一步。

    安装策略（防误删，按 plugins/ 是否已有同名目录分流）：
    - **新建插件**（无同名目录）：一次审批通过后直接复制进 plugins/<name>。
    - **覆盖更新**（已有同名目录）：分两步、各一次审批，**全程不直接 rmtree 旧目录**——
      ① 先把新代码以"临时名"安装进 plugins/（移动第一步），成功后记录在案；② 再单独审批
      删除旧目录（只精确删记录在案的那一个旧目录），随后把临时目录改回正式名。运行期 data/
      不做特殊保留——data 兼容性由插件自身负责。

    安装完成后再改代码无需重新审批——本工具会识别"本会话已安装"直接重新同步，
    load_plugin_into_core 也会把工作区最新代码同步进 plugins/。审批播报由 Kanban 统一转译。

    Args:
        ctx: 工具执行上下文
        plugin_name: 工作区里待安装的插件名
    """
    src, err = _resolve_in_workspace(plugin_name, "")
    if src is None:
        return err
    if not src.exists() or not any(src.rglob("*.py")):
        return f"错误：工作区里没有可安装的插件 {plugin_name}（缺目录或无 .py 文件），请先 scaffold_plugin 并写代码。"

    dest = (_plugin_root() / plugin_name).resolve()
    if not _is_plugin_child(dest):
        return "错误：目标路径越界。"

    from gsuid_core.ai_core.planning.kanban import request_subtask_approval
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.runtime import get_plan_context

    plan_ctx = get_plan_context()
    task = await AIAgentTask.get_by_id(plan_ctx.task_id) if plan_ctx is not None and plan_ctx.task_id else None
    if task is None:
        return "错误：插件安装必须在 Kanban 任务上下文内发起（以便经主人审批），当前缺任务上下文。"

    logs = await _task_logs()
    phase, staged_name = _install_state(logs, plugin_name)

    # 0) 本会话已安装过 → 直接重新同步工作区最新代码（已获审批，无需再审批）
    if phase == "installed":
        sync_err = _physical_install(src, dest)
        if sync_err:
            return sync_err
        logger.info(f"🧩 [PluginDev] 重新同步已安装插件 {plugin_name} → plugins/")
        return (
            f"✅ 「{plugin_name}」最新代码已重新同步进 plugins/{plugin_name}/（本会话已审批过，无需再审批）。\n"
            "下一步：load_plugin_into_core 重载，再 test_plugin_command 自测。"
        )

    # A) 覆盖更新第二步：删旧目录审批已过 → 收尾（精确删旧 + 临时名改回真名）
    if phase == "do_finalize":
        if not staged_name:
            return "错误：账本缺少暂存目录记录，无法收尾覆盖更新；请重新发起 copy_to_plugin_dir。"
        staged = (_plugin_root() / staged_name).resolve()
        if not staged.is_dir():
            return f"错误：暂存目录 plugins/{staged_name}/ 不存在，无法收尾；请重新发起 copy_to_plugin_dir 重做暂存。"
        fin_err = _finalize_update(dest, staged)
        if fin_err:
            return fin_err
        await _record(_mark(plugin_name, "installed"))
        logger.info(f"🧩 [PluginDev] 覆盖更新收尾完成：删旧目录并把 {staged_name} 改回 {plugin_name}")
        return (
            f"✅ 「{plugin_name}」已覆盖更新到 plugins/{plugin_name}/（旧目录经审批删除、新代码就位）。\n"
            "下一步：load_plugin_into_core 加载，再 test_plugin_command 自测。"
        )

    # B) 覆盖更新第一步：以临时名安装审批已过 → 安装到临时目录（移动第一步）并发起删旧审批
    if phase == "do_stage":
        staging_name = _pick_staging_name(plugin_name)
        if staging_name is None:
            return f"错误：找不到可用的临时安装目录名（{plugin_name}_new* 均被占用）。"
        staging_dest = (_plugin_root() / staging_name).resolve()
        install_err = _physical_install(src, staging_dest)
        if install_err:
            shutil.rmtree(staging_dest, ignore_errors=True)  # 失败清理半成品，不留垃圾目录
            return install_err
        await _record(_mark(plugin_name, f"staged|{staging_name}"))  # 只记录成功的移动
        await _record(_mark(plugin_name, "req-delete"))
        await request_subtask_approval(task, _delete_approval_prompt(plugin_name, staging_name))
        logger.info(f"🧩 [PluginDev] 插件 {plugin_name} 以临时名 {staging_name} 暂存完成，发起删除旧目录审批")
        return _delete_waiting_reply(plugin_name, staging_name)

    # C) 新建插件：安装审批已过 → 直接复制进 plugins/<name>
    if phase == "do_install":
        if dest.exists():
            # 审批窗口内冒出了同名目录：为避免 rmtree 误删，改走"覆盖更新"安全流程并重新审批
            logger.info(f"🧩 [PluginDev] 插件 {plugin_name} 新建审批期间出现同名目录，转覆盖更新安全流程")
            return await _request_overwrite_stage(task, plugin_name)
        install_err = _physical_install(src, dest)
        if install_err:
            return install_err
        await _record(_mark(plugin_name, "installed"))
        logger.info(f"🧩 [PluginDev] 已新建安装插件 {plugin_name} 到 plugins/")
        return (
            f"✅ 「{plugin_name}」已安装到 plugins/{plugin_name}/。\n"
            "下一步：load_plugin_into_core 加载，再 test_plugin_command 自测。"
        )

    # D) 等待中各态（任务已是 waiting_approval，开发代理理应已停手）：原样提醒，别重复发起
    if phase == "await_delete":
        return _delete_waiting_reply(plugin_name, staged_name or "（临时目录）")
    if phase in ("await_install", "await_stage"):
        return f"插件「{plugin_name}」正在等待主人审批，请立即停止后续操作；主人同意后框架会自动重新调度你继续。"

    # E) phase == "none"：首次调用 → 按"新建 / 覆盖更新"分流，发起第一步审批
    if not dest.exists():
        await _record(_mark(plugin_name, "req-install"))
        await request_subtask_approval(task, f"插件「{plugin_name}」（新建插件）已在工作区开发完成，请求安装到框架。")
        logger.info(f"🧩 [PluginDev] 插件 {plugin_name}（新建）发起安装审批，挂起任务等待主人")
        return (
            f"已发起安装审批：插件「{plugin_name}」（新建插件）正在等待主人同意，请立即停止后续操作；"
            "主人同意后框架会自动重新调度你完成安装与自测。"
        )

    return await _request_overwrite_stage(task, plugin_name)


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def load_plugin_into_core(
    ctx: RunContext[ToolContext],
    plugin_name: str,
) -> str:
    """
    把插件热加载进运行中的框架（全新插件首次加载 / 已加载插件重载，均走此工具）。

    **关键：load 前会先把工作区最新代码同步进 plugins/ 再重载**——这样开发期"改工作区
    代码 → load → test"循环每次都跑到最新代码，不会重载到旧 plugins/。同步仅在该插件
    已通过审批（copy_to_plugin_dir）后生效；未审批就有工作区改动会被拦下、提示先走审批。

    复用框架的 reload_plugin：清理该插件旧的 SV / 模块 / 定时任务 / 路由，从 plugins/
    重新发现并 import，再重跑其 @on_core_start 钩子。若返回含 ❌ 即加载失败，按报错改代码后重调。

    Args:
        ctx: 工具执行上下文
        plugin_name: 要加载的插件名

    Returns:
        reload_plugin 的结果文本（成功 ✨ / 失败 ❌ 原样回传）
    """
    plugin_name = _existing_plugin_dirname(plugin_name)

    # 工作区有该插件代码时：仅在"本会话已完成安装"后才同步工作区→plugins/ 再重载（消除"改工作区
    # 却重载旧 plugins/"的死循环）。未安装就直接 load 会被拦下——避免绕开 copy_to_plugin_dir 的
    # 审批与"覆盖更新不直接删旧目录"流程去 rmtree 同名旧目录。
    ws, _ = _resolve_in_workspace(plugin_name, "")
    if ws is not None and ws.exists() and any(ws.rglob("*.py")):
        if not _is_installed(await _task_logs(), plugin_name):
            return "错误：工作区有未安装的插件改动，请先 copy_to_plugin_dir 走主人审批安装，再 load。"
        sync_err = _physical_install(ws, (_plugin_root() / plugin_name).resolve())
        if sync_err:
            return sync_err

    plugin_dir, err = _resolve_in_plugin(plugin_name, "")
    if plugin_dir is None:
        return err
    if not plugin_dir.exists():
        return f"错误：插件目录不存在，请先 scaffold_plugin 并 copy_to_plugin_dir 安装：{plugin_name}"

    from gsuid_core.utils.plugins_update.reload_plugin import reload_plugin

    logger.info(f"🧩 [PluginDev] 请求热加载插件 {plugin_name}")
    # reload_plugin 内部用 get_running_loop().create_task 重跑启动 Hook，必须在
    # 事件循环线程内同步调用（与 core重载插件 命令同链路），不可丢进 to_thread。
    return reload_plugin(plugin_name)


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def test_plugin_command(
    ctx: RunContext[ToolContext],
    plugin_name: str,
    command: str,
    text: str = "",
) -> str:
    """
    功能自测：实跑插件某个命令的处理逻辑，返回它实际产出的内容（回复主人前必做）。

    前置：插件须已 load_plugin_into_core 加载成功。**纯命令插件也能自测**——无需
    声明 to_ai：本工具先找该命令的 to_ai (by_trigger) 工具，找不到再直接从插件的
    SV 触发器注册表按**处理函数名**取原始触发器实跑。两条路径都走 MockBot：命令里
    bot.send 的内容被收集回传、**不会真的发给用户**，但 fetch / 数据库等真实副作用
    会真实发生。

    工作循环：写代码（工作区）→ load_plugin_into_core（会自动把工作区改动同步进 plugins/
    再重载）→ test_plugin_command 实跑核心命令 → 看产出是否符合预期；不对就改代码 →
    重新 load → 再测，直到通过再交付主人。**改完代码必须重新 load 才生效**，别只改不 load。

    只测查询 / 只读类命令。带写入 / 删除 / 不可逆副作用的命令**不要**在这里实跑，
    在交付摘要里标注"需主人手动验证"。若返回提示"找不到该命令处理函数"，那是**终态**
    ——换用提示里列出的处理函数名，或如实标注无法自测后交付，**切勿**为此反复改代码重载。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名（用于校验被测命令确属该插件）
        command: 被测触发器的**处理函数名**（如 "weather_handler"）
        text: 模拟输入——命令/前缀/后缀类传命令词之后的参数（如 "北京"）；正则类(on_regex)
            传用户**完整消息**（如 "北京天气"），框架据此还原 raw_text 与 regex_group；无参命令留空

    Returns:
        插件实际产出的文本 / 资源摘要；或未找到命令 / 执行报错的说明
    """
    ok, err = _validate_plugin_name(plugin_name)
    if not ok:
        return err
    if not command:
        return "错误：command 不能为空，请传被测触发器的处理函数名（如 weather_handler）"
    if ctx.deps.bot is None or ctx.deps.ev is None:
        return "错误：当前执行上下文缺少 bot / ev，无法实跑命令自测。"

    plugin_name = _existing_plugin_dirname(plugin_name)

    from gsuid_core.ai_core.register import get_registered_tools

    # ① 优先走 to_ai (by_trigger) 路径——其包装产出含图片 / 音视频资源 ID，体验最完整。
    registered = get_registered_tools()
    by_trigger = registered.get("by_trigger", {}) if registered else {}
    plugin_ai_tools = {name: tb for name, tb in by_trigger.items() if tb.plugin == plugin_name}
    if command in plugin_ai_tools:
        logger.info(f"🧩 [PluginDev] 自测 to_ai 命令 {plugin_name}.{command}(text={text!r})")
        try:
            result = await plugin_ai_tools[command].tool.function(ctx, text=text)
        except Exception as e:
            logger.exception(f"🧩 [PluginDev] 自测命令 {command} 抛异常: {e}")
            return f"❌ 自测命令 [{command}] 抛出异常：{type(e).__name__}: {e}（请据此改代码后重新加载再测）"
        return f"【命令 {command}(text={text!r}) 实跑结果】\n{result}"

    # ② 纯命令路径——未声明 to_ai 也能测：直接从插件 SV 触发器注册表按处理函数名取原始
    #    触发器，用 MockBot 实跑。纯命令插件本就合法，自测不该强制要求 to_ai。
    pure_triggers = _collect_plugin_triggers(plugin_name)
    if command in pure_triggers:
        from copy import deepcopy

        from gsuid_core.ai_core.trigger_bridge import run_trigger_via_mockbot

        _sv, trig = pure_triggers[command]
        fake_ev = deepcopy(ctx.deps.ev)
        fake_ev.text = text
        if trig.type == "regex":
            import re as _re

            # 正则触发：text 即用户完整消息，raw_text 与之对齐（不能塞正则表达式本身），groups 据此还原
            match = _re.search(trig.keyword, text)
            fake_ev.raw_text = text
            fake_ev.regex_dict = match.groupdict() if match else {}
            fake_ev.regex_group = match.groups() if match else ()
            fake_ev.command = "|".join(g if g is not None else "" for g in match.groups()) if match else text
        else:
            fake_ev.command = trig.keyword
            fake_ev.raw_text = f"{trig.prefix}{trig.keyword}{text}".strip()
        # 关键：实跑**未包装**的原处理函数（__wrapped__），而非 SV 的 modify_func 包装。
        # 后者用 try/except 吞掉一切异常只打日志、返回 None——会让本自测把"处理函数内
        # 真实崩溃"（如 ev.original_message 不存在）误报成"命令已执行但无产出"，导致带病
        # 交付。直接跑原函数，异常会冒泡到下面的 except、如实回报给开发代理。
        raw_func = getattr(trig.func, "__wrapped__", trig.func)
        logger.info(f"🧩 [PluginDev] 自测纯命令 {plugin_name}.{command}(text={text!r})")
        try:
            result = await run_trigger_via_mockbot(ctx.deps.bot, fake_ev, raw_func)
        except Exception as e:
            logger.exception(f"🧩 [PluginDev] 自测命令 {command} 抛异常: {e}")
            return (
                f"❌ 自测命令 [{command}] 抛出异常：{type(e).__name__}: {e}"
                "（这是处理函数内的真实 bug，请据此改代码后重新加载再测，别当成功交付）"
            )
        if not result:
            result = (
                "（命令已执行，但没有任何 bot.send / ai_return 文本或资源产出——"
                "若是纯副作用 / 空输入提示属正常；"
                "本应出图出文却空，要查渲染或取数逻辑）"
            )
        return f"【命令 {command}(text={text!r}) 实跑结果】\n{result}"

    # ③ 两条路径都没命中 → 终态提示，明确叫停"反复改代码重试"的死循环。
    all_names = sorted(set(plugin_ai_tools) | set(pure_triggers))
    if all_names:
        available = "、".join(all_names)
        return (
            f"⚠️ 插件 {plugin_name} 下没有名为 [{command}] 的命令处理函数（这是终态，**别再为此改代码重载**）。"
            f"当前可自测的处理函数名有：{available}。请换用其中之一重测。"
        )
    return (
        f"⚠️ 插件 {plugin_name} 当前没注册任何触发器（这是终态，**别再为此反复改代码重载**）。"
        "常见原因：内层包 __full__.py 没 import 业务子模块、或业务子模块 import 报错导致触发器没注册。"
        "请先确认 load_plugin_into_core 返回 ✨ 成功且业务模块确被导入；若确属纯命令插件且命令已注册"
        "却仍取不到，可跳过自测、直接交付并标注「需主人手动验证」。"
    )


def _collect_plugin_triggers(plugin_name: str) -> Dict[str, tuple]:
    """汇总某插件下所有 SV 的全部触发器（不限 to_ai），按**处理函数名**索引。

    返回 ``{func_name: (sv, trigger)}``。用于让 test_plugin_command 支持纯命令自测：
    遍历全局 ``SL.lst``，取归属该插件（``sv.self_plugin_name == plugin_name``）的 SV，
    展平其 ``TL``（type→keyword→Trigger）。同名处理函数对应多个前缀触发器时只留第一个
    （实跑任意一个等价）。``modify_func`` 用 @wraps 保留了原函数名，故可按 __name__ 匹配。
    """
    from gsuid_core.sv import SL

    result: Dict[str, tuple] = {}
    for sv in SL.lst.values():
        if getattr(sv, "self_plugin_name", None) != plugin_name:
            continue
        trigger_map = getattr(sv, "TL", {}) or {}
        for type_map in trigger_map.values():
            for trig in type_map.values():
                fn = getattr(getattr(trig, "func", None), "__name__", None)
                if fn and fn not in result:
                    result[fn] = (sv, trig)
    return result


def _heading_levels(lines: list[str]) -> Dict[int, int]:
    """返回 ``{行号: 标题级别}``，只含**代码围栏（```）外**的 Markdown ATX 标题行。

    必须跳过围栏内的行——否则 Python / Shell 注释（``# ...``）会被误判为标题，导致
    章节抽取在代码块的注释处提前截断（实测 §三 在 ``# ----- 发送纯文本 -----`` 处断掉）。
    """
    result: Dict[int, int] = {}
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped.startswith("#"):
            continue
        level = len(stripped) - len(stripped.lstrip("#"))
        rest = stripped[level:]
        # ATX 标题要求 # 后跟空格或行尾；过滤 #hashtag 之类
        if level <= 6 and (rest == "" or rest.startswith(" ")):
            result[i] = level
    return result


def _extract_guide_section(text: str, section: str) -> str:
    """从 SKILL 全文里抽出一个标题命中 ``section`` 的章节正文。

    命中规则：找到第一个标题文本包含 ``section``（大小写不敏感）的 Markdown 标题，
    返回从该标题起、直到下一个**同级或更高级**标题前的内容（围栏内的 ``#`` 不算标题）。
    """
    lines = text.splitlines()
    headings = _heading_levels(lines)
    needle = section.strip().lower()

    start = -1
    start_level = 0
    for idx in sorted(headings):
        level = headings[idx]
        heading_text = lines[idx].lstrip()[level:].strip().lower()
        if needle in heading_text:
            start = idx
            start_level = level
            break
    if start < 0:
        return ""

    end = len(lines)
    for idx in sorted(headings):
        if idx > start and headings[idx] <= start_level:
            end = idx
            break

    body = "\n".join(lines[start:end]).strip()
    if len(body) > _GUIDE_MAX_CHARS:
        body = body[:_GUIDE_MAX_CHARS] + "\n\n…（章节过长已截断，可按更细的小节标题再查）"
    return body


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def read_plugin_dev_guide(
    ctx: RunContext[ToolContext],
    section: str = "",
) -> str:
    """
    按需查阅 GsCore 插件开发权威指南（gscore-plugin-development SKILL 全文）。

    指南极长，不要一次性全读。section 留空时返回「目录（全部章节标题）」，再按需传入
    某个章节标题关键词（如 "触发器"、"数据库操作"、"AI 集成：to_ai"、"配置管理"、
    "完整插件示例"）获取该章节正文。

    Args:
        ctx: 工具执行上下文
        section: 章节标题关键词；留空返回目录

    Returns:
        目录或指定章节正文；指南文件缺失 / 章节未命中时返回提示
    """
    if not _SKILL_PATH.exists():
        return f"错误：未找到插件开发指南文件：{_SKILL_PATH}"

    try:
        text = _SKILL_PATH.read_text(encoding="utf-8")
    except OSError as e:
        return f"错误：读取指南失败：{e}"

    if not section.strip():
        lines = text.splitlines()
        toc: list[str] = []
        for idx, level in sorted(_heading_levels(lines).items()):
            title = lines[idx].lstrip()[level:].strip()
            if title and not title.startswith("目录"):
                toc.append(f"{'  ' * (level - 1)}- {title}")
        return "GsCore 插件开发指南目录（传 section=章节标题关键词 读正文）：\n" + "\n".join(toc)

    body = _extract_guide_section(text, section)
    if not body:
        return f"未找到包含「{section}」的章节，请先用空 section 查目录确认标题。"
    return body
