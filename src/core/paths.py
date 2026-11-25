# -*- coding: utf-8 -*-
"""项目内的固定路径（静态资源、可写数据目录等）

源码运行时资源与数据都在项目根目录下；PyInstaller 打包后（sys.frozen）
静态资源在解包目录（sys._MEIPASS），日志/数据库等可写数据放到
%LOCALAPPDATA%\\douban-post-crawler，避免安装目录只读或升级时被覆盖。
"""
import os
import sys

# 是否运行在 PyInstaller 等打包环境
IS_FROZEN = bool(getattr(sys, 'frozen', False))


def _frozen_asset_dir():
    """打包运行时的静态资源目录（PyInstaller onefile 为 _MEIPASS）"""
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.abspath(sys.executable))


def _local_appdata():
    """Windows 可写数据目录；其他平台退回用户主目录下的隐藏目录"""
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        base = os.path.expanduser('~')
    return os.path.join(base, 'douban-post-crawler')


if IS_FROZEN:
    APP_DIR = _frozen_asset_dir()
    ASSET_DIR = os.path.join(APP_DIR, 'assets')
    # 打包后程序目录通常不可写，日志/数据库/备份统一放到用户目录
    DATA_DIR = _local_appdata()
else:
    # src/core/paths.py → 上溯三级是项目根目录
    APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ASSET_DIR = os.path.join(APP_DIR, 'assets')
    DATA_DIR = APP_DIR


def asset(name):
    '''返回 assets 目录下的资源绝对路径'''
    return os.path.join(ASSET_DIR, name)


def ensure_data_dir():
    '''确保可写数据目录存在（返回其路径）'''
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR
