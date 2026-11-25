# -*- coding: utf-8 -*-
"""本机敏感配置的加密存储（Windows DPAPI）与日志脱敏工具。

设计目标：
- 统一「API Key / 代理账号密码」这类敏感配置的加解密实现，避免各处重复
  写 ctypes；非 Windows 平台给出明确的能力查询接口，由调用方决定降级策略
  （本项目约定：不可用时不写明文、不保存）。
- 密文统一带 ``dpapi:`` 前缀，便于区分「已加密」与「历史版本明文」，迁移时
  不会把明文误当成密文解密。
- 不依赖 PySide6，可在任意线程调用（CryptProtectData 本身是线程安全的）。
"""

import base64
import re
import sys
import threading

# 密文前缀：带前缀的值才走解密，旧版明文可在读取时自动迁移
_PREFIX = 'dpapi:'

# 是否支持 DPAPI 的探测结果（None 表示尚未探测）
_available = None
# DPAPI 相关 ctypes 句柄缓存，避免每次加解密都重新查询
_ctx = None
_ctx_ready = False
_lock = threading.Lock()


def _load_ctx():
    """加载 crypt32/kernel32 并返回调用上下文；失败返回 None。

    返回 (ctypes, DataBlob, crypt32, kernel32) 四元组，供加解密函数复用。
    """

    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD),
                        ('pbData', ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        return ctypes, _DataBlob, crypt32, kernel32
    except Exception:  # noqa: BLE001
        return None


def _get_ctx():
    """返回（并缓存）DPAPI 调用上下文；不可用时返回 None。"""

    global _ctx, _ctx_ready
    if not _ctx_ready:
        with _lock:
            if not _ctx_ready:
                _ctx = _load_ctx()
                _ctx_ready = True
    return _ctx


def available():
    """当前系统是否支持 DPAPI 加密（结果缓存，可放心在循环里调用）。"""

    global _available
    if _available is None:
        # _get_ctx 内部自带锁；这里不能再持有 _lock（Lock 不可重入，会自锁）
        _available = _get_ctx() is not None
    return _available


def protect_b64(text):
    """用 DPAPI 加密文本并返回 base64（不带前缀）；失败或非 Windows 返回空串。

    保留旧版 ai_analyzer._dpapi_protect 的语义，便于配置文件向后兼容。
    """

    if not text:
        return ''
    ctx = _get_ctx()
    if ctx is None:
        return ''
    ctypes, DataBlob, crypt32, kernel32 = ctx
    try:
        raw = text.encode('utf-8')
        buf = ctypes.create_string_buffer(raw)
        blob_in = DataBlob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DataBlob()
        if not crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None, 1,
                ctypes.byref(blob_out)):
            return ''
        try:
            protected = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
        return base64.b64encode(protected).decode('ascii')
    except Exception:  # noqa: BLE001
        return ''


def unprotect_b64(token):
    """解密 DPAPI base64 文本（不带前缀）；失败或非 Windows 返回空串。"""

    if not token:
        return ''
    ctx = _get_ctx()
    if ctx is None:
        return ''
    ctypes, DataBlob, crypt32, kernel32 = ctx
    try:
        protected = base64.b64decode(token)
        buf = ctypes.create_string_buffer(protected)
        blob_in = DataBlob(len(protected), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DataBlob()
        if not crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 1,
                ctypes.byref(blob_out)):
            return ''
        try:
            raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))
        return raw.decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001
        return ''


def is_protected(value):
    """判断是否为带 ``dpapi:`` 前缀且密文非空的加密值。"""

    return (isinstance(value, str) and value.startswith(_PREFIX)
            and len(value) > len(_PREFIX))


def protect_secret(value):
    """加密敏感配置，返回 ``dpapi:<base64>``。

    - 空值返回空串；
    - 已经是密文时原样返回（幂等，避免重复加密）；
    - 系统不支持或加密失败时返回空串，调用方不得退化为写明文。
    """

    if not value:
        return ''
    text = str(value)
    if is_protected(text):
        return text
    token = protect_b64(text)
    return (_PREFIX + token) if token else ''


def unprotect_secret(value):
    """解密敏感配置。

    - 带 ``dpapi:`` 前缀：解密失败返回空串（调用方按「无有效配置」处理）；
    - 不带前缀：视为历史明文原样返回，供调用方迁移；
    - 空值返回空串。
    """

    if not value:
        return ''
    text = str(value)
    if not is_protected(text):
        return text
    return unprotect_b64(text[len(_PREFIX):])


# 匹配 scheme://user:pass@host 形式的 URL 凭证（也适用于异常消息文本）
_URL_CRED_RE = re.compile(r'(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<cred>[^/@\s]+)@')


def mask_url(text):
    """把 URL / 报错文本里的账号密码脱敏成 ``scheme://***@host``。

    不依赖完整 URL 解析：对任意字符串做正则替换，可安全用于异常消息。
    """

    if not text:
        return ''
    try:
        return _URL_CRED_RE.sub(lambda m: m.group('scheme') + '***@', str(text))
    except Exception:  # noqa: BLE001
        return str(text)
