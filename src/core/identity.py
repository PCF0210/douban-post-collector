# -*- coding: utf-8 -*-
"""统一的浏览器身份：设备档位、session 与请求头构造

豆瓣会同时检查 TLS 指纹、User-Agent、Client Hints 与请求头组合。以前各模块
各自随机拼头，会出现「Safari UA + Chrome TLS」「Chrome 131 的 UA + Chrome 126
的 Full-Version」「XHR 带导航专属头」这类自相矛盾组合。现在一个 profile 同时
决定 impersonate / UA / Client Hints，其余模块只引用，不再自己造指纹。
"""

import random
from urllib.parse import urlparse

from .config import ACCEPT_LANGUAGES, DEVICE_PROFILES
from .tls import HAS_CURL_CFFI, make_session

_PROFILE_BY_FAMILY = {p['family']: p for p in DEVICE_PROFILES}

_IMAGE_ACCEPT = 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'


def pick_profile():
    """随机挑一个完整设备档位"""
    return random.choice(DEVICE_PROFILES)


def profile_by_family(family):
    """按 family 取档位（用于测试/强制指定）；未知 family 回退到第一个"""
    return _PROFILE_BY_FAMILY.get(family) or DEVICE_PROFILES[0]


def pick_language():
    return random.choice(ACCEPT_LANGUAGES)


def create_session(profile, proxy=None):
    """按档位创建 session，TLS/HTTP2 指纹由 impersonate 决定

    旧版 curl_cffi 可能不认识新档位（如 chrome131_android）：依次退回到
    chrome120 / chrome110。这种情况下 UA 与 TLS 会再次不一致，README 已
    注明推荐的 curl_cffi 版本；没有 curl_cffi 时走 TLSAdapter 回退分支。
    """
    if not HAS_CURL_CFFI:
        return make_session(proxy=proxy)
    targets = [profile['impersonate'], 'chrome120', 'chrome110']
    last_error = None
    for target in targets:
        try:
            return make_session(impersonate=target, proxy=proxy)
        except Exception as e:  # 旧版库不认识目标档位时抛异常
            last_error = e
    raise RuntimeError(
        f'当前 curl_cffi 不支持 {profile["impersonate"]} 档位，请升级 curl_cffi'
    ) from last_error


def _client_hints(profile):
    """只有 Chromium 系才发 Client Hints；Safari 档位返回空"""
    if not profile.get('ch_ua'):
        return {}
    return {
        'Sec-CH-UA': profile['ch_ua'],
        'Sec-CH-UA-Mobile': profile.get('ch_mobile') or '?0',
        'Sec-CH-UA-Platform': profile.get('ch_platform') or '"Android"',
    }


def _fallback_accept_encoding(headers):
    """curl_cffi 会按 impersonate 使用档位自己的 Accept-Encoding（含 br/zstd），
    只有退化成 requests 时才需要手写浏览器常见值。"""
    if not HAS_CURL_CFFI:
        headers['Accept-Encoding'] = 'gzip, deflate'
    return headers


def xhr_headers(profile, referer, lang=None):
    """站内 XHR（rexxar API）请求头。

    故意不设置：
    - Connection：HTTP/2 下浏览器不发这个头；
    - Origin：同源 GET 不带；
    - Device-Memory / Downlink / ECT / RTT / Viewport-Width：豆瓣根本没发
      Accept-CH 请求这些 client hints，主动发送反而是可识别特征；
    - Sec-GPC：默认不开启。

    Sec-Fetch-User / Upgrade-Insecure-Requests 是导航请求专属头，这里显式置
    None 让 curl_cffi 删掉 impersonate 默认带上的值。
    """
    headers = {
        'User-Agent': profile['ua'],
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': lang or pick_language(),
        'Referer': referer,
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': None,
        'Upgrade-Insecure-Requests': None,
        'X-Requested-With': 'XMLHttpRequest',
        'X-Override-Referer': referer,
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
    }
    headers.update(_client_hints(profile))
    return _fallback_accept_encoding(headers)


def nav_headers(profile, referer=None, sec_fetch_site='none', lang=None):
    """顶层导航（预热 / 帖子页）请求头"""
    headers = {
        'User-Agent': profile['ua'],
        'Accept': profile.get('nav_accept') or (
            'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'),
        'Accept-Language': lang or pick_language(),
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': sec_fetch_site,
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
    }
    if referer:
        headers['Referer'] = referer
    headers.update(_client_hints(profile))
    return _fallback_accept_encoding(headers)


def image_headers(profile, referer='https://m.douban.com/', lang=None, url=''):
    """图片请求头。图片在 img*.doubanio.com / img*.douban.com，Sec-Fetch-Site
    必须按真实域名关系计算，写死 same-origin 是明显错误。"""
    headers = {
        'User-Agent': profile['ua'],
        'Accept': _IMAGE_ACCEPT,
        'Accept-Language': lang or pick_language(),
        'Referer': referer,
        'Sec-Fetch-Dest': 'image',
        'Sec-Fetch-Mode': 'no-cors',
        'Sec-Fetch-Site': sec_fetch_site_for(url) if url else 'cross-site',
        'Sec-Fetch-User': None,
        'Upgrade-Insecure-Requests': None,
    }
    headers.update(_client_hints(profile))
    return _fallback_accept_encoding(headers)


def sec_fetch_site_for(url, page_url='https://m.douban.com/'):
    """按 URL 与帖子页的注册域关系返回 Sec-Fetch-Site 值"""
    try:
        host = (urlparse(url).hostname or '').lower()
        page_host = (urlparse(page_url).hostname or '').lower()
    except ValueError:
        return 'none'
    if not host:
        return 'none'
    if host == page_host:
        return 'same-origin'
    return 'same-site' if _registrable(host) == _registrable(page_host) else 'cross-site'


def _registrable(host):
    parts = (host or '').split('.')
    if len(parts) <= 2:
        return host
    # 豆瓣只用 .com / .com.cn；这里覆盖常见的二级后缀即可
    if parts[-2] in ('com', 'net', 'org', 'gov', 'edu') and len(parts) >= 3:
        return '.'.join(parts[-3:]) if parts[-1] == 'cn' else '.'.join(parts[-2:])
    return '.'.join(parts[-2:])
