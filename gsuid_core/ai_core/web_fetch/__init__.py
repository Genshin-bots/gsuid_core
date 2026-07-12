"""
Web Fetch 模块

提供网页内容抓取并转换为 Markdown 格式的功能。
使用 aiohttp 进行异步 HTTP 请求，使用 BeautifulSoup 清理 HTML，
使用 markdownify 将 HTML 转换为 Markdown。
"""

import aiohttp
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# 默认请求头，模拟浏览器访问
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 默认超时时间（秒）
DEFAULT_TIMEOUT = 30

# 默认最大内容长度（字符数），防止超大页面
MAX_CONTENT_LENGTH = 100_000


async def fetch_webpage_as_markdown(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_length: int = MAX_CONTENT_LENGTH,
) -> str:
    """
    抓取指定 URL 的网页内容并转换为 Markdown 格式

    Args:
        url: 要抓取的网页 URL
        timeout: 请求超时时间（秒），默认 30 秒
        max_length: 返回 Markdown 内容的最大字符数，默认 100000

    Returns:
        转换后的 Markdown 文本内容

    Raises:
        ValueError: URL 无效或响应状态码异常
        aiohttp.ClientError: 网络请求错误
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(t("无效的 URL: {url}，必须以 http:// 或 https:// 开头", url=url))

    logger.info(t("🌐 [WebFetch] 正在抓取网页: {url}", url=url))

    try:
        async with aiohttp.ClientSession(
            headers=DEFAULT_HEADERS,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise ValueError(t("请求失败，状态码: {p0}，URL: {url}", p0=response.status, url=url))

                # 检查 Content-Type，确保是 HTML 页面
                content_type = response.content_type or ""
                if "text" not in content_type and "html" not in content_type:
                    raise ValueError(
                        t("不支持的内容类型: {content_type}，仅支持 HTML/文本页面", content_type=content_type)
                    )

                html_content = await response.text()

    except aiohttp.ClientError as e:
        logger.error(t("🌐 [WebFetch] 网络请求失败: {url}, 错误: {e}", url=url, e=e))
        raise ValueError(t("网络请求失败: {e}", e=e)) from e

    # 使用 BeautifulSoup 清理 HTML，完全移除无关标签及其内容
    try:
        soup = BeautifulSoup(html_content, "lxml")

        # 完全移除这些标签及其所有内容（包括 CSS、JS 代码）
        for tag_name in [
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
            "nav",
            "footer",
            "header",
        ]:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 移除 HTML 注释
        from bs4 import Comment

        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # 优先提取主要内容区域，减少噪音
        main_content = (
            soup.find("article")
            or soup.find("main")
            or soup.find(attrs={"role": "main"})
            or soup.find("div", class_="markdown-body")
            or soup.find("div", id="content")
            or soup.find("body")
            or soup
        )

        cleaned_html = str(main_content)

    except Exception as e:
        logger.error(t("🌐 [WebFetch] HTML 清理失败: {url}, 错误: {e}", url=url, e=e))
        raise ValueError(t("HTML 清理失败: {e}", e=e)) from e

    # 使用 markdownify 将清理后的 HTML 转换为 Markdown
    try:
        markdown_content = md(
            cleaned_html,
            heading_style="ATX",  # 使用 # 风格标题
            bullets="-",  # 使用 - 作为列表符号
        )
    except Exception as e:
        logger.error(t("🌐 [WebFetch] HTML 转 Markdown 失败: {url}, 错误: {e}", url=url, e=e))
        raise ValueError(t("HTML 转 Markdown 失败: {e}", e=e)) from e

    # 清理多余的空行
    lines = markdown_content.split("\n")
    cleaned_lines = []
    empty_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            empty_count += 1
            if empty_count <= 2:
                cleaned_lines.append("")
        else:
            empty_count = 0
            cleaned_lines.append(line)

    result = "\n".join(cleaned_lines).strip()

    # 截断过长的内容
    if len(result) > max_length:
        result = result[:max_length] + "\n\n...(内容已截断)"
        logger.warning(
            t(
                "🌐 [WebFetch] 内容过长已截断: {url}, 原始长度: {p0}, 截断至: {max_length}",
                url=url,
                p0=len(markdown_content),
                max_length=max_length,
            )
        )

    logger.info(t("🌐 [WebFetch] 抓取完成: {url}, Markdown 长度: {p0} 字符", url=url, p0=len(result)))

    return result


__all__ = ["fetch_webpage_as_markdown"]
