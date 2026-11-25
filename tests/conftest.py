# -*- coding: utf-8 -*-
"""pytest 公共配置：保证可以从仓库根目录导入 src 包"""
import atexit
import os
import shutil
import sys
import tempfile

# 保证 `pytest` 直接运行时也能 import src（CI 用 python -m pytest，已包含根目录）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# 单测不弹真实窗口、不写控制台日志
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('DBP_LOG_CONSOLE', '0')

# db_manager 在 import 时读取 DBP_DB_FILE：必须在任何 src 模块导入前指向临时库，
# 否则单测可能读写仓库根目录的真实 douban_data.db。
_TMP_DB_DIR = tempfile.mkdtemp(prefix='dbp_tests_')
os.environ.setdefault('DBP_DB_FILE', os.path.join(_TMP_DB_DIR, 'douban_data.db'))
atexit.register(shutil.rmtree, _TMP_DB_DIR, True)
