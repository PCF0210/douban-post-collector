# -*- coding: utf-8 -*-
"""豆瓣小组帖子采集器 GUI 应用 - 入口"""
import os
import sys
import traceback

# src/ui/main_window.py 使用 PEP 701（f-string 内嵌同类引号），需要 Python 3.12+。
# 低版本会在 import 阶段直接 SyntaxError，双击 .pyw 时窗口一闪而过、毫无提示，
# 所以先做版本检查，给用户一个看得懂的提示。
MIN_PY = (3, 12)
if sys.version_info < MIN_PY:
    _msg = ('需要 Python %d.%d 或更高版本，当前为 %d.%d.%d。\n\n'
            '请升级 Python 后重新运行，详细要求见 README.md 的「环境要求」。'
            % (MIN_PY[0], MIN_PY[1], sys.version_info.major,
               sys.version_info.minor, sys.version_info.micro))
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        _app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, 'Python 版本过低', _msg)
    except Exception:
        pass
    print(_msg, file=sys.stderr)
    sys.exit(1)

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from src.core import paths
from src.core.file_log import log_error, log_info

# 单实例锁名称：同一用户会话内只允许一个窗口，避免两个实例同时写库
SINGLE_INSTANCE_KEY = 'DoubanPostCollector.SingleInstance'
# 必须全局持有，否则 QLocalServer 被回收后锁失效
_single_instance_server = None


def _set_windows_app_id():
    """Windows 任务栏按 AppUserModelID 归组，否则会显示成 python 的图标"""
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'DoubanGroupPostCollector')
        except Exception:
            pass


def _install_excepthook():
    """未捕获异常写日志并弹窗提示，避免 .pyw 双击运行时无任何反馈"""
    def _hook(exc_type, exc_value, exc_tb):
        text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            log_error('未捕获异常:\n' + text)
        except Exception:
            pass
        try:
            QMessageBox.critical(
                None, '程序异常',
                '程序遇到未处理的异常，详细信息已写入日志。\n\n'
                f'{exc_type.__name__}: {exc_value}')
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _hook


def _acquire_single_instance():
    """申请单实例锁；已有实例在运行时返回 False"""
    global _single_instance_server
    try:
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
    except Exception as exc:
        # 拿不到 QtNetwork 时不阻止启动（单实例只是防双写，不是核心功能）
        log_error(f'单实例检查不可用（QtNetwork 导入失败）: {exc}')
        return True

    probe = QLocalSocket()
    probe.connectToServer(SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(300):
        probe.disconnectFromServer()
        return False

    # 上次进程异常退出可能残留 socket 文件，先清理再监听
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    if not server.listen(SINGLE_INSTANCE_KEY):
        log_error('单实例服务监听失败，继续启动（无法确认是否已有实例）')
        return True
    _single_instance_server = server
    return True


def _prepare_database():
    """启动时初始化数据库，并尝试备份与完整性检查（失败只记日志，不阻塞启动）"""
    try:
        from src.data import db_manager
    except Exception as exc:
        log_error(f'数据库模块导入失败: {exc}')
        return
    try:
        db_manager.init_db()
    except Exception as exc:
        log_error(f'数据库初始化失败: {exc}')

    backup = getattr(db_manager, 'backup_db', None)
    if callable(backup):
        try:
            try:
                backup(keep=5)
            except TypeError:
                backup()
        except Exception as exc:
            log_error(f'启动备份数据库失败（不影响使用）: {exc}')

    quick_check = getattr(db_manager, 'quick_check', None)
    if callable(quick_check):
        try:
            result = quick_check()
            if result not in (None, 'ok'):
                log_error(f'数据库完整性检查异常: {result}')
        except Exception as exc:
            log_error(f'数据库完整性检查失败（不影响使用）: {exc}')


def main():
    _set_windows_app_id()

    app = QApplication(sys.argv)
    app.setApplicationName('豆瓣小组帖子采集器')
    app.setOrganizationName('DBScraper')

    _install_excepthook()

    if not _acquire_single_instance():
        log_info('检测到已有实例在运行，本次启动退出')
        QMessageBox.information(None, '豆瓣小组帖子采集器',
                                '程序已经在运行，请切换到已打开的窗口。')
        return 0

    icon_path = paths.asset('logo.ico')
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    app.setFont(QFont('Microsoft YaHei UI', 9))

    _prepare_database()

    from src.ui.main_window import DoubanScraperWindow
    window = DoubanScraperWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
