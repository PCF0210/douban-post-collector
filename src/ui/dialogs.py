# -*- coding: utf-8 -*-
import json
import os
import threading
import time
import traceback

from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
                               QHeaderView)

from ..core.config import OCR_LANGUAGE
from ..core.paths import asset
from ..core.secret_store import (available as secret_available, is_protected,
                                 mask_url, protect_secret, unprotect_secret)
from ..data.db_manager import (delete_topic as db_delete_topic, get_comment_dicts,
                               get_images, get_stats, get_topic, get_topics,
                               get_topics_count)
from ..ocr.ocr_extractor import OCRExtractor
from ..core.file_log import LOG_DIR, log_error, log_info
from ..core.tls import make_session
from .html_render import _reply_ref, _strip_html
from .widgets import _ProxyTestResult, _cn_buttons
from .ui_theme import PALETTE as C


class _QueuedResultRelay(QObject):
    '''后台任务结果信号中继（后台线程 → UI 线程）'''

    done = Signal(str, object)


class _QueuedResultHandler(QObject):
    '''后台任务结果的槽对象

    必须用绑定到窗口/对话框的 QObject 方法做接收者，并显式指定
    Qt.QueuedConnection；否则槽会在发信号的后台线程里执行，里面碰控件
    就会跨线程崩溃（参见 widgets.py 中 _OcrSignalSink 的注释）。
    '''

    def __init__(self, parent, callback):
        super().__init__(parent)
        self._callback = callback

    @Slot(str, object)
    def handle(self, tag, payload):
        try:
            self._callback(tag, payload)
        except RuntimeError:
            return  # 宿主窗口已销毁


class _ProxyTestHandler(QObject):
    '''代理测试结果槽：在 UI 线程恢复按钮并弹出测试结果'''

    def __init__(self, dlg, test_btn, use_proxy):
        super().__init__(dlg)
        self._dlg = dlg
        self._btn = test_btn
        self._use_proxy = bool(use_proxy)

    @Slot(bool, str)
    def on_done(self, ok, msg):
        try:
            self._btn.setEnabled(True)
            self._btn.setText('测试连接（访问 m.douban.com）')
            suffix = '（经代理）' if self._use_proxy else '（直连，未启用代理）'
            if ok:
                QMessageBox.information(self._dlg, '测试结果', f'{msg}\n{suffix}')
            else:
                QMessageBox.warning(self._dlg, '测试结果', f'{msg}\n{suffix}')
        except RuntimeError:
            return  # 对话框已销毁


