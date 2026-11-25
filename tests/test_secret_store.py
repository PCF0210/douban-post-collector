# -*- coding: utf-8 -*-
"""敏感配置存储（DPAPI 封装）与日志脱敏的单元测试。

DPAPI 仅在 Windows 可用；其余平台只验证降级契约（返回空串/不写明文）。
"""

import sys

import pytest

from src.core import secret_store


def test_available_is_bool():
    assert isinstance(secret_store.available(), bool)


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI 仅 Windows 可用')
def test_protect_roundtrip():
    token = secret_store.protect_b64('hello-secret')
    assert token
    assert secret_store.unprotect_b64(token) == 'hello-secret'


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI 仅 Windows 可用')
def test_protect_secret_has_prefix_and_roundtrip():
    value = secret_store.protect_secret('socks5h://user:pass@127.0.0.1:1080')
    assert value.startswith('dpapi:')
    assert secret_store.is_protected(value)
    assert secret_store.unprotect_secret(value) == 'socks5h://user:pass@127.0.0.1:1080'


@pytest.mark.skipif(sys.platform != 'win32', reason='DPAPI 仅 Windows 可用')
def test_protect_secret_is_idempotent():
    once = secret_store.protect_secret('abc')
    twice = secret_store.protect_secret(once)
    assert once == twice
    assert secret_store.unprotect_secret(twice) == 'abc'


def test_protect_secret_empty():
    assert secret_store.protect_secret('') == ''
    assert secret_store.protect_secret(None) == ''


def test_is_protected():
    assert secret_store.is_protected('dpapi:AAAA') is True
    assert secret_store.is_protected('dpapi:') is False
    assert secret_store.is_protected('plain-text') is False
    assert secret_store.is_protected(None) is False


def test_unprotect_plaintext_passthrough():
    # 历史明文原样返回，供调用方迁移
    assert secret_store.unprotect_secret('old-plain') == 'old-plain'
    assert secret_store.unprotect_secret('') == ''
    assert secret_store.unprotect_secret(None) == ''


def test_unprotect_bad_token_returns_empty():
    # 非法密文按「无有效配置」处理，不得抛异常
    assert secret_store.unprotect_b64('not-base64!!') == ''
    assert secret_store.unprotect_secret('dpapi:not-base64!!') == ''


def test_mask_url_hides_credentials():
    assert secret_store.mask_url('http://user:pass@host:8080/x') == 'http://***@host:8080/x'
    masked = secret_store.mask_url('socks5h://u:p@127.0.0.1:1080')
    assert masked == 'socks5h://***@127.0.0.1:1080'
    assert secret_store.mask_url('') == ''
    assert secret_store.mask_url(None) == ''


def test_mask_url_in_exception_text():
    text = 'ProxyError: failed to connect http://alice:secret@proxy.local:8080/path'
    masked = secret_store.mask_url(text)
    assert 'alice' not in masked
    assert 'secret' not in masked
    assert 'proxy.local:8080' in masked


def test_mask_url_without_credentials_unchanged():
    url = 'https://m.douban.com/group/topic/123/'
    assert secret_store.mask_url(url) == url
