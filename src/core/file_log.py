# -*- coding: utf-8 -*-
"""滚动文件日志：写入数据目录 logs/dbp.log（便于排查反爬被限等问题）

- 源码运行：项目根/logs/dbp.log
- 打包运行：%LOCALAPPDATA%\\douban-post-crawler\\logs\\dbp.log
- 日志级别：环境变量 DBP_LOG_LEVEL（默认 INFO，可设 DEBUG/WARNING 等，也接受数字）
- 控制台输出：环境变量 DBP_LOG_CONSOLE=1 时同时打印到 stderr（调试用）
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from .paths import DATA_DIR

# 与数据库/备份同处一个可写数据目录
LOG_DIR = os.path.join(DATA_DIR, 'logs')

_LOGGER_NAME = 'dbp'
_LEVEL_NAMES = {
    'CRITICAL': logging.CRITICAL,
    'ERROR': logging.ERROR,
    'WARNING': logging.WARNING,
    'WARN': logging.WARNING,
    'INFO': logging.INFO,
    'DEBUG': logging.DEBUG,
    'NOTSET': logging.NOTSET,
}
_logger = None


def _resolve_level():
    """从环境变量解析日志级别，非法值退回 INFO"""
    raw = (os.environ.get('DBP_LOG_LEVEL') or 'INFO').strip().upper()
    if raw.isdigit():
        return int(raw)
    return _LEVEL_NAMES.get(raw, logging.INFO)


def _console_enabled():
    return (os.environ.get('DBP_LOG_CONSOLE') or '').strip().lower() in ('1', 'true', 'yes', 'on')


def get_logger():
    """获取（惰性初始化的）滚动文件 logger"""
    global _logger
    if _logger is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(_resolve_level())
        # 幂等：同名 logger 若已挂过 handler（例如模块被重复导入），先清掉，
        # 否则同一条日志会被写进文件多次
        if not logger.handlers:
            handler = RotatingFileHandler(
                os.path.join(LOG_DIR, 'dbp.log'),
                maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8',
            )
            handler.setFormatter(logging.Formatter(
                '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
            ))
            logger.addHandler(handler)
            if _console_enabled():
                console = logging.StreamHandler()
                console.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
                logger.addHandler(console)
        else:
            # 已初始化过：环境变量可能被测试/入口调整，允许重新同步级别
            logger.setLevel(_resolve_level())
        logger.propagate = False
        _logger = logger
    return _logger


def log_info(msg):
    get_logger().info(msg)


def log_error(msg):
    get_logger().error(msg)
