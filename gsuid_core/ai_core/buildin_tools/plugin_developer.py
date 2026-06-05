"""
插件开发工具模块

提供让 AI「插件开发代理」(``plugin_developer_agent``) 在
``gsuid_core/plugins/<Name>/`` 目录下脚手架、读写、自检并热加载一个 GsCore 插件的能力。

## 与 ``file_manager.py`` 的区别（关键）

``file_manager`` 的读写被框架**强制沙箱**到 Kanban / ad-hoc Artifact Workspace
（见 ``file_manager._get_safe_path`` → ``planning.workspace.resolve_safe_path``），
够不到真正的插件目录；本模块是「被授权写入 ``plugins/``」的**专用通道**：

- 每个工具都用 ``check_pm`` 限定**仅主人 (PM=0)** 可触发（与 ``execute_shell_command``
  同级信任面——主人本就能 ``core重载插件`` / 跑 shell）。
- 路径强制限定在**单个插件目录**内（``_resolve_in_plugin`` 做 ``resolve()`` 后的
  归属校验），杜绝路径穿越 / 跨插件写入。
- 工具单独归入 ``category="plugin_dev"``，并被 ``rag.tools.NON_SEARCHABLE_TOOL_CATEGORIES``
  登记为「永不可检索」：既**不进**主人格保底池(self/buildin)、也**永不**被任何 Agent
  （主人格 / 通用子代理 / 其它能力代理补充检索）的向量检索召回。只有
  ``plugin_developer_agent`` 画像在 ``tool_names`` 里显式引用时才装配
  （``runner._resolve_tools`` 走 ``get_all_tools`` 按名取、不经向量检索）。

热加载复用框架既有的 ``reload_plugin()``——它对全新插件目录同样有效（清理阶段对
新插件 no-op，随后 ``gss.load_plugin`` 从 ``PLUGIN_PATH`` 发现新目录并导入、重跑
``@on_core_start`` 钩子）。
"""

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
        f'    alias=["{show_name}"],\n'
        ")\n\n"
        "# 已放置 __full__.py：框架会自动遍历内层包下所有子模块并 import，\n"
        "# 触发各子模块的 @sv.on_xxx 注册，无需在此手动 from . import xxx。\n"
    )

    pyproject = (
        "[project]\n"
        f'name = "{plugin_name.lower()}"\n'
        'version = "1.0.0"\n'
        f'description = "{desc}"\n'
        f'authors = [{{ name = "{author_name}" }}]\n'
        "dependencies = []\n"
        "\n"
        "# 启动时自动安装 dependencies 中声明的依赖。\n"
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
        f"{plugin_name}/__full__.py": "",  # 空文件：标记扫描子目录全部导入
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
    脚手架：在 plugins/ 下创建一个全新 GsCore 插件的标准嵌套加载骨架。

    生成外层插件包（__init__.py / __nest__.py / pyproject.toml / README.md）与内层
    同名 Python 包（__init__.py 含 Plugins(...) / __full__.py / version.py）。这是
    开发一个插件的第一步；之后用 write_plugin_file 往里写业务子模块。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名（同时是目录名与内层包名），只能含字母/数字/下划线，
            不能以下划线开头。例如 "HelloWorld"、"MyGameUID"。
        display_name: 给用户看的展示名（别名），留空则用 plugin_name。
        description: 插件一句话描述，写入 README / pyproject。
        force_prefix: 强制命令前缀（用户必须以此开头才触发）。例如 "hw"、"我的插件"。
            留空则该插件允许无前缀触发（allow_empty_prefix=True，慎用）。
        author: 作者名，写入 pyproject，留空默认 "plugin_developer_agent"。

    Returns:
        创建结果与已生成文件清单；目录已存在且非空时返回错误。
    """
    plugin_dir, err = _resolve_in_plugin(plugin_name, "")
    if plugin_dir is None:
        return err

    if plugin_dir.exists() and any(plugin_dir.iterdir()):
        return f"错误：插件目录已存在且非空：{plugin_name}（如需修改请用 write_plugin_file，勿重复脚手架）"

    files = _skeleton_files(plugin_name, display_name, description, force_prefix, author)
    try:
        for rel_path, content in files.items():
            target, sub_err = _resolve_in_plugin(plugin_name, rel_path)
            if target is None:
                return sub_err
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.exception(f"🧩 [PluginDev] 脚手架插件 {plugin_name} 失败: {e}")
        return f"错误：脚手架失败：{e}"

    logger.info(f"🧩 [PluginDev] 已脚手架插件 {plugin_name}（{len(files)} 个文件）")
    listing = "\n".join(f"  - {p}" for p in files)
    return (
        f"成功创建插件骨架 {plugin_name}，包含以下文件：\n{listing}\n\n"
        "下一步：用 write_plugin_file 写业务子模块（如 "
        f"{plugin_name}/{plugin_name.lower()}_xxx/__init__.py），"
        "完成后用 validate_plugin 自检、load_plugin_into_core 热加载。"
    )


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def write_plugin_file(
    ctx: RunContext[ToolContext],
    plugin_name: str,
    file_path: str,
    content: str,
    overwrite: bool = True,
) -> str:
    """
    向指定插件目录内写入 / 覆盖一个文件（自动创建父目录）。

    只能写到 plugins/<plugin_name>/ 之内，禁止路径穿越。用于往脚手架好的插件里
    补充业务代码（触发器、配置、数据库模型、帮助、渲染、AI 工具等）。

    Args:
        ctx: 工具执行上下文
        plugin_name: 目标插件名（须已 scaffold_plugin 创建，或正在编辑的现有插件）
        file_path: 相对插件根目录的路径，例如
            "HelloWorld/helloworld_main/__init__.py"
        content: 文件完整内容
        overwrite: 文件已存在时是否覆盖，默认 True；False 时已存在则报错

    Returns:
        写入结果信息
    """
    if not file_path:
        return "错误：文件路径不能为空"

    target, err = _resolve_in_plugin(plugin_name, file_path)
    if target is None:
        return err
    if target == (_plugin_root() / plugin_name).resolve():
        return "错误：file_path 不能指向插件根目录本身"

    if target.exists() and not target.is_file():
        return f"错误：目标路径不是文件：{file_path}"
    if target.exists() and not overwrite:
        return f"错误：文件已存在且 overwrite=False：{file_path}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.exception(f"🧩 [PluginDev] 写入 {plugin_name}/{file_path} 失败: {e}")
        return f"错误：写入文件失败：{e}"

    logger.info(f"🧩 [PluginDev] 写入文件 {plugin_name}/{file_path}（{len(content)} 字符）")
    return f"成功写入文件：{plugin_name}/{file_path}"


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def read_plugin_file(
    ctx: RunContext[ToolContext],
    plugin_name: str,
    file_path: str,
) -> str:
    """
    读取指定插件目录内某个文件的内容。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名
        file_path: 相对插件根目录的文件路径

    Returns:
        文件内容；读取失败时返回错误信息
    """
    if not file_path:
        return "错误：文件路径不能为空"

    target, err = _resolve_in_plugin(plugin_name, file_path)
    if target is None:
        return err
    if not target.exists():
        return f"错误：文件不存在：{file_path}"
    if not target.is_file():
        return f"错误：路径不是文件：{file_path}"

    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"错误：文件非 UTF-8 文本，无法读取：{file_path}"
    except OSError as e:
        return f"错误：读取文件失败：{e}"


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def list_plugin_tree(
    ctx: RunContext[ToolContext],
    plugin_name: str,
    sub_path: str = "",
) -> str:
    """
    递归列出指定插件目录（或其子目录）的文件树。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名
        sub_path: 相对插件根目录的子目录，默认空（列整个插件目录）

    Returns:
        缩进表示层级的文件树；目录不存在时返回错误
    """
    root, err = _resolve_in_plugin(plugin_name, sub_path)
    if root is None:
        return err
    if not root.exists():
        return f"错误：目录不存在：{plugin_name}/{sub_path}".rstrip("/")
    if not root.is_dir():
        return f"错误：路径不是目录：{sub_path}"

    lines: list[str] = []

    def _walk(directory: Path, depth: int) -> None:
        for entry in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name)):
            indent = "  " * depth
            if entry.is_dir():
                lines.append(f"{indent}📁 {entry.name}/")
                _walk(entry, depth + 1)
            else:
                lines.append(f"{indent}📄 {entry.name}")

    _walk(root, 0)
    if not lines:
        return f"目录为空：{plugin_name}/{sub_path}".rstrip("/")
    label = f"{plugin_name}/{sub_path}".rstrip("/")
    return f"{label} 文件树：\n" + "\n".join(lines)


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def delete_plugin_path(
    ctx: RunContext[ToolContext],
    plugin_name: str,
    file_path: str,
) -> str:
    """
    删除指定插件目录内的单个文件（用于开发期纠错，不能删目录、不能删插件根）。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名
        file_path: 相对插件根目录的文件路径

    Returns:
        删除结果
    """
    if not file_path:
        return "错误：文件路径不能为空"

    target, err = _resolve_in_plugin(plugin_name, file_path)
    if target is None:
        return err
    if target == (_plugin_root() / plugin_name).resolve():
        return "错误：禁止删除插件根目录"
    if not target.exists():
        return f"错误：文件不存在：{file_path}"
    if target.is_dir():
        return f"错误：本工具只删单个文件，不删目录：{file_path}"

    try:
        target.unlink()
    except OSError as e:
        return f"错误：删除失败：{e}"

    logger.info(f"🧩 [PluginDev] 删除文件 {plugin_name}/{file_path}")
    return f"成功删除文件：{plugin_name}/{file_path}"


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def validate_plugin(
    ctx: RunContext[ToolContext],
    plugin_name: str,
) -> str:
    """
    语法自检：对插件目录下所有 .py 文件做 py_compile，报告语法错误。

    在 load_plugin_into_core 热加载之前调用，提前发现语法错误，避免把坏代码加载进
    运行中的框架。注意：它只检查语法，不能发现 import 错误或运行时错误——那些要靠
    load_plugin_into_core 的返回信息发现。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名

    Returns:
        "✅ 语法检查通过" 或逐条列出 文件:行 的语法错误
    """
    plugin_dir, err = _resolve_in_plugin(plugin_name, "")
    if plugin_dir is None:
        return err
    if not plugin_dir.exists():
        return f"错误：插件目录不存在：{plugin_name}"

    py_files = sorted(plugin_dir.rglob("*.py"))
    if not py_files:
        return f"插件 {plugin_name} 下没有 .py 文件可检查"

    errors: list[str] = []
    for py_file in py_files:
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            rel = py_file.relative_to(plugin_dir)
            errors.append(f"  - {plugin_name}/{rel}: {e.msg.strip()}")

    if errors:
        return f"❌ 语法检查发现 {len(errors)} 处错误：\n" + "\n".join(errors)
    return f"✅ 语法检查通过（共 {len(py_files)} 个 .py 文件）"


@ai_tools(category="plugin_dev", check_func=check_pm, capability_domain="插件开发")
async def load_plugin_into_core(
    ctx: RunContext[ToolContext],
    plugin_name: str,
) -> str:
    """
    把插件热加载进运行中的框架（全新插件首次加载 / 已加载插件重载，均走此工具）。

    复用框架的 reload_plugin：清理该插件旧的 SV / 模块 / 定时任务 / 路由（全新插件
    为 no-op），从 plugins/ 重新发现并 import，再重跑其 @on_core_start 钩子。加载后
    该插件的命令立即生效。若返回信息含 ❌，说明加载失败——按报错改代码后重新调用。

    Args:
        ctx: 工具执行上下文
        plugin_name: 要加载的插件名

    Returns:
        reload_plugin 的结果文本（成功 ✨ / 失败 ❌ 原样回传）
    """
    plugin_dir, err = _resolve_in_plugin(plugin_name, "")
    if plugin_dir is None:
        return err
    if not plugin_dir.exists():
        return f"错误：插件目录不存在，请先 scaffold_plugin：{plugin_name}"

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

    前置：插件须已 load_plugin_into_core 加载成功，且被测命令在
    `@sv.on_xxx(..., to_ai="...")` 里声明了 to_ai（这样它会被注册成可调用的
    by_trigger AI 工具，本工具据此实跑它的处理函数）。执行走 MockBot：命令里
    bot.send 的内容被收集回传、**不会真的发给用户**，但 fetch / 数据库等真实副作用
    会真实发生。

    工作循环：写完代码 → load_plugin_into_core → test_plugin_command 实跑核心命令
    → 看产出是否符合预期；不对就改代码 → 重新 load → 再测，直到通过再交付主人。

    只测查询 / 只读类命令。带写入 / 删除 / 不可逆副作用的命令**不要**在这里实跑，
    在交付摘要里标注"需主人手动验证"。被测命令没写 to_ai 时本工具找不到它——要么给
    它补上 to_ai（推荐，顺便获得 AI 调用能力），要么如实标注无法自测。

    Args:
        ctx: 工具执行上下文
        plugin_name: 插件名（用于校验被测命令确属该插件）
        command: 被测触发器的**处理函数名**（如 "weather_suffix"）
        text: 模拟用户在命令后输入的参数（如 "北京"）；无参命令留空

    Returns:
        插件实际产出的文本 / 资源摘要；或未找到命令 / 执行报错的说明
    """
    ok, err = _validate_plugin_name(plugin_name)
    if not ok:
        return err
    if not command:
        return "错误：command 不能为空，请传被测触发器的处理函数名（如 weather_suffix）"
    if ctx.deps.bot is None or ctx.deps.ev is None:
        return "错误：当前执行上下文缺少 bot / ev，无法实跑命令自测。"

    from gsuid_core.ai_core.register import get_registered_tools

    registered = get_registered_tools()
    if "by_trigger" not in registered:
        return (
            "错误：当前没有任何 to_ai 触发器工具可自测。请确认插件已 load_plugin_into_core "
            '加载成功，且被测命令声明了 to_ai（@sv.on_xxx(..., to_ai="...")）。'
        )

    plugin_tools = {name: tb for name, tb in registered["by_trigger"].items() if tb.plugin == plugin_name}
    if not plugin_tools:
        return (
            f"错误：插件 {plugin_name} 没有可自测的 to_ai 触发器。请确认已 load_plugin_into_core "
            '加载成功，且要测的命令在 @sv.on_xxx(..., to_ai="...") 里声明了 to_ai。'
        )
    if command not in plugin_tools:
        available = "、".join(sorted(plugin_tools))
        return (
            f"错误：插件 {plugin_name} 下没有名为 [{command}] 的命令处理函数。可自测的命令（处理函数名）有：{available}"
        )

    tool_base = plugin_tools[command]
    logger.info(f"🧩 [PluginDev] 自测命令 {plugin_name}.{command}(text={text!r})")
    try:
        # tool_base.tool 是 by_trigger 包装出的 pydantic_ai Tool，.function 即 trigger_bridge
        # 的 _ai_tool_wrapper：用 MockBot 实跑原处理函数、收集产出并回传（见 trigger_bridge）。
        result = await tool_base.tool.function(ctx, text=text)
    except Exception as e:
        logger.exception(f"🧩 [PluginDev] 自测命令 {command} 抛异常: {e}")
        return f"❌ 自测命令 [{command}] 抛出异常：{type(e).__name__}: {e}（请据此改代码后重新加载再测）"
    return f"【命令 {command}(text={text!r}) 实跑结果】\n{result}"


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
