"""系统提示词组的读写。名为「默认」的组每次启动从代码写回，运行时也不读磁盘。"""

from __future__ import annotations

import os
import json
from typing import TypedDict
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.resource import PROMPT_GROUP_PATH
from gsuid_core.utils.path_safety import PathEscapeError, safe_join, is_safe_filename
from gsuid_core.ai_core.prompt_group.render import (
    render_text,
    strip_leading_rule,
    unfold_wrapped_lines,
    strip_hard_cap_clause,
    split_json_output_contract,
    strip_embedded_answer_contract,
)
from gsuid_core.ai_core.prompt_group.catalog import (
    SPECS,
    DEFAULT_GROUP_NAME,
    PromptSpec,
    spec_keys,
    default_body,
    default_prompts,
)


class PromptField(TypedDict):
    key: str
    section: str
    title: str
    description: str
    placeholders: list[str]
    text: str
    frozen_suffix: str


class PromptGroupDetail(TypedDict):
    name: str
    builtin: bool
    fields: list[PromptField]


class PromptGroupSummary(TypedDict):
    name: str
    builtin: bool
    field_count: int


class BuiltinGroupError(ValueError):
    """默认组不允许写入。"""


class PromptGroupError(ValueError):
    """名称非法，或组不存在。"""


def _file(name: str) -> Path:
    if not is_safe_filename(name) or len(name) > 40:
        raise PromptGroupError("名称无效")
    try:
        return safe_join(PROMPT_GROUP_PATH, name, "prompts.json")
    except PathEscapeError as e:
        raise PromptGroupError("名称无效") from e


def is_builtin_group(name: str) -> bool:
    return name == DEFAULT_GROUP_NAME


def group_exists(name: str) -> bool:
    if is_builtin_group(name):
        return True
    try:
        return _file(name).is_file()
    except PromptGroupError:
        return False


def _read_custom(name: str) -> dict[str, str]:
    path = _file(name)
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(t("log.persona.prompt_group_unreadable", name=name, e=e))
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(t("log.persona.prompt_group_unreadable", name=name, e=e))
        return {}
    if not isinstance(loaded, dict):
        return {}
    bucket = loaded["prompts"] if "prompts" in loaded else None
    if not isinstance(bucket, dict):
        return {}
    allowed = spec_keys()
    out: dict[str, str] = {}
    for key, value in bucket.items():
        if isinstance(key, str) and isinstance(value, str) and key in allowed:
            out[key] = value
    return out


def group_name_for_persona(persona_name: str | None) -> str:
    """人格 config.json 里的 prompt_group。缺文件或组已删则回到默认组。"""
    if not persona_name:
        return DEFAULT_GROUP_NAME
    from gsuid_core.ai_core.resource import PERSONA_PATH
    from gsuid_core.utils.path_safety import PathEscapeError, safe_join
    from gsuid_core.ai_core.persona.config import persona_config_manager

    try:
        path = safe_join(PERSONA_PATH, persona_name, "config.json")
    except PathEscapeError:
        return DEFAULT_GROUP_NAME
    if not path.is_file():
        return DEFAULT_GROUP_NAME
    raw = persona_config_manager.get_config(persona_name).get_config("prompt_group").data
    if isinstance(raw, str):
        chosen = raw.strip()
        if chosen and group_exists(chosen):
            return chosen
    return DEFAULT_GROUP_NAME


def effective_text(persona_name: str | None, key: str) -> str:
    if key not in spec_keys():
        return default_body(key)
    group = group_name_for_persona(persona_name)
    if is_builtin_group(group):
        return default_body(key)
    custom = _read_custom(group)
    if key in custom:
        return custom[key]
    return default_body(key)


def _json_contract(key: str) -> str:
    """默认正文里从「以严格 JSON 格式输出」起的尾句。没有这句则是空串。"""
    _, tail = split_json_output_contract(default_body(key))
    return tail


def render_prompt(persona_name: str | None, key: str, **values: str) -> str:
    text = effective_text(persona_name, key)
    if key == "speech_block":
        text = strip_hard_cap_clause(strip_embedded_answer_contract(text))
    # 先切模板、折续行，再填占位。群记录里的缩进行不能被折掉。
    contract = _json_contract(key)
    if contract:
        text, _ = split_json_output_contract(text)
    text = render_text(unfold_wrapped_lines(text), values)
    if contract:
        tail = unfold_wrapped_lines(contract)
        text = f"{text}\n\n{tail}" if text and tail else text or tail
    if key == "closing":
        return strip_leading_rule(text)
    return text


def group_file_mtime(persona_name: str) -> float:
    name = group_name_for_persona(persona_name)
    try:
        path = _file(name)
    except PromptGroupError:
        return 0.0
    if path.is_file():
        return path.stat().st_mtime
    return 0.0


def _payload(prompts: dict[str, str]) -> str:
    return json.dumps({"prompts": prompts}, ensure_ascii=False, indent=2) + "\n"


