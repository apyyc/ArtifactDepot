"""客户端 IP / 代理转发解析。

审计里的两类 IP：
- ``ip``：服务端直接看到的客户端地址（``request.client.host``），
  有反向代理时是代理地址，可信度取决于部署；
- ``public_ip``：**仅当直连方是 config.trusted_proxies 中的可信代理时**，
  才从 ``X-Forwarded-For`` 解析；否则返回空字符串。

不再采信客户端自报的 ``public_ip`` query/form 参数（可任意伪造），
避免审计记录出现「用户自己填的公网 IP」。
"""
import ipaddress

from fastapi import Request

from artifactdepot.config import get_config


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _ip_in(host: str, entries) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    for entry in entries or []:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def forwarded_chain(request: Request) -> list:
    raw = request.headers.get("x-forwarded-for", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def resolved_public_ip(request: Request) -> str:
    """可信代理链上的真实客户端 IP；无 trusted_proxies 配置或不可信时返回 ""。"""
    trusted = get_config().get("trusted_proxies") or []
    if not trusted:
        return ""
    if not _ip_in(client_ip(request), trusted):
        return ""
    chain = forwarded_chain(request)
    # 从右往左取第一个不在可信代理集合里的地址（最接近真实客户端）
    for ip in reversed(chain):
        if not _ip_in(ip, trusted):
            return ip
    return chain[0] if chain else ""
