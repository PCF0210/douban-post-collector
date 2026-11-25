# -*- coding: utf-8 -*-
"""paths / file_log 基础设施测试（不联网、不写数据库）"""
import logging
import os

from src.core import file_log, paths


def test_source_mode_paths():
    # 源码运行：数据目录就是项目根，静态资源在 assets/
    assert paths.IS_FROZEN is False
    assert paths.DATA_DIR == paths.APP_DIR
    assert os.path.isdir(paths.ASSET_DIR)
    assert os.path.isfile(paths.asset('logo.ico'))


def test_ensure_data_dir_creates_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'DATA_DIR', str(tmp_path / 'data'))
    assert paths.ensure_data_dir() == str(tmp_path / 'data')
    assert (tmp_path / 'data').is_dir()


def test_log_level_env(monkeypatch):
    monkeypatch.setenv('DBP_LOG_LEVEL', 'DEBUG')
    assert file_log._resolve_level() == logging.DEBUG
    monkeypatch.setenv('DBP_LOG_LEVEL', 'warning')
    assert file_log._resolve_level() == logging.WARNING
    monkeypatch.setenv('DBP_LOG_LEVEL', '20')
    assert file_log._resolve_level() == 20
    monkeypatch.setenv('DBP_LOG_LEVEL', '不知道')
    assert file_log._resolve_level() == logging.INFO


def test_console_enabled_env(monkeypatch):
    for value in ('1', 'true', 'YES', 'on'):
        monkeypatch.setenv('DBP_LOG_CONSOLE', value)
        assert file_log._console_enabled() is True
    monkeypatch.setenv('DBP_LOG_CONSOLE', '0')
    assert file_log._console_enabled() is False


def test_log_info_writes_file():
    # 触发惰性初始化（会创建 logs/ 目录并写入一行日志）
    file_log.log_info('pytest: 日志探针')
    log_file = os.path.join(file_log.LOG_DIR, 'dbp.log')
    if file_log.LOG_DIR == os.path.join(paths.DATA_DIR, 'logs'):
        assert os.path.isfile(log_file)
        with open(log_file, encoding='utf-8') as f:
            assert 'pytest: 日志探针' in f.read()
