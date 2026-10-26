"""从工具轨迹里抽出能对外说的事实，并读模型文末的四行。"""

import re

from gsuid_core.ai_core.autonomy.models import (
    FactPack,
    DriveKind,
    ParsedRun,
    AutonomyStepTrace,
)

# 这些工具只是在看菜单或翻记忆，不算「已经帮人做成一件事」。
META_TOOLS = frozenset({"list_plugin_commands", "find_tools", "search_cognition", "recall_cognition"})
FAIL_MARKS = ("未绑定", "失败", "超时", "权限不足", "拒绝：", "没有在线的适配器", "不代发")
_RESOURCE = re.compile(r"res_[A-Za-z0-9_-]+")
_FOOTER = {
    "DRIVE": "drive",
    "TARGET": "target",
    "SUMMARY": "summary",
    "TRAILS": "trails",
}


def output_is_failed(text: str) -> bool:
    body = text.strip()
    if not body:
        return True
    return any(mark in body for mark in FAIL_MARKS)


def extract_facts(steps: list[AutonomyStepTrace]) -> FactPack:
    verified: list[str] = []
    observations: list[str] = []
    resources: list[str] = []
    for step in steps:
        if step.is_error or output_is_failed(step.output_text):
            continue
        verified.append(step.tool_name)
        observations.append(step.output_text.strip()[:600])
        resources.extend(step.resource_ids)
        for match in _RESOURCE.findall(step.output_text):
            if match not in resources:
                resources.append(match)
    substantive = any(name not in META_TOOLS for name in verified)
    return FactPack(
        verified_tools=verified,
        factual_observations=observations,
        generated_resources=resources,
        has_substantive_outcome=substantive,
    )


def parse_footer(text: str) -> ParsedRun:
    """自由文本。缺行或动力对不上，一律休息，避免把草稿当结论。"""
    found: dict[str, str] = {}
    body_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("-").strip()
        if "：" in line:
            key, _, value = line.partition("：")
        elif ":" in line:
            key, _, value = line.partition(":")
        else:
            body_lines.append(raw_line)
            continue
        name = key.strip().upper()
        if name in _FOOTER:
            if _FOOTER[name] not in found:
                found[_FOOTER[name]] = value.strip()
            continue
        body_lines.append(raw_line)
    drive_raw = found["drive"] if "drive" in found else ""
    trails_raw = found["trails"] if "trails" in found else ""
    trails = [item.strip() for item in trails_raw.split("|") if item.strip()]
    spoken = "\n".join(body_lines).strip()
    return ParsedRun(
        drive=_drive_of(drive_raw),
        target=found["target"] if "target" in found else "",
        summary=found["summary"] if "summary" in found else "",
        trails=trails[:8],
        spoken_text=spoken,
    )


def _drive_of(raw: str) -> DriveKind:
    if raw == "care_user":
        return "care_user"
    if raw == "group_topic":
        return "group_topic"
    if raw == "self_explore":
        return "self_explore"
    return "rest"


def fact_summary(pack: FactPack) -> str:
    parts = [item[:200] for item in pack.factual_observations[:6]]
    return "；".join(parts)