def _write_text(path: Path, text: str) -> None:
    """先写临时文件再替换。中途失败时原来的 prompts.json 还在。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def sync_default_group() -> bool:
    """把代码里的默认正文写回默认组。内容相同则不改文件时间。"""
    path = _file(DEFAULT_GROUP_NAME)
    text = _payload(default_prompts())
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    _write_text(path, text)
    return True


def list_groups() -> list[PromptGroupSummary]:
    names: set[str] = {DEFAULT_GROUP_NAME}
    if PROMPT_GROUP_PATH.is_dir():
        for child in PROMPT_GROUP_PATH.iterdir():
            if child.is_dir() and is_safe_filename(child.name) and (child / "prompts.json").is_file():
                names.add(child.name)
    ordered = [DEFAULT_GROUP_NAME] + sorted(n for n in names if n != DEFAULT_GROUP_NAME)
    return [PromptGroupSummary(name=name, builtin=is_builtin_group(name), field_count=len(SPECS)) for name in ordered]


def _fields(name: str, prompts: dict[str, str]) -> list[PromptField]:
    rows: list[PromptField] = []
    for spec in SPECS:
        if spec.key not in prompts:
            raw = default_body(spec.key)
        else:
            raw = prompts[spec.key]
        if spec.key == "speech_block":
            raw = strip_hard_cap_clause(strip_embedded_answer_contract(raw))
        text = unfold_wrapped_lines(raw)
        if spec.key == "closing":
            text = strip_leading_rule(text)
        if _json_contract(spec.key):
            text, _ = split_json_output_contract(text)
        rows.append(_field(spec, text))
    return rows


def _field(spec: PromptSpec, text: str) -> PromptField:
    return PromptField(
        key=spec.key,
        section=spec.section,
        title=spec.title,
        description=spec.description,
        placeholders=list(spec.placeholders),
        text=text,
        frozen_suffix=_json_contract(spec.key),
    )


def group_detail(name: str) -> PromptGroupDetail:
    if not group_exists(name):
        raise PromptGroupError("系统提示词组不存在")
    if is_builtin_group(name):
        prompts = default_prompts()
    else:
        prompts = _read_custom(name)
    return PromptGroupDetail(name=name, builtin=is_builtin_group(name), fields=_fields(name, prompts))


def _clean_prompts(prompts: dict[str, str]) -> dict[str, str]:
    allowed = spec_keys()
    unknown = [key for key in prompts if key not in allowed]
    if unknown:
        raise PromptGroupError("含有未知字段")
    cleaned: dict[str, str] = {}
    for spec in SPECS:
        if spec.key in prompts:
            text = prompts[spec.key]
        else:
            text = default_body(spec.key)
        if spec.key == "speech_block":
            text = strip_hard_cap_clause(strip_embedded_answer_contract(text))
        if spec.key == "closing":
            text = strip_leading_rule(text)
        if _json_contract(spec.key):
            text, _ = split_json_output_contract(text)
        cleaned[spec.key] = text
    return cleaned


def save_group(name: str, prompts: dict[str, str]) -> PromptGroupDetail:
    if is_builtin_group(name):
        raise BuiltinGroupError("默认系统提示词组每次启动从程序写回，不能修改")
    if not group_exists(name):
        raise PromptGroupError("系统提示词组不存在")
    cleaned = _clean_prompts(prompts)
    _write_text(_file(name), _payload(cleaned))
    return group_detail(name)


def create_group(name: str, *, copy_from: str | None = None) -> PromptGroupDetail:
    if is_builtin_group(name):
        raise BuiltinGroupError("不能新建名为默认的组")
    if not is_safe_filename(name) or len(name) > 40:
        raise PromptGroupError("名称无效")
    if group_exists(name):
        raise PromptGroupError("同名组已存在")
    source = copy_from or DEFAULT_GROUP_NAME
    if not group_exists(source):
        raise PromptGroupError("复制来源不存在")
    if is_builtin_group(source):
        prompts = default_prompts()
    else:
        custom = _read_custom(source)
        prompts = {spec.key: custom[spec.key] if spec.key in custom else default_body(spec.key) for spec in SPECS}
    prompts["closing"] = strip_leading_rule(prompts["closing"])
    prompts["speech_block"] = strip_hard_cap_clause(strip_embedded_answer_contract(prompts["speech_block"]))
    for spec in SPECS:
        if spec.key in prompts and _json_contract(spec.key):
            prompts[spec.key], _ = split_json_output_contract(prompts[spec.key])
    _write_text(_file(name), _payload(prompts))
    return group_detail(name)


def _unbind_personas(name: str) -> None:
    """删组前把仍选中它的人格改回默认组。"""
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.persona.resource import list_available_personas

    for persona_name in list_available_personas():
        raw = persona_config_manager.get_config(persona_name).get_config("prompt_group").data
        if isinstance(raw, str) and raw.strip() == name:
            persona_config_manager.set_prompt_group(persona_name, DEFAULT_GROUP_NAME)


def delete_group(name: str) -> None:
    if is_builtin_group(name):
        raise BuiltinGroupError("默认系统提示词组不能删除")
    path = _file(name)
    if not path.is_file():
        raise PromptGroupError("系统提示词组不存在")
    _unbind_personas(name)
    path.unlink()
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
