"""文档内容提取模块

提供文档内容提取管道，支持 PDF、Word、Excel 等格式转换为 Markdown 文本。
用于 AI 处理用户发送的文件/文档消息。

使用方式:
    from gsuid_core.ai_core.multimodal.document import extract_document_content

    markdown = await extract_document_content(file_data=b"...", filename="report.pdf")
"""

import os

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.utils import (
    get_mcp_tool_id,
    is_mcp_provider,
    cleanup_tempfile,
    sanitize_mcp_text,
    build_mcp_arguments,
    call_mcp_tool_checked,
    save_binary_to_tempfile,
)
from gsuid_core.ai_core.configs.ai_config import ai_config

# 支持的文档格式
SUPPORTED_DOCUMENT_FORMATS = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
}


def _get_doc_provider() -> str:
    """获取当前配置的文档提取服务提供方

    Returns:
        提供方名称，如 "MCP"
    """
    return ai_config.get_config("document_extract_provider").data


def _get_file_extension(filename: str) -> str:
    """从文件名中提取扩展名（小写）

    Args:
        filename: 文件名

    Returns:
        小写的文件扩展名（含点号），如 ".pdf"
    """
    _, ext = os.path.splitext(filename)
    return ext.lower()


def is_supported_document(filename: str) -> bool:
    """检查文件是否为支持的文档格式

    Args:
        filename: 文件名

    Returns:
        True 如果是支持的文档格式
    """
    ext = _get_file_extension(filename)
    return ext in SUPPORTED_DOCUMENT_FORMATS


async def extract_document_content(
    file_data: bytes,
    filename: str,
    page_range: str | None = None,
) -> str:
    """统一的文档内容提取接口

    根据用户配置的 document_extract_provider 自动选择文档提取服务。
    将文档内容转换为 Markdown 格式的文本。

    Args:
        file_data: 文件二进制数据
        filename: 文件名（用于判断格式）
        page_range: 页码范围（如 "1-5"），仅对 PDF 等分页文档有效

    Returns:
        文档内容的 Markdown 文本

    Raises:
        RuntimeError: 文档提取失败时抛出
        ValueError: 不支持的文件格式时抛出

    Example:
        >>> content = await extract_document_content(pdf_bytes, "report.pdf")
        >>> print(content)
        "# 报告标题\n\n## 第一章\n..."
    """
    ext = _get_file_extension(filename)

    if ext not in SUPPORTED_DOCUMENT_FORMATS:
        raise ValueError(
            t("不支持的文档格式: {ext}。支持的格式: {p0}", ext=ext, p0=", ".join(SUPPORTED_DOCUMENT_FORMATS.keys()))
        )

    # 纯文本文件直接读取
    if ext in (".txt", ".md", ".csv", ".json", ".xml", ".html"):
        try:
            return file_data.decode("utf-8")
        except UnicodeDecodeError:
            return file_data.decode("gbk", errors="replace")

    provider = _get_doc_provider()

    if is_mcp_provider(provider):
        mcp_tool_id = get_mcp_tool_id("document_extract_mcp_tool_id", "Document Extract")

        ext = _get_file_extension(filename)
        file_path = await save_binary_to_tempfile(file_data, ext, "📄 [Document]")

        arguments = build_mcp_arguments(
            "document_extract_mcp_tool_id",
            {"file_source": file_path, "page_range": page_range},
        )

        try:
            result = await call_mcp_tool_checked(mcp_tool_id, arguments, "Document Extract")
            return sanitize_mcp_text(result.text)
        finally:
            cleanup_tempfile(file_path, "📄 [Document]")

    # 未知 provider
    logger.warning(t("📄 [Document] 未知的提供方 '{provider}'，仅支持 MCP", provider=provider))
    raise RuntimeError(t("文档提取不支持该提供方: {provider}", provider=provider))
