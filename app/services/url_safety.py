"""URL 安全校验工具

用于防止 SSRF（Server-Side Request Forgery）：
1. 域名白名单：仅允许已知上游 CDN 域名
2. 内网 IP 拒绝：禁止访问 10.x / 172.16-31.x / 192.168.x / 169.254.x / 127.x 等
3. 协议限制：仅允许 http/https

适用场景：
- B 站 audio_url / subtitle_url（来自上游 API 响应，可能被中间人篡改）
- ASR transcription 结果下载 URL（来自 DashScope 服务）

性能说明：
- 同步版 `is_safe_url` 在白名单通过后会调用 `socket.getaddrinfo` 做 DNS 解析，
  该调用是阻塞的，在 async 上下文中应改用 `is_safe_url_async`（内部用 to_thread 包装）。
- DNS 解析结果带 TTL 缓存（默认 60s），避免同域名重复解析；CDN IP 相对稳定，
  缓存不会显著放大 DNS rebinding 风险（rebinding 通常秒级切换）。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Optional
from urllib.parse import urlparse

from loguru import logger


# DNS 解析结果缓存：hostname -> (ips, expire_at)
# CDN 域名解析结果通常稳定，缓存可避免每次 SSRF 校验都阻塞 DNS 查询
_DNS_CACHE: dict[str, tuple[list[str], float]] = {}
_DNS_CACHE_TTL_SEC = 60.0


# B 站 CDN 与字幕域名白名单
BILIBILI_ALLOWED_HOSTS = {
    "upos-sz-mirrorhw.bilivideo.com",
    "upos-sz-mirrorcos.bilivideo.com",
    "upos-sz-mirrorali.bilivideo.com",
    "upos-sz-mirrorhw.bilivideo.cn",
    "upos-sz-mirrorcos.bilivideo.cn",
    "upos-sz-mirrorali.bilivideo.cn",
    "upos-hz-mirrorakm.akamaized.net",
    "cn-szx-12-12.bilivideo.com",
    "i0.hdslb.com",
    "i1.hdslb.com",
    "i2.hdslb.com",
    "s1.hdslb.com",
    "aisubtitle.hdslb.com",
    "aisubtitle-hd.hdslb.com",
}

# DashScope 文件下载域名白名单
DASHSCOPE_ALLOWED_HOSTS = {
    "dashscope.aliyuncs.com",
    "dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
    "dashscope-result-hz.oss-cn-hangzhou.aliyuncs.com",
    "dashscope-result.oss-cn-beijing.aliyuncs.com",
    "dashscope-result.oss-cn-hangzhou.aliyuncs.com",
}


def _is_private_ip(ip_str: str) -> bool:
    """判断 IP 是否为内网/环回/链路本地地址。"""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_hostname(hostname: str) -> list[str]:
    """解析主机名为 IP 列表（防 DNS rebinding 简易版）。

    返回解析到的 IP 字符串列表；解析失败返回空列表。

    带 TTL 缓存：同一 hostname 在 `_DNS_CACHE_TTL_SEC` 内复用结果，
    避免每次 SSRF 校验都阻塞 DNS 查询。
    """
    now = time.monotonic()
    cached = _DNS_CACHE.get(hostname)
    if cached and cached[1] > now:
        return cached[0]
    try:
        # getaddrinfo 返回 (family, type, proto, canonname, sockaddr) 列表
        infos = socket.getaddrinfo(hostname, None)
        ips: list[str] = []
        for info in infos:
            sockaddr = info[4]
            if sockaddr and sockaddr[0]:
                ips.append(sockaddr[0])
        result = list({ip for ip in ips if ip})
    except Exception:
        # 解析失败不缓存（可能是临时网络抖动），下次重试
        return []
    _DNS_CACHE[hostname] = (result, now + _DNS_CACHE_TTL_SEC)
    return result


def _check_hostname_ip(hostname: str) -> tuple[bool, str]:
    """校验 hostname 解析到的 IP 不在内网段。

    Returns:
        (is_safe, reason): 安全时 reason 为空字符串
    """
    # 先校验 hostname 本身是否为 IP 字面量
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_private_ip(str(ip)):
            return False, f"hostname 解析为内网 IP: {hostname}"
        return True, ""
    except ValueError:
        # 不是 IP 字面量，做 DNS 解析
        ips = _resolve_hostname(hostname)
        for ip_str in ips:
            if _is_private_ip(ip_str):
                logger.warning(f"[SSRF] {hostname} 解析到内网 IP {ip_str}，拒绝访问")
                return False, f"hostname 解析到内网 IP: {ip_str}"
        # 解析为空（DNS 失败）时软通过：可用性优先，避免 DNS 抖动误杀正常请求
        return True, ""


def is_safe_url(
    url: str,
    allowed_hosts: Optional[set[str]] = None,
    allow_dashscope: bool = False,
    check_internal_ip: bool = True,
) -> tuple[bool, str]:
    """校验 URL 是否安全可访问。

    Args:
        url: 待校验的 URL
        allowed_hosts: 允许的主机名白名单（精确匹配，不包含子域）
        allow_dashscope: 是否允许 DashScope 域名（用于 ASR 结果下载）
        check_internal_ip: 是否解析主机名并校验 IP 不在内网段

    Returns:
        (is_safe, reason): 是否安全 + 不安全原因（安全时为空字符串）
    """
    if not url or not isinstance(url, str):
        return False, "URL 为空"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL 解析失败: {e}"

    # 1. 协议校验
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"不允许的协议: {scheme}"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "URL 缺少 hostname"

    # 2. 域名白名单校验
    host_allowed = False
    if allowed_hosts and hostname in allowed_hosts:
        host_allowed = True
    if allow_dashscope and hostname in DASHSCOPE_ALLOWED_HOSTS:
        host_allowed = True
    # 允许 *.bilivideo.com / *.bilivideo.cn / *.hdslb.com 子域（CDN 节点多）
    if not host_allowed:
        for suffix in (".bilivideo.com", ".bilivideo.cn", ".hdslb.com"):
            if hostname.endswith(suffix):
                host_allowed = True
                break
        # 允许 *.aliyuncs.com（DashScope OSS）
        if allow_dashscope and hostname.endswith(".aliyuncs.com"):
            host_allowed = True
    if not host_allowed:
        return False, f"hostname 不在白名单: {hostname}"

    # 3. 内网 IP 校验（防 DNS 解析到内网）
    if check_internal_ip:
        ok, reason = _check_hostname_ip(hostname)
        if not ok:
            return False, reason

    return True, ""


async def is_safe_url_async(
    url: str,
    allowed_hosts: Optional[set[str]] = None,
    allow_dashscope: bool = False,
    check_internal_ip: bool = True,
) -> tuple[bool, str]:
    """`is_safe_url` 的异步版本。

    将同步的 DNS 解析（`socket.getaddrinfo`）放到线程池执行，
    避免阻塞事件循环。校验逻辑与同步版完全一致。
    """
    if not url or not isinstance(url, str):
        return False, "URL 为空"

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL 解析失败: {e}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"不允许的协议: {scheme}"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False, "URL 缺少 hostname"

    # 2. 域名白名单校验
    host_allowed = False
    if allowed_hosts and hostname in allowed_hosts:
        host_allowed = True
    if allow_dashscope and hostname in DASHSCOPE_ALLOWED_HOSTS:
        host_allowed = True
    if not host_allowed:
        for suffix in (".bilivideo.com", ".bilivideo.cn", ".hdslb.com"):
            if hostname.endswith(suffix):
                host_allowed = True
                break
        if allow_dashscope and hostname.endswith(".aliyuncs.com"):
            host_allowed = True
    if not host_allowed:
        return False, f"hostname 不在白名单: {hostname}"

    # 3. 内网 IP 校验：放到线程池避免阻塞事件循环
    if check_internal_ip:
        ok, reason = await asyncio.to_thread(_check_hostname_ip, hostname)
        if not ok:
            return False, reason

    return True, ""


async def is_safe_bilibili_url_async(url: str) -> tuple[bool, str]:
    """校验 B 站 audio/subtitle URL 是否安全（异步版）。"""
    return await is_safe_url_async(url, allowed_hosts=BILIBILI_ALLOWED_HOSTS)


async def is_safe_dashscope_url_async(url: str) -> tuple[bool, str]:
    """校验 DashScope 文件下载 URL 是否安全（异步版）。"""
    return await is_safe_url_async(url, allow_dashscope=True)


def is_safe_bilibili_url(url: str) -> tuple[bool, str]:
    """校验 B 站 audio/subtitle URL 是否安全。"""
    return is_safe_url(url, allowed_hosts=BILIBILI_ALLOWED_HOSTS)


def is_safe_dashscope_url(url: str) -> tuple[bool, str]:
    """校验 DashScope 文件下载 URL 是否安全。"""
    return is_safe_url(url, allow_dashscope=True)
