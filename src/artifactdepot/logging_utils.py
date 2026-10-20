# Copyright 2026 apyyc
# SPDX-License-Identifier: Apache-2.0

"""日志安全工具：对 uvicorn access log 的 query string 做脱敏。

背景：token 可能通过 `?token=`（旧式调用/下载直链）出现在请求 URL 中，
uvicorn 默认把完整 path+query 写进 access log，导致凭据落到日志文件。
这里给 `uvicorn.access` logger 装一个 Filter，只把敏感参数的值替换为 `***`，
既保留访问日志，又不泄露 token / 签名。

uvicorn 的 access 日志调用形如：
    logger.info('%s - "%s %s HTTP/%s" %d', client, method, full_path, version, status)
因此 record.args[2] 就是带 query 的 path。
"""
import logging

# 需要脱敏的 query 参数（大小写不敏感）
SENSITIVE_QUERY_KEYS = {
    "token", "tk", "sig", "api_token", "access_token", "password", "secret",
}


def redact_query_string(path: str) -> str:
    """把 URL/路径中敏感 query 参数的值替换为 ***，其余原样保留。"""
    if not path or "?" not in path:
        return path
    base, _, qs = path.partition("?")
    if not qs:
        return path
    out = []
    for pair in qs.split("&"):
        key, sep, _val = pair.partition("=")
        if key.strip().lower() in SENSITIVE_QUERY_KEYS:
            out.append(f"{key}=***")
        else:
            out.append(pair)
    return base + "?" + "&".join(out)


class RedactAccessLogQuery(logging.Filter):
    """过滤 uvicorn.access 记录，脱敏其中的 query string。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
                lst = list(args)
                lst[2] = redact_query_string(lst[2])
                record.args = tuple(lst)
        except Exception:
            # 日志脱敏失败绝不能影响请求处理
            pass
        return True


def install_access_log_redaction() -> None:
    """幂等地给 uvicorn.access logger 安装脱敏 Filter。"""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactAccessLogQuery) for f in logger.filters):
        logger.addFilter(RedactAccessLogQuery())
