# -*- coding: utf-8 -*-
"""自适应限速器测试（不联网）"""
import pytest

from src.core import limiter as limiter_module
from src.core.config import (
    ADAPTIVE_COOLDOWN, MAX_CONCURRENT_PAGES, MAX_CONCURRENT_ADAPTIVE_MIN,
)
from src.core.limiter import AdaptiveRateLimiter


def make_limiter(**kwargs):
    logs = []
    limiter = AdaptiveRateLimiter(log_callback=logs.append, **kwargs)
    return limiter, logs


def test_initial_delay_is_base_range():
    limiter, _ = make_limiter(base_delay=1.0)
    assert limiter.get_delay() == (1.0, 2.0)
    assert limiter.consecutive_failures == 0


def test_failure_increases_delay_and_reduces_concurrency():
    limiter, logs = make_limiter(base_delay=1.0, max_failures=3)
    limiter.report_failure()
    d_min, d_max = limiter.get_delay()
    assert d_min > 1.0
    assert d_max >= d_min
    assert limiter.consecutive_failures == 1
    assert limiter.get_concurrent_pages() == max(
        MAX_CONCURRENT_ADAPTIVE_MIN, MAX_CONCURRENT_PAGES - 1)
    assert any('被限等级' in line for line in logs)


def test_failure_count_capped():
    limiter, _ = make_limiter(base_delay=1.0, max_failures=3)
    for _ in range(10):
        limiter.report_failure()
    assert limiter.consecutive_failures == 3


def test_success_does_not_reset_before_cooldown():
    limiter, _ = make_limiter(base_delay=1.0)
    limiter.report_failure()
    limiter.report_success()
    # 刚被限，冷却期未过，不应清零
    assert limiter.consecutive_failures == 1


def test_success_resets_after_cooldown(monkeypatch):
    limiter, logs = make_limiter(base_delay=1.0)
    limiter.report_failure()

    # 模拟时间流逝到冷却期之后
    now = limiter_module.time.time()
    monkeypatch.setattr(limiter_module.time, 'time',
                        lambda: now + ADAPTIVE_COOLDOWN + 1)
    limiter.report_success()
    assert limiter.consecutive_failures == 0
    assert limiter.get_delay() == (1.0, 2.0)
    assert any('恢复默认延迟' in line for line in logs)


def test_delay_never_exceeds_config_cap():
    limiter, _ = make_limiter(base_delay=10.0, max_failures=8)
    for _ in range(8):
        limiter.report_failure()
    d_min, d_max = limiter.get_delay()
    from src.core.config import ADAPTIVE_DELAY_MAX
    assert d_min <= ADAPTIVE_DELAY_MAX
    assert d_max <= ADAPTIVE_DELAY_MAX
    assert limiter.get_concurrent_pages() >= MAX_CONCURRENT_ADAPTIVE_MIN
