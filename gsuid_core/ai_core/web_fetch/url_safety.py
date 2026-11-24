"""公开 HTTP URL 安全校验与防 DNS 重绑定解析器（SSRF 防线，供 web_fetch 及插件共用）。"""

from __future__ import annotations

import socket
import asyncio
import ipaddress
from urllib.parse import urlparse

import aiohttp.resolver
from aiohttp.abc import ResolveResult, AbstractResolver

from gsuid_core.i18n import t

_FORBIDDEN_EXACT_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "host.docker.internal",
        "gateway.docker.internal",
        "metadata.google.internal",
        "metadata",
        "0.0.0.0",
    }
)
_FORBIDDEN_HOST_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
)


def _is_non_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _is_non_public_ip(ip.ipv4_mapped)
    return bool(
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def validate_public_url_syntax(url: str) -> str:
    """静态校验 URL 必须为公网 http/https，拦截环回/内网 IP 与容器宿主域名。"""
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        raise ValueError(t("无效的 URL: {url}，必须以 http:// 或 https:// 开头", url=raw))
    parsed = urlparse(raw)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError(t("无效的 URL（缺少主机名）: {url}", url=raw))
    if host in _FORBIDDEN_EXACT_HOSTS or host.endswith(_FORBIDDEN_HOST_SUFFIXES):
        raise ValueError(t("禁止访问本机或内网地址: {host}", host=host))
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return raw
    if _is_non_public_ip(ip):
        raise ValueError(t("禁止访问环回或私有网段 IP: {host}", host=host))
    return raw


async def validate_public_url_dns(url: str) -> str:
    """前置 DNS 校验公网目标（直连场景配合 PublicOnlyResolver 消除 TOCTOU；代理场景作前置防线）。"""
    cleaned = validate_public_url_syntax(url)
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return cleaned
    except ValueError:
        pass
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return cleaned
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_text = str(sockaddr[0]).split("%", 1)[0]
        try:
            resolved = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if _is_non_public_ip(resolved):
            raise ValueError(t("禁止访问解析到内网/环回地址的域名: {host} -> {ip}", host=host, ip=ip_text))
    return cleaned


class PublicOnlyResolver(AbstractResolver):
    """在 aiohttp 建连阶段校验每次 DNS 解析结果，消除校验与实际连出之间的重绑定 TOCTOU。"""

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self._inner: AbstractResolver = inner if inner is not None else aiohttp.resolver.DefaultResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        cleaned = host.strip().lower().rstrip(".")
        if cleaned in _FORBIDDEN_EXACT_HOSTS or cleaned.endswith(_FORBIDDEN_HOST_SUFFIXES):
            raise ValueError(t("禁止访问本机或内网地址: {host}", host=cleaned))
        results = await self._inner.resolve(host, port, family)
        for entry in results:
            ip_text = str(entry["host"]).split("%", 1)[0]
            try:
                resolved = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if _is_non_public_ip(resolved):
                raise ValueError(t("禁止访问解析到内网/环回地址的域名: {host} -> {ip}", host=cleaned, ip=ip_text))
        return results

    async def close(self) -> None:
        await self._inner.close()


__all__ = ["PublicOnlyResolver", "validate_public_url_syntax", "validate_public_url_dns"]