class DialogMixin:
    """DialogMixin"""

    def _show_ocr_status(self):
        '''显示 OCR 引擎状态对话框（后台探测，避免 UI 线程卡住最长 ~10 秒）'''

        self.statusBar.showMessage('正在后台检测 OCR 引擎…', 3000)
        self._start_ocr_async('ocr_status')

    def _set_ocr_busy(self, busy):
        '''OCR 探测期间禁用识别按钮并显示等待文案'''

        btn = getattr(self, 'btn_ocr', None)
        try:
            if btn is not None:
                if busy:
                    btn.setEnabled(False)
                    btn.setText('OCR 检测中…')
                else:
                    self._refresh_ocr_button()
        except RuntimeError:
            return  # 窗口已销毁

    def _start_ocr_async(self, purpose):
        '''后台线程探测 OCR 引擎/语言包，结果经信号回主线程后再弹窗。

        purpose: 'ocr_status'（引擎状态）或 'ocr_language'（识别语言设置）。
        tesseract 引擎探测最长 ~10 秒、语言包列表最长 ~5 秒，都不能直接放在 UI 线程。
        '''

        if getattr(self, '_ocr_async_running', False):
            self.statusBar.showMessage('OCR 引擎检测正在进行，请稍候…', 3000)
            return
        self._ocr_async_running = True
        self._set_ocr_busy(True)
        relay = _QueuedResultRelay(self)
        handler = _QueuedResultHandler(self, self._on_ocr_async_result)
        relay.done.connect(handler.handle, Qt.QueuedConnection)
        # 挂到窗口上并保持强引用：线程结束前 relay/handler 不能被回收
        self._ocr_async_relay = relay
        self._ocr_async_handler = handler
        reset_first = purpose == 'ocr_status'

        def _work(relay=relay, handler=handler):
            payload = {
                'available': False, 'status': '', 'engine': '', 'language': '',
                'supports_selection': False, 'langs': [], 'error': '',
            }
            try:
                if reset_first:
                    # 手动重新检测：万一是上次 tesseract 启动超时导致的误判
                    OCRExtractor.reset_instance()
                extractor = OCRExtractor.get_instance()
                payload['available'] = bool(extractor.available)
                payload['status'] = extractor.status_message()
                engine = (extractor.engine_name or '')
                payload['engine'] = engine
                payload['language'] = getattr(extractor, 'language', '') or ''
                supports = getattr(extractor, 'supports_language_selection', True)
                if (not hasattr(extractor, 'supports_language_selection')
                        and engine == 'rapidocr'):
                    # 兼容尚未提供该属性的旧版 OCRExtractor：RapidOCR 用内置模型
                    supports = False
                payload['supports_selection'] = bool(supports)
                if payload['available'] and payload['supports_selection']:
                    payload['langs'] = list(extractor.available_languages() or [])
            except Exception:
                payload['error'] = traceback.format_exc()
                log_error('OCR 引擎探测异常:\n' + payload['error'])
            try:
                relay.done.emit(purpose, payload)
            except RuntimeError:
                return  # 主窗口已销毁

        threading.Thread(target=_work, daemon=True).start()

    def _on_ocr_async_result(self, purpose, payload):
        '''OCR 探测结果回到 UI 线程后分发处理'''

        self._ocr_async_running = False
        self._set_ocr_busy(False)
        try:
            if purpose == 'ocr_language':
                self._apply_ocr_language_probe(payload)
            else:
                self._apply_ocr_status_probe(payload)
        except RuntimeError:
            return  # 窗口已销毁

    def _apply_ocr_status_probe(self, payload):
        '''展示 OCR 引擎状态（payload 由后台线程探测得到）'''

        if payload.get('error'):
            log_error('OCR 引擎探测失败:\n' + payload.get('error', ''))
            QMessageBox.warning(self, 'OCR 引擎不可用', 'OCR 引擎检测出错，详情见 logs/dbp.log')
            return
        status_html = (payload.get('status') or '').replace('\n', '<br>')
        if payload.get('available'):
            extra = ''
            if payload.get('engine') != 'rapidocr':
                langs = payload.get('langs') or []
                lang_text = ', '.join(langs) if langs else '无'
                language = payload.get('language') or self._read_ocr_language()
                extra = f'<br><br><b>已安装语言包：</b>{lang_text}<br><br>当前识别语言：{language}'
            QMessageBox.information(self, 'OCR 引擎状态',
                                    f'<b>OCR 引擎可用</b><br><br>{status_html}{extra}')
            return
        QMessageBox.warning(
            self, 'OCR 引擎不可用',
            f'<b>OCR 引擎不可用</b><br><br>{status_html}<br><br>'
            '<b>安装步骤（任选其一）：</b><br>'
            '1. 推荐：<code>pip install rapidocr-onnxruntime</code><br>'
            '&nbsp;&nbsp;自带中英文模型，速度更快、准确率更高<br>'
            '2. 或安装 Tesseract OCR：<br>'
            '&nbsp;&nbsp;Windows: 从 '
            '<a href="https://github.com/UB-Mannheim/tesseract/wiki">UB-Mannheim/tesseract</a> '
            '下载安装<br>'
            '&nbsp;&nbsp;macOS: brew install tesseract tesseract-lang<br>'
            '&nbsp;&nbsp;Linux: sudo apt install tesseract-ocr tesseract-ocr-chi-sim<br>'
            '&nbsp;&nbsp;再执行：<code>pip install pytesseract Pillow</code><br><br>'
            '安装后重启程序。')

    def _settings(self):
        # QSettings 每次构造都会重新读注册表/配置文件，缓存一份复用
        s = getattr(self, '_qsettings', None)
        if s is None:
            s = QSettings('DBScraper', 'DBScraper')
            self._qsettings = s
        return s

    def _read_proxy(self):
        '''读取代理设置，返回 (enabled, url)。

        新版把代理地址（可能含账号密码）用 DPAPI 加密保存；这里对密文自动解密，
        并对旧版本遗留的明文做一次自动迁移，解密失败时按「未配置代理」处理。
        '''

        s = self._settings()
        enabled = s.value('proxy/enabled', False, type=bool)
        raw = str(s.value('proxy/url', '') or '')
        if not raw:
            return enabled, ''
        if is_protected(raw):
            url = unprotect_secret(raw)
            if not url:
                log_info('已保存的代理地址解密失败（可能更换了 Windows 账户），本次按未配置代理解析')
                return enabled, ''
            return enabled, url
        # 旧版本明文：能加密就自动迁移，避免账号密码继续明文留在配置里
        if secret_available():
            token = protect_secret(raw)
            if token:
                s.setValue('proxy/url', token)
                log_info('代理地址已从明文迁移为 DPAPI 密文保存')
        return enabled, raw

    def _read_ocr_language(self):
        '''读取 OCR 识别语言设置（未设置时回落到 config.OCR_LANGUAGE）'''

        lang = self._settings().value('ocr/language', '')
        return lang or OCR_LANGUAGE

    def _show_proxy_settings(self):
        '''代理设置对话框（设置对所有爬取/下载/OCR 生效）'''

        enabled, url = self._read_proxy()
        dlg = QDialog(self)
        dlg.setWindowTitle('代理设置')
        dlg.setMinimumWidth(420)
        form = QFormLayout(dlg)
        chk = QCheckBox('启用代理', dlg)
        chk.setChecked(enabled)
        edit = QLineEdit(url, dlg)
        edit.setPlaceholderText('http://127.0.0.1:7890')
        form.addRow(chk)
        form.addRow('代理地址：', edit)
        hint = QLabel('代理地址格式：http://ip:port 或 socks5://ip:port\n启用后爬取帖子、下载图片、OCR 都会走代理。')
        hint.setStyleSheet('color: #9CA3AF; font-size: 12px;')
        form.addRow(hint)
        test_btn = QPushButton('测试连接（访问 m.douban.com）', dlg)
        test_btn.setCursor(Qt.PointingHandCursor)
        form.addRow(test_btn)
        def _run_proxy_test():
            use_proxy = chk.isChecked()
            proxy_url = edit.text().strip()
            if use_proxy and not proxy_url:
                QMessageBox.warning(dlg, '提示', '已勾选启用代理，但代理地址为空。')
                return
            test_btn.setEnabled(False)
            test_btn.setText('测试中...')
            relay = _ProxyTestResult(self)
            # 槽必须是绑定到 dlg 的 QObject 方法并使用 QueuedConnection：
            # 否则信号从后台线程 emit 时，槽也会在工作线程里碰控件（会崩溃）
            handler = _ProxyTestHandler(dlg, test_btn, use_proxy)
            relay.done.connect(handler.on_done, Qt.QueuedConnection)
            # 强引用：线程结束前 handler 不能被回收；挂在 dlg 上便于随对话框销毁
            dlg._proxy_test_handler = handler

            def _work(relay=relay, handler=handler):
                t0 = time.time()
                try:
                    sess = make_session(proxy=proxy_url if use_proxy else None)
                    r = sess.get('https://m.douban.com/', timeout=10)
                    ms = int((time.time() - t0) * 1000)
                    relay.done.emit(True, f'连接成功：HTTP {r.status_code}，耗时 {ms}ms')
                    return
                except Exception as e:
                    # 异常消息里可能带完整代理地址（含账号密码），必须脱敏后再显示
                    relay.done.emit(False, f'连接失败：{mask_url(e)}')

            threading.Thread(target=_work, daemon=True).start()

        test_btn.clicked.connect(_run_proxy_test)
        buttons = _cn_buttons(QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dlg))
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec() == QDialog.Accepted:
            s = self._settings()
            raw_url = edit.text().strip()
            s.setValue('proxy/enabled', chk.isChecked())
            token = protect_secret(raw_url)
            if raw_url and token:
                # 代理地址可能含账号密码：能加密就绝不写明文
                s.setValue('proxy/url', token)
            elif raw_url and secret_available():
                log_error('代理地址加密失败，已取消本次保存（不写入明文）')
                QMessageBox.warning(self, '保存失败', '代理地址加密失败，未保存本次修改。')
                return
            elif raw_url:
                # 非 Windows / DPAPI 不可用时的降级：保留明文并记录日志提示
                s.setValue('proxy/url', raw_url)
                log_info('当前系统不支持 DPAPI 安全存储，代理地址将以明文保存在配置中')
            else:
                s.setValue('proxy/url', '')
            state = '已启用' if chk.isChecked() else '已禁用'
            self.statusBar.showMessage(f'代理设置已保存（{state}）', 3000)

    def _show_ocr_language_settings(self):
        '''OCR 识别语言配置对话框（引擎探测与语言包枚举放到后台线程）'''

        self.statusBar.showMessage('正在后台检测 OCR 识别语言…', 3000)
        self._start_ocr_async('ocr_language')

    def _apply_ocr_language_probe(self, payload):
        '''按后台探测结果打开语言选择框，或给出对应的提示'''

        if payload.get('error'):
            log_error('OCR 语言探测失败:\n' + payload.get('error', ''))
            QMessageBox.warning(self, 'OCR 不可用', 'OCR 引擎检测出错，详情见 logs/dbp.log')
            return
        if not payload.get('available'):
            QMessageBox.warning(self, 'OCR 不可用',
                                f'OCR 引擎不可用，无法配置语言：\n\n{payload.get("status") or ""}')
            return
        if not payload.get('supports_selection'):
            # RapidOCR 内置中英文模型，没有 Tesseract 语言包概念，不能再误报「未找到语言包」
            QMessageBox.information(self, 'OCR 识别语言',
                                    '当前使用 RapidOCR，内置中英文模型，无需安装或选择语言包。')
            return
        langs = payload.get('langs') or []
        if not langs:
            QMessageBox.warning(self, '未找到语言包', '未检测到任何 Tesseract 语言包，请安装后重试。')
            return
        current = set((self._read_ocr_language() or '').split('+'))
        current.discard('')
        if not current:
            current = {'chi_sim', 'eng'}
        dlg = QDialog(self)
        dlg.setWindowTitle('OCR 识别语言')
        dlg.setMinimumSize(340, 400)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel('选择要启用的识别语言（可多选，用 + 组合）：'))
        listw = QListWidget(dlg)
        for lang in sorted(langs):
            item = QListWidgetItem(lang)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if lang in current else Qt.Unchecked)
            listw.addItem(item)
        lay.addWidget(listw)
        buttons = _cn_buttons(QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dlg))
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() == QDialog.Accepted:
            selected = [listw.item(i).text() for i in range(listw.count())
                 if listw.item(i).checkState() == Qt.Checked]
            if not selected:
                QMessageBox.information(self, '提示', '请至少勾选一种语言。')
                return
            language = '+'.join(selected)
            self._settings().setValue('ocr/language', language)
            # 探测线程刚创建过实例，这里用不阻塞的 peek；实例不存在时只保存设置
            extractor = OCRExtractor.peek_instance()
            if extractor is not None:
                extractor.language = language
            else:
                log_info(f'OCR 引擎实例尚未创建，识别语言 {language} 已保存，实例创建后生效')
            self.statusBar.showMessage(f'OCR 语言已设为: {language}', 3000)

    def _show_db_browser(self):
        '''打开数据库浏览对话框

        统计/帖子列表/加载/导出/删除的 SQL 全部放到后台线程执行，UI 线程只负责
        回填控件；操作进行中禁用相关按钮，避免重复提交或旧结果覆盖新结果。
        '''

        dlg = QDialog(self)
        dlg.setWindowTitle('数据库浏览')
        dlg.resize(900, 600)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        stats_bar = QLabel('统计加载中…')
        stats_bar.setStyleSheet(f'padding: 8px; color: {C['text2']}; font-size: 12px; border: none;')
        layout.addWidget(stats_bar)
        search_row = QHBoxLayout()
        search_input = QLineEdit()
        search_input.setPlaceholderText('搜索标题 / 作者...')
        search_btn = QPushButton('搜索')
        search_btn.setCursor(Qt.PointingHandCursor)
        search_input.returnPressed.connect((lambda: load_table(keyword=search_input.text().strip(), reset_page=True)))
        search_btn.clicked.connect((lambda: load_table(keyword=search_input.text().strip(), reset_page=True)))
        refresh_btn = QPushButton('刷新')
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.clicked.connect((lambda: load_table(keyword=search_input.text().strip(), reset_page=True)))
        search_row.addWidget(search_input, stretch=1)
        search_row.addWidget(search_btn)
        search_row.addWidget(refresh_btn)
        search_row.addStretch()
        layout.addLayout(search_row)
        page_row = QHBoxLayout()
        btn_prev_page = QPushButton('上一页')
        btn_prev_page.setCursor(Qt.PointingHandCursor)
        btn_next_page = QPushButton('下一页')
        btn_next_page.setCursor(Qt.PointingHandCursor)
        page_label = QLabel('')
        page_label.setStyleSheet(f'color: {C['text2']}; font-size: 12px; border: none;')
        page_row.addWidget(btn_prev_page)
        page_row.addWidget(btn_next_page)
        page_row.addStretch()
        page_row.addWidget(page_label)
        layout.addLayout(page_row)
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(['ID', '标题', '作者', '评论数', '爬取时间'])
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setDefaultSectionSize(40)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(table)
        btn_row = QHBoxLayout()
        btn_load = QPushButton('加载到主窗口')
        btn_load.setCursor(Qt.PointingHandCursor)
        btn_load.setEnabled(False)
        btn_export_db_json = QPushButton('导出选中 JSON')
        btn_export_db_json.setCursor(Qt.PointingHandCursor)
        btn_export_db_json.setEnabled(False)
        btn_delete = QPushButton('删除记录')
        btn_delete.setCursor(Qt.PointingHandCursor)
        btn_delete.setEnabled(False)
        btn_delete.setObjectName('btnDanger')
        btn_row.addWidget(btn_load)
        btn_row.addWidget(btn_export_db_json)
        btn_row.addWidget(btn_delete)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        PAGE_SIZE = 100
        # busy: 后台任务是否在跑；gen: 查询代次（防旧结果覆盖新结果）；closed: 对话框已关闭
        state = {'page': 0, 'keyword': '', 'pages': 1, 'busy': False, 'gen': 0, 'closed': False}

        def _decode_raw(row):
            '''raw_json 损坏时退回整行，避免 json.loads 异常打断槽函数。'''

            if not isinstance(row, dict):
                return row
            raw = row.get('raw_json')
            if not raw:
                return row
            try:
                return json.loads(raw)
            except Exception:
                return row

        def _on_task(tag, payload):
            '''后台任务结果分发（已由 _QueuedResultHandler 保证在 UI 线程执行）'''

            if state.get('closed'):
                return
            if tag == 'stats':
                _apply_stats(payload)
            elif tag == 'topics':
                _apply_topics(payload)
            elif tag == 'load':
                _apply_load(payload)
            elif tag == 'export':
                _apply_export(payload)
            elif tag == 'delete':
                _apply_delete(payload)

        relay = _QueuedResultRelay(dlg)
        handler = _QueuedResultHandler(dlg, _on_task)
        relay.done.connect(handler.handle, Qt.QueuedConnection)
        # 挂在对话框上保持强引用：对话框关闭前 relay/handler 不能被回收
        dlg._db_relay = relay
        dlg._db_handler = handler

        def _run_async(tag, fn):
            '''在后台线程执行 fn，结果经信号回 UI 线程；fn 的返回值放进 payload.data。'''

            def _work(relay=relay, handler=handler):
                try:
                    payload = {'ok': True, 'data': fn()}
                except Exception as e:
                    payload = {'ok': False, 'error': str(e)}
                if state.get('closed'):
                    return
                try:
                    relay.done.emit(tag, payload)
                except RuntimeError:
                    return  # 对话框已销毁

            threading.Thread(target=_work, daemon=True).start()

        def _refresh_selection():
            '''按当前选中行与忙碌状态刷新操作按钮（任务进行中不可点）'''

            has_sel = (not state['busy']) and table.currentRow() >= 0
            btn_load.setEnabled(has_sel)
            btn_export_db_json.setEnabled(has_sel)
            btn_delete.setEnabled(has_sel)

        def _set_busy(busy):
            '''统一忙碌态切换：禁用查询/翻页/操作按钮，结束后恢复选中态按钮。'''

            state['busy'] = bool(busy)
            for widget in (search_input, search_btn, refresh_btn, btn_prev_page, btn_next_page):
                widget.setEnabled(not busy)
            if not busy:
                btn_prev_page.setEnabled(state['page'] > 0)
                btn_next_page.setEnabled(state['page'] < state['pages'] - 1)
            _refresh_selection()

        def _selected_tid():
            '''取当前选中行对应的帖子 ID；无效时返回 0。'''

            row = table.currentRow()
            if row < 0:
                return 0
            item = table.item(row, 0)
            try:
                return int(item.text()) if item else 0
            except (TypeError, ValueError):
                return 0

        def _apply_stats(payload):
            if not payload['ok']:
                stats_bar.setText('统计读取失败')
                QMessageBox.warning(dlg, '数据库错误', f'读取统计信息失败：\n{payload["error"]}')
                return
            stats = payload['data']
            stats_bar.setText(f'帖子: {stats['total_topics']}  |  评论: {stats['total_comments']}  |  图片: {stats['total_images']}  |  已下载: {stats['downloaded_images']}')

        def _apply_topics(payload):
            _set_busy(False)
            if state.get('closed'):
                return
            if not payload['ok']:
                page_label.setText('加载失败')
                QMessageBox.warning(dlg, '读取失败', f'读取帖子列表失败：\n{payload["error"]}')
                return
            data = payload['data']
            if data['gen'] != state['gen']:
                return  # 过期结果（用户已发起新查询），直接丢弃
            total = data['total']
            state['pages'] = data['pages']
            state['page'] = data['page']
            topics = data['topics']
            table.setRowCount(len(topics))
            for i, t in enumerate(topics):
                title = t.get('title', '') or ''
                if len(title) > 50:
                    title = title[:50] + '...'
                items = [QTableWidgetItem(str(t.get('id', ''))), QTableWidgetItem(title), QTableWidgetItem(str(t.get('author', '') or '')), QTableWidgetItem(str(t.get('comments_count', 0))), QTableWidgetItem(str(t.get('scraped_at', '') or ''))]
                for j, item in enumerate(items):
                    if j == 1:
                        item.setForeground(QColor(C['text']))
                        item.setToolTip(t.get('title', '') or '')
                    elif j != 0:
                        item.setForeground(QColor(C['text2']))
                    table.setItem(i, j, item)
                continue
            start = state['page'] * PAGE_SIZE + 1 if total else 0
            end = min((state['page'] + 1) * PAGE_SIZE, total)
            page_label.setText(f'第 {state['page'] + 1}/{state['pages']} 页  ·  {start}-{end} / 共 {total} 条' + (f'  ·  搜索"{state['keyword']}"' if state['keyword'] else ''))
            btn_prev_page.setEnabled(state['page'] > 0)
            btn_next_page.setEnabled(state['page'] < state['pages'] - 1)

        def load_table(keyword=None, reset_page=False, page=None):
            '''后台加载当前页帖子列表（忙碌/重复点击时忽略）。'''

            if state['busy']:
                return
            if keyword is not None:
                state['keyword'] = keyword
            if reset_page:
                state['page'] = 0
            if page is not None:
                state['page'] = max(0, min(page, state['pages'] - 1))
            state['gen'] += 1
            gen = state['gen']
            keyword_now = state['keyword']
            page_now = state['page']
            _set_busy(True)
            page_label.setText('加载中…')

            def _query():
                total = get_topics_count(keyword_now)
                pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                cur = min(max(0, page_now), pages - 1)
                topics = get_topics(limit=PAGE_SIZE, offset=cur * PAGE_SIZE, keyword=keyword_now)
                return {'gen': gen, 'total': total, 'pages': pages, 'page': cur, 'topics': topics}

            _run_async('topics', _query)

        btn_prev_page.clicked.connect((lambda: load_table(page=state['page'] - 1)))
        btn_next_page.clicked.connect((lambda: load_table(page=state['page'] + 1)))

        def on_selection_changed():
            _refresh_selection()

        def on_load():
            tid = _selected_tid()
            if not tid or state['busy']:
                return
            _set_busy(True)

            def _query():
                topic_data = get_topic(tid)
                if not topic_data:
                    return {'missing': True, 'tid': tid}
                # 用 get_comment_dicts：历史数据的 reply_to 由 ref_comment 现场归一化，
                # 直接 get_comments_all 只能拿到没有楼层的原始 ref_comment。
                comments = get_comment_dicts(tid)
                images = get_images(tid)
                result = {'topic': _decode_raw(topic_data), 'comments': [_decode_raw(c) for c in comments], 'image_urls': [img['url'] for img in images], 'total_comments': len(comments)}
                return {'missing': False, 'tid': tid, 'result': result}

            _run_async('load', _query)

        def _apply_load(payload):
            _set_busy(False)
            if state.get('closed'):
                return
            if not payload['ok']:
                QMessageBox.warning(dlg, '读取失败', f'加载帖子失败：\n{payload["error"]}')
                return
            data = payload['data']
            if data.get('missing'):
                QMessageBox.warning(dlg, '提示', f'未找到帖子 #{data["tid"]} 的数据')
                return
            result = data['result']
            self.result_data = result
            self._ocr_results.clear()
            self._load_ocr_from_db(data['tid'])
            self._display_result(result)
            dlg.close()
            self.statusBar.showMessage(f'已从数据库加载帖子 #{data["tid"]}', 3000)

        def on_export_db():
            tid = _selected_tid()
            if not tid or state['busy']:
                return
            default_name = f'douban_topic_{tid}_from_db.json'
            # 文件选择框必须留在主线程；选完路径后再把 SQL 与写文件放后台
            path, _ = QFileDialog.getSaveFileName(dlg, '保存 JSON', default_name, 'JSON 文件 (*.json)')
            if not path:
                return
            _set_busy(True)

            def _query():
                topic_data = get_topic(tid)
                if not topic_data:
                    return {'missing': True, 'tid': tid, 'path': path}
                comments = get_comment_dicts(tid)
                images = get_images(tid)
                export_data = {'topic': _decode_raw(topic_data), 'comments': comments, 'image_urls': [img['url'] for img in images]}
                try:
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(export_data, f, ensure_ascii=False, indent=2)
                except PermissionError:
                    return {'missing': False, 'permission': True, 'tid': tid, 'path': path}
                return {'missing': False, 'permission': False, 'tid': tid, 'path': path}

            _run_async('export', _query)

        def _apply_export(payload):
            _set_busy(False)
            if state.get('closed'):
                return
            if not payload['ok']:
                QMessageBox.warning(dlg, '导出失败', f'导出 JSON 时出错:\n{payload["error"]}')
                return
            data = payload['data']
            if data.get('missing'):
                QMessageBox.warning(dlg, '导出失败', f'未找到帖子 #{data["tid"]} 的数据')
                return
            if data.get('permission'):
                QMessageBox.warning(dlg, '导出失败', f'没有权限写入该路径:\n{data["path"]}')
                return
            QMessageBox.information(dlg, '成功', f'已导出到:\n{data["path"]}')

        def on_delete():
            tid = _selected_tid()
            if not tid or state['busy']:
                return
            # 确认框仍在主线程（阻塞用户交互），确认后再把删除丢后台
            reply = QMessageBox.question(dlg, '确认删除', f'确定要删除帖子 #{tid} 及其所有评论和图片记录吗？\n此操作不可撤销。', QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            _set_busy(True)

            def _query():
                local_paths = db_delete_topic(tid)
                removed = 0
                for p in local_paths:
                    try:
                        if p and os.path.isfile(p):
                            os.remove(p)
                            removed += 1
                    except OSError as e:
                        log_error(f'删除图片文件失败: {p}: {e}')
                        continue
                return {'tid': tid, 'removed': removed}

            _run_async('delete', _query)

        def _apply_delete(payload):
            _set_busy(False)
            if state.get('closed'):
                return
            if not payload['ok']:
                QMessageBox.warning(dlg, '删除失败', f'删除帖子失败：\n{payload["error"]}')
                return
            data = payload['data']
            self.statusBar.showMessage(f'已删除帖子 #{data["tid"]}' + (f'（并清理 {data["removed"]} 个图片文件）' if data['removed'] else ''), 3000)
            load_table(search_input.text().strip())

        table.currentItemChanged.connect((lambda _: on_selection_changed()))
        btn_load.clicked.connect(on_load)
        btn_export_db_json.clicked.connect(on_export_db)
        btn_delete.clicked.connect(on_delete)
        # 初始统计与帖子列表都走后台线程：打开对话框不再被 SQL 卡住
        _run_async('stats', get_stats)
        load_table()
        dlg.exec()
        state['closed'] = True

    def _open_log_file(self):
        '''打开运行日志文件（不存在时打开日志目录）'''

        log_info('打开日志文件')
        log_path = os.path.join(LOG_DIR, 'dbp.log')
        target = log_path if os.path.isfile(log_path) else LOG_DIR
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _show_about(self):
        box = QMessageBox(self)
        box.setWindowTitle('关于 豆瓣小组帖子采集器')
        logo_path = asset('logo_128.png')
        if os.path.isfile(logo_path):
            dpr = self.devicePixelRatioF() or 1.0
            side = int(round(56 * dpr))
            pm = QPixmap(logo_path).scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm.setDevicePixelRatio(dpr)
            box.setIconPixmap(pm)
        box.setTextFormat(Qt.RichText)
        divider = (
            '<table width="100%" cellspacing="0" cellpadding="0" style="margin:12px 0;">'
            f'<tr><td style="background:{C["border"]};height:1px;font-size:1px;">&nbsp;</td></tr>'
            '</table>'
        )
        box.setText(
            f'<h3 style="margin:0;">豆瓣小组帖子采集器</h3>'
            f'<p style="color:{C["text2"]};margin:6px 0 0 0;">抓取 · 阅读 · OCR 识别 · AI 总结 · 导出</p>'
            f'{divider}'
            '<p><b>使用方法：</b></p>'
            '<ol><li>输入帖子 URL 或帖子 ID</li><li>点击「开始爬取」</li><li>查看结果并导出</li></ol>'
            f'<p style="color:{C["text3"]};font-size:11px;">基于 Rexxar API · Python + PySide6</p>'
            f'<p style="color:{C["text3"]};font-size:11px;">仅供学习与技术研究使用（非商业用途）。'
            '请遵守豆瓣用户协议，控制请求频率，勿用于大规模采集。</p>'
        )
        box.setStandardButtons(QMessageBox.Ok)
        ok_button = box.button(QMessageBox.Ok)
        if ok_button is not None:
            ok_button.setText('好')
        box.exec()
