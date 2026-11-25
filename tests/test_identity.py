# -*- coding: utf-8 -*-
"""设备档位与请求头一致性测试（不联网）"""
from src.core import identity
from src.core.config import DEVICE_PROFILES
from src.core.tls import HAS_CURL_CFFI


def test_device_profiles_have_required_fields():
    assert DEVICE_PROFILES, 'DEVICE_PROFILES 不能为空'
    families = set()
    for profile in DEVICE_PROFILES:
        assert profile['family'] not in families
        families.add(profile['family'])
        for key in ('family', 'label', 'impersonate', 'ua'):
            assert profile.get(key), f'档位缺少字段 {key}: {profile}'


def test_profile_lookup_by_family():
    chrome = identity.profile_by_family('chrome-android')
    safari = identity.profile_by_family('safari-ios')
    assert chrome['impersonate'].startswith('chrome')
    assert safari['impersonate'].startswith('safari')
    # 未知 family 回退到第一个档位而不是抛异常
    assert identity.profile_by_family('__not_exist__') == DEVICE_PROFILES[0]


def test_chrome_xhr_headers_consistent():
    chrome = identity.profile_by_family('chrome-android')
    headers = identity.xhr_headers(chrome, 'https://m.douban.com/group/topic/1/')

    assert headers['User-Agent'] == chrome['ua']
    assert headers['Accept'] == 'application/json, text/plain, */*'
    assert headers['X-Requested-With'] == 'XMLHttpRequest'
    assert headers['X-Override-Referer'] == 'https://m.douban.com/group/topic/1/'
    # 导航专属头必须置 None（交给 curl_cffi 删除默认值）
    assert headers['Sec-Fetch-User'] is None
    assert headers['Upgrade-Insecure-Requests'] is None
    # Chrome 档位必须带与 UA 版本一致的 Client Hints
    assert headers['Sec-CH-UA'] == chrome['ch_ua']
    assert headers['Sec-CH-UA-Mobile'] == '?1'
    assert headers['Sec-CH-UA-Platform'] == chrome['ch_platform']
    # 豆瓣没有发 Accept-CH 申请的性能类 hints，发出去反而是指纹
    for banned in ('Device-Memory', 'Downlink', 'ECT', 'RTT',
                   'Viewport-Width', 'Sec-GPC', 'Connection', 'Origin'):
        assert banned not in headers
    if HAS_CURL_CFFI:
        assert 'Accept-Encoding' not in headers


def test_safari_xhr_headers_have_no_client_hints():
    safari = identity.profile_by_family('safari-ios')
    headers = identity.xhr_headers(safari, 'https://m.douban.com/group/topic/1/')
    assert headers['User-Agent'] == safari['ua']
    assert not any(key.startswith('Sec-CH-UA') for key in headers)


def test_nav_headers_are_navigation_semantics():
    chrome = identity.profile_by_family('chrome-android')
    headers = identity.nav_headers(chrome, sec_fetch_site='none')
    assert headers['Sec-Fetch-Dest'] == 'document'
    assert headers['Sec-Fetch-Mode'] == 'navigate'
    assert headers['Sec-Fetch-Site'] == 'none'
    assert headers['Sec-Fetch-User'] == '?1'
    assert headers['Upgrade-Insecure-Requests'] == '1'
    # 第一跳（首页）不带 Referer；后续跳转才带上一页地址
    assert 'Referer' not in headers
    with_referer = identity.nav_headers(
        chrome, 'https://m.douban.com/', sec_fetch_site='same-origin')
    assert with_referer['Referer'] == 'https://m.douban.com/'


def test_image_headers_sec_fetch_site_by_real_domain():
    chrome = identity.profile_by_family('chrome-android')
    # 帖子页在 m.douban.com，图片在 img*.doubanio.com → 跨站
    img_headers = identity.image_headers(
        chrome, url='https://img3.doubanio.com/view/photo/l/public/p1.jpg')
    assert img_headers['Sec-Fetch-Dest'] == 'image'
    assert img_headers['Sec-Fetch-Mode'] == 'no-cors'
    assert img_headers['Sec-Fetch-Site'] == 'cross-site'
    assert img_headers['Sec-Fetch-User'] is None
    assert img_headers['Upgrade-Insecure-Requests'] is None

    # 同一注册域（img1.douban.com vs m.douban.com）→ same-site
    same_site = identity.image_headers(
        chrome, url='https://img1.douban.com/a.jpg')
    assert same_site['Sec-Fetch-Site'] == 'same-site'

    # 完全同 host → same-origin
    same_origin = identity.image_headers(
        chrome, url='https://m.douban.com/a.jpg')
    assert same_origin['Sec-Fetch-Site'] == 'same-origin'


def test_sec_fetch_site_for_invalid_url():
    assert identity.sec_fetch_site_for('') == 'none'
    assert identity.sec_fetch_site_for('not-a-url') == 'none'


def test_create_session_uses_profile_impersonate():
    chrome = identity.profile_by_family('chrome-android')
    session = identity.create_session(chrome)
    try:
        if HAS_CURL_CFFI:
            assert session.impersonate == chrome['impersonate']
        else:
            assert session is not None
    finally:
        session.close()
