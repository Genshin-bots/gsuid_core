"""把命令和已发出的回执写成会话里要留下的一轮。"""

from __future__ import annotations

from typing import Sequence

from pydantic_ai.messages import ImageUrl, UserContent, BinaryContent

from .capture import MAX_IMAGES, SentPiece

_TEXT_CAP = 8_000
_MARK = "[命令回执]"


def receipt_parts(
    command: str,
    pieces: Sequence[SentPiece],
    plugin_label: str,
) -> tuple[list[UserContent], str]:
    """用户侧留下原话、来源和图片；助手侧留下发出的文字。"""
    spoken = command.strip() or "（这条命令没有文字）"
    if len(spoken) > 2_000:
        spoken = spoken[:2_000] + "…[命令过长，已截断]"

    texts: list[str] = []
    images: list[UserContent] = []
    skipped_images = 0
    for piece in pieces:
        if piece.kind == "text" and piece.text.strip():
            texts.append(piece.text.strip())
            continue
        if piece.kind != "image":
            continue
        if piece.omitted:
            skipped_images += 1
            continue
        if len(images) >= MAX_IMAGES:
            skipped_images += 1
            continue
        if piece.image is not None and piece.media_type:
            images.append(BinaryContent(data=piece.image, media_type=piece.media_type))
            continue
        if piece.image_url:
            images.append(ImageUrl(url=piece.image_url))
            continue

    body = "\n".join(texts)
    if len(body) > _TEXT_CAP:
        body = body[:_TEXT_CAP] + "\n…[回执过长，已截断]"

    lines = [f"{_MARK} 用户说：{spoken}"]
    if plugin_label:
        lines.append(f"来源：{plugin_label}")
    lines.append("下面是你已经发出的回执。图片是你发的，不是用户发来的。")
    if body:
        lines.append("发出的文字：\n" + body)
    if images:
        lines.append(f"发出的图片：{len(images)} 张，附在本条后面。")
    if skipped_images:
        lines.append(f"另有 {skipped_images} 张图片没有放入会话。")

    user: list[UserContent] = ["\n".join(lines), *images]
    if images and body:
        assistant = f"{_MARK} 已发出文字和 {len(images)} 张图片。\n{body}"
    elif images:
        assistant = f"{_MARK} 已发出 {len(images)} 张图片。"
    else:
        assistant = f"{_MARK} {body}" if body else f"{_MARK} 已发出回执。"
    return user, assistant
