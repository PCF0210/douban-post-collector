# -*- coding: utf-8 -*-
"""scraper 纯逻辑测试：增量锚点、熔断状态机、掩码与可中断等待（不联网）"""
import threading
import time
from types import SimpleNamespace

import pytest

from src.core.config import BLOCK_COOLDOWN_MAX, MAX_CONSECUTIVE_BLOCKS
from src.crawl.scraper import ScraperWorker, _mask_bid


@pytest.fixture()
def worker():
    # ScraperWorker 的构造只准备内存状态，不创建 session、不发网络请求
    w = ScraperWorker('123456789')
    try:
        yield w
    finally:
        w.stop()


def test_mask_bid_does_not_leak_full_value():
    assert _mask_bid('') == ''
    assert _mask_bid('abc') == 'ab***'
    masked = _mask_bid('0123456789abcdef')
    assert masked == '012***ef'
    assert '3456789abcd' not in masked


def test_incremental_anchor_no_base(worker):
    assert worker._incremental_anchor({'total': 10, 'comments': []}, 0) == 0


def test_incremental_anchor_total_smaller_than_base(worker):
    # 接口总数比本地最后楼层还小 → 有评论被删，楼层已位移
    assert worker._incremental_anchor({'total': 3, 'comments': []}, 10) is None


def test_incremental_anchor_empty_comments_returns_none(worker):
    # 空页可能是限流返回的空壳，不能当成「没有新评论」，必须回退完整抓取
    assert worker._incremental_anchor({'total': 100, 'comments': []}, 10) is None


def test_incremental_anchor_matches_by_id(worker):
    monkeypatch = pytest.MonkeyPatch()
    worker._local_comment_identity = lambda floor: ('y', '')
    try:
        data = {'total': 100, 'comments': [
            {'id': 'x', 'content': 'a'},
            {'id': 'y', 'content': 'b'},
        ]}
        assert worker._incremental_anchor(data, 10) == 1
    finally:
        monkeypatch.undo()


def test_incremental_anchor_matches_by_text(worker):
    worker._local_comment_identity = lambda floor: ('', 'b')
    data = {'total': 100, 'comments': [
        {'id': 'x', 'content': 'a'},
        {'id': 'y', 'content': 'b'},
    ]}
    assert worker._incremental_anchor(data, 10) == 1


def test_incremental_anchor_no_match_returns_none(worker):
    worker._local_comment_identity = lambda floor: ('not-exist', 'not-exist')
    data = {'total': 100, 'comments': [{'id': 'x', 'content': 'a'}]}
    assert worker._incremental_anchor(data, 10) is None


def test_sleep_is_interruptible_by_stop(worker):
    worker.stop()
    start = time.monotonic()
    assert worker._sleep(5) is False
    assert time.monotonic() - start < 0.5


def test_sleep_is_interruptible_by_abort(worker):
    worker._abort_reason = '连续被拦截'
    start = time.monotonic()
    assert worker._sleep(5) is False
    assert time.monotonic() - start < 0.5


def test_note_block_trips_circuit_breaker(worker):
    for i in range(MAX_CONSECUTIVE_BLOCKS - 1):
        cooldown = worker._note_block(reason='测试')
        assert cooldown is not None
        assert worker._abort_reason == ''
    # 第 MAX 次触发熔断，返回 None 让调用方中止
    assert worker._note_block(reason='测试') is None
    assert worker._abort_reason
    assert worker._abort_message()


def test_note_block_requests_identity_rotation(worker):
    from src.core.config import IDENTITY_ROTATE_AFTER_BLOCKS
    for _ in range(IDENTITY_ROTATE_AFTER_BLOCKS):
        worker._note_block(reason='测试')
    assert worker._force_rotate is True


def test_note_success_clears_state(worker):
    worker._note_block(reason='测试')
    worker._note_success()
    assert worker._consecutive_blocks == 0
    assert worker._pending_cooldown == 0.0
    assert worker._force_rotate is False
    assert worker._abort_reason == ''


def test_note_block_concurrent_updates_are_not_lost(worker):
    threads = [threading.Thread(target=worker._note_block, args=())
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()
    # 8 个线程各计一次，计数与熔断状态都必须完整保留（无锁实现会丢更新）
    assert worker._consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS
    assert worker._abort_reason


def test_block_cooldown_respects_retry_after(worker):
    resp = SimpleNamespace(headers={'Retry-After': '7'})
    cooldown = worker._block_cooldown(resp)
    assert cooldown == 7.0
    # 超出上限时按上限截断
    big = SimpleNamespace(headers={'Retry-After': '9999'})
    assert worker._block_cooldown(big) == BLOCK_COOLDOWN_MAX


def test_stop_flag_initial_state(worker):
    assert worker._stop is False
    worker.stop()
    assert worker._stop is True
