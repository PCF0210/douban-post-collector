# -*- coding: utf-8 -*-
"""共享网络层：curl_cffi 探测 + TLS 指纹伪装 Adapter + session 工厂"""

import ssl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.exceptions import (
        Timeout as CurlTimeout,
        ConnectionError as CurlConnectionError,
        RequestException as CurlRequestException,
        CurlError,
    )
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    curl_requests = None
    CurlTimeout = ()
    CurlConnectionError = ()
    CurlRequestException = ()
    CurlError = ()


class TLSAdapter(HTTPAdapter):
    """自定义 TLS Adapter，模拟浏览器 TLS 指纹（cipher 顺序 + ALPN + TLS 1.2+）"""

    CIPHERS = (
        'ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20'
        ':ECDHE+AES256:DHE+AES256:ECDHE+AES128:DHE+AES128'
        ':RSA+AESGCM:RSA+AES256:RSA+AES128'
        ':!aNULL:!MD5:!DSS'
    )

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers(self.CIPHERS)
        try:
            # requests/urllib3 只实现 HTTP/1.1；如果这里协商到 h2，
            # 服务端会按 HTTP/2 发送帧，而客户端按 HTTP/1.1 解析，请求直接失败。
            # 因此回退分支只声明 http/1.1（curl_cffi 分支使用自己的 TLS 栈，不受影响）。
            ctx.set_alpn_protocols(['http/1.1'])
        except (NotImplementedError, AttributeError):
            pass
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.options |= 0x4   # SSL_OP_LEGACY_SERVER_CONNECT
        ctx.options |= getattr(ssl, 'OP_NO_TICKET', 0x4000)
        ctx.check_hostname = True
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def make_session(impersonate=None, proxy=None):
    """创建带 TLS 指纹伪装的 session（优先 curl_cffi，回退 TLSAdapter）

    impersonate 默认取 config.DEFAULT_IMPERSONATE，与默认设备档位保持一致；
    身份模块会显式传入 profile 对应的档位，保证 UA 与 TLS 同族同版本。
    """
    if not impersonate:
        from .config import DEFAULT_IMPERSONATE
        impersonate = DEFAULT_IMPERSONATE
    if HAS_CURL_CFFI:
        session = curl_requests.Session(impersonate=impersonate)
    else:
        session = requests.Session()
        session.mount('https://', TLSAdapter())
    if proxy:
        session.proxies = {'http': proxy, 'https': proxy}
    return session
