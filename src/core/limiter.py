# -*- coding: utf-8 -*-
"""自适应延迟 / 并发限速器（线程安全）"""

import threading
import time

from .config import (
    ADAPTIVE_DELAY_MIN, ADAPTIVE_DELAY_MAX,
    ADAPTIVE_BACKOFF_FACTOR, ADAPTIVE_COOLDOWN,
    MAX_CONCURRENT_PAGES, MAX_CONCURRENT_ADAPTIVE_MIN,
)


class AdaptiveRateLimiter:
    """自适应延迟与限速控制器（线程安全）

    - 被限时指数增长延迟，连续成功后逐渐恢复
    - 可控制并发数：连续被限时降低并发
    - base_delay: 正常状态下的基础延迟下界（图片下载可用较小值）
    """

    def __init__(self, log_callback=None, base_delay=None, max_failures=8):
        self._failures = 0
        self._last_failure_time = 0.0
        self._log = log_callback
        self._base_delay = ADAPTIVE_DELAY_MIN if base_delay is None else base_delay
        self._max_failures = max_failures
        self._lock = threading.RLock()

    @property
    def consecutive_failures(self):
        with self._lock:
            return self._failures

    def get_delay(self):
        """返回 (最小延迟, 最大延迟) 秒"""
        with self._lock:
            if self._failures == 0:
                return (self._base_delay, self._base_delay * 2)
            factor = ADAPTIVE_BACKOFF_FACTOR ** self._failures
            # 上下界都不得突破 ADAPTIVE_DELAY_MAX（之前上界乘了 1.5，
            # 实际会涨到 22.5s，与「增长到 15s 上限」的语义不符）
            min_d = min(self._base_delay * factor, ADAPTIVE_DELAY_MAX)
            max_d = min(self._base_delay * factor * 2.5, ADAPTIVE_DELAY_MAX)
            if max_d < min_d:
                max_d = min_d
            return (min_d, max_d)

    def get_concurrent_pages(self):
        """根据被限等级返回当前允许的并发页数（每被限一次收一档）"""
        with self._lock:
            allowed = MAX_CONCURRENT_PAGES - self._failures
            return max(MAX_CONCURRENT_ADAPTIVE_MIN, allowed)

    def report_success(self):
        now = time.time()
        with self._lock:
            if self._failures > 0 and (now - self._last_failure_time) > ADAPTIVE_COOLDOWN:
                if self._log:
                    self._log(f'  自适应: {ADAPTIVE_COOLDOWN}s 无被限，恢复默认延迟')
                self._failures = 0

    def report_failure(self):
        now = time.time()
        with self._lock:
            self._last_failure_time = now
            old = self._failures
            self._failures = min(self._failures + 1, self._max_failures)
            if old != self._failures and self._log:
                d_min, d_max = self.get_delay()
                self._log(f'  自适应: 被限等级 {self._failures}/{self._max_failures}, '
                          f'延迟范围 [{d_min:.1f}s ~ {d_max:.1f}s]')
