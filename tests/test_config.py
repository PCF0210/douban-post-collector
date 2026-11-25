# -*- coding: utf-8 -*-
"""配置常量一致性测试（不联网）

这里只断言“必须成立的关系”，不写死具体数值，避免以后调参时测试过期。
"""
from src.core import config


def test_page_size_matches_step():
    # 分页步长必须等于实际能拿到的条数（首页返回更少时 scraper 会按实际缩小）
    assert config.COMMENTS_PAGE_STEP == config.COMMENTS_PER_PAGE


def test_comment_page_size_within_api_cap():
    # 实测豆瓣评论接口单页上限 100 条
    assert 1 <= config.COMMENTS_PER_PAGE <= 100


def test_concurrency_sane():
    assert config.MAX_CONCURRENT_PAGES >= 1
    assert config.MAX_CONCURRENT_ADAPTIVE_MIN >= 1
    assert config.MAX_CONCURRENT_ADAPTIVE_MIN <= config.MAX_CONCURRENT_PAGES
    assert config.IMAGE_DOWNLOAD_CONCURRENCY >= 1


def test_delays_and_timeouts_positive():
    assert config.PAGE_DELAY_MIN > 0
    assert config.PAGE_DELAY_MAX >= config.PAGE_DELAY_MIN
    assert config.COOKIE_PAGE_DELAY_MAX >= config.COOKIE_PAGE_DELAY_MIN
    assert config.DEFAULT_TIMEOUT > 0
    assert config.WARMUP_TIMEOUT > 0
    assert config.SHORT_LINK_TIMEOUT > 0
    assert config.ADAPTIVE_DELAY_MAX >= config.ADAPTIVE_DELAY_MIN
    assert config.ADAPTIVE_COOLDOWN > 0
    assert config.BLOCK_COOLDOWN_MAX >= config.BLOCK_COOLDOWN_BASE
    assert config.IDENTITY_ROTATE_MIN_INTERVAL > 0
    assert config.MAX_CONSECUTIVE_BLOCKS >= 1


def test_api_base_and_warmup_paths():
    assert config.API_BASE.startswith('https://m.douban.com/')
    assert config.WARMUP_PATHS, '预热路径不能为空'
    # 预热页必须都是实测可用的 m 站页面（失效路径会拖慢启动并在日志留异常）
    for url in config.WARMUP_PATHS:
        assert url.startswith('https://m.douban.com/'), url
