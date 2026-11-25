# -*- coding: utf-8 -*-
import re
import traceback

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView,
                               QLabel, QMessageBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ..crawl.scraper import ScraperWorker
from ..core.file_log import log_error, log_info
from ..data.db_manager import get_comment_dicts, get_images, get_topic
from .html_render import build_comments_html, build_content_html
from .ui_theme import PALETTE as C
from .widgets import _cn_buttons


class _ThreadDoneRelay(QObject):
    '''QThread.finished 的主线程收尾中继

    finished 信号是从 worker 线程发出的；lambda 没有接收者对象，PySide6 会直接
    在发信号的线程里执行它（在里面清 QThread 引用/碰界面都属于跨线程操作）。
    这里用绑定在主窗口（主线程）的 QObject 方法做接收者，并显式 QueuedConnection，
    保证收尾逻辑一定在主线程执行。
    '''

    def __init__(self, window, thread_attr, worker_attr, thread):
        super().__init__(window)
        self._window = window
        self._thread_attr = thread_attr
        self._worker_attr = worker_attr
        self._thread = thread

    def on_done(self):
        '''线程已退出（主线程执行）：清引用，再按停止请求决定是否显示终态'''

        window = self._window
        window._finalize_worker(self._thread_attr, self._worker_attr, self._thread)
        window._after_worker_cleanup()
        self.deleteLater()


class ScrapeMixin:
    """ScrapeMixin"""

    def parse_topic_ids(self, text):
        '''解析输入文本，返回帖子 ID 列表（支持多行 / 逗号 / 空格分隔）'''

        text = text.strip()
        if not text:
            return []
        parts = re.split('[\\n,，\\s]+', text)
        ids = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part.isdigit():
                ids.append(part)
                continue
            match = re.search('/topic/(\\d+)', part)
            if match:
                ids.append(match.group(1))
                continue
            if not re.match('^https?://', part, re.IGNORECASE):
                continue
            if 'd.douban.com' in part:
                ids.append(part)
        seen = set()
        unique = []
        for tid in ids:
            if tid not in seen:
                seen.add(tid)
                unique.append(tid)
        return unique

    def parse_topic_id(self, text):
        ids = self.parse_topic_ids(text)
        if ids:
            return ids[0]

    def start_incremental(self):
        '''增量更新：只拉上次入库之后的新评论（本地没有该帖时自动完整抓取）'''

        self.start_scrape(incremental=True)

    def start_scrape(self, incremental=False):
        # 先确认没有后台线程仍在运行：旧任务还没退出就启动新任务会出现同一帖子
        # 双 worker、旧信号串到新任务等问题，等它彻底收工再开始更稳妥。
        if self._has_running_workers():
            QMessageBox.warning(self, '任务进行中',
                                '任务进行中，请先停止当前任务，等后台线程退出后再开始新的爬取。')
            return
        topic_ids = self.parse_topic_ids(self.url_input.toPlainText())
        if not topic_ids:
            QMessageBox.warning(self, '提示', '请输入有效的帖子 URL 或帖子 ID')
            return
        # 确认没有运行中的 worker 之后才复位停止标志：否则旧任务迟到的
        # finished/error 会把新任务误判成「已停止」（串台）。
        self._scrape_stop_requested = False
        self._batch_stop_requested = False
        self._stop_clears_result = False
        self._stopped_state_shown = False
        self._stop_batch_timer()
        self._batch_queue = []
        self._batch_current_index = 0
        self._batch_results = []
        self._batch_errors = {}
        self._last_batch_result = None
        self.result_data = None
        self.content_text.setHtml(build_content_html(
            '<div class="welcome"><span class="welcome-mark">抓取中</span>'
            '<h1>正在抓取帖子…</h1><p>请稍候，完成后会自动展示正文。</p></div>'
        ), baseUrl=QUrl('https://m.douban.com/'))
        self.comment_view.setHtml(build_comments_html(None), baseUrl=QUrl('https://m.douban.com/'))
        self.comment_count_label.setText('')
        self._reset_comment_dates()
        self._reset_info('正在抓取…')
        self.btn_export_json.setEnabled(False)
        self.btn_export_txt.setEnabled(False)
        self.btn_export_csv.setEnabled(False)
        self.btn_export_md.setEnabled(False)
        self.btn_download_images.setEnabled(False)
        self.btn_ocr.setEnabled(False)
        self.btn_ai.setEnabled(False)
        self._ocr_results.clear()
        self._update_gallery([])
        self._search_matches = 0
        self._search_current = 0
        self._update_search_ui()
        if len(topic_ids) == 1:
            self._is_batch_mode = False
            self._batch_incremental = False
            self._start_single_scrape(topic_ids[0], incremental=incremental)
            return
        self._is_batch_mode = True
        self._batch_incremental = bool(incremental)
        self._batch_queue = list(topic_ids)
        self._batch_current_index = 0
        self.statusBar.showMessage(f'批量爬取: 共 {len(topic_ids)} 个帖子')
        self._start_batch_next()

    def _start_single_scrape(self, topic_id, incremental=False):
        '''启动单个帖子的爬取（incremental=True 时只拉新评论）'''

        self.btn_scrape.setEnabled(False)
        self.btn_incremental.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.statusBar.showMessage('正在爬取...')
        self.worker_thread = QThread()
        proxy_enabled, proxy_url = self._read_proxy()
        self.worker = ScraperWorker(topic_id, use_proxy=proxy_enabled, proxy=proxy_url,
                                   incremental=incremental)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        # 这些槽都要碰界面控件：普通 bound-method connect 在 PySide6 里可能退化成
        # DirectConnection（槽在 worker 线程执行），必须显式排队到主线程。
        self.worker.log.connect(self._on_log, Qt.QueuedConnection)
        self.worker.progress.connect(self._on_progress, Qt.QueuedConnection)
        self.worker.finished.connect(self._on_finished, Qt.QueuedConnection)
        self.worker.error.connect(self._on_error, Qt.QueuedConnection)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.error.connect(self.worker_thread.quit)
        # 收尾必须走主线程中继（清引用/停止态判定都在主线程）
        self._watch_thread_finished(self.worker_thread, 'worker_thread', 'worker')
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    def _start_batch_next(self):
        '''启动批量爬取中的下一个帖子（由成员 QTimer 触发，可随时取消）'''

        if not self._is_batch_mode or self._batch_stop_requested:
            return
        if self._has_running_workers():
            # 上一个帖子的线程还没完全退出：稍后重试，避免同一个帖子同时跑两个 worker
            self._schedule_batch_next(300)
            return
        if self._batch_current_index >= len(self._batch_queue):
            self._on_batch_all_done()
            return
        topic_id = self._batch_queue[self._batch_current_index]
        total = len(self._batch_queue)
        self.statusBar.showMessage(f'批量 [{self._batch_current_index + 1}/{total}] 正在爬取 {topic_id}...')
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(int(self._batch_current_index * (100 / total)))
        self.btn_scrape.setEnabled(False)
        self.btn_incremental.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.content_text.setHtml(build_content_html('<p style="color: #D1D5DB;">爬取后将显示帖子内容...</p>'), baseUrl=QUrl('https://m.douban.com/'))
        self.comment_view.setHtml(build_comments_html(None), baseUrl=QUrl('https://m.douban.com/'))
        self.worker_thread = QThread()
        proxy_enabled, proxy_url = self._read_proxy()
        self.worker = ScraperWorker(topic_id, use_proxy=proxy_enabled, proxy=proxy_url,
                                   incremental=self._batch_incremental)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self._on_batch_log, Qt.QueuedConnection)
        self.worker.progress.connect(self._on_batch_post_progress, Qt.QueuedConnection)
        self.worker.finished.connect(self._on_batch_one_finished, Qt.QueuedConnection)
        self.worker.error.connect(self._on_batch_one_error, Qt.QueuedConnection)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.error.connect(self.worker_thread.quit)
        self._watch_thread_finished(self.worker_thread, 'worker_thread', 'worker')
        self.worker_thread.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.start()

    def _batch_tid(self, worker=None):
        '''取当前批次的 topic id（越界时不再抛异常）

        之前是 self._batch_queue[self._batch_current_index]，而 stop_scrape 会
        清空队列；飞行中的 worker 仍会 emit log，槽里越界抛出的 IndexError 在
        Qt 事件循环里无人捕获，Qt 会直接 abort() 掉整个进程。
        '''

        q = self._batch_queue
        idx = self._batch_current_index
        if not q or idx < 0 or idx >= len(q):
            return '?'
        return q[idx]

    def _on_batch_log(self, msg):
        self.statusBar.showMessage(f'[#{self._batch_tid()}] {msg}')
        log_info(msg)

    def _on_batch_post_progress(self, current, total):
        '''批量模式下单个帖子内部的进度更新，映射到整体进度'''

        batch_total = len(self._batch_queue)
        if batch_total <= 0 or total <= 0:
            return
        overall = (self._batch_current_index + current / total) * (100 / batch_total)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(int(overall))

    def _on_batch_one_finished(self, result):
        '''单个帖子爬取完成（批量模式）

        只保留摘要（标题/评论数等），不再把每个帖子的完整 result 攒在内存里：
        一次批量几十个帖子、每个帖子几千条评论时旧实现会占住大量内存。完整结果
        只在结束时保留最近一帖用于展示；摘要对话框双击时按需从数据库重载。
        '''

        if self._batch_stop_requested or not self._is_batch_mode:
            return
        tid = self._batch_tid()
        topic = result.get('topic') or {}
        comments = result.get('comments') or []
        images = result.get('image_urls') or []
        self._batch_results.append({
            'topic_id': str(tid),
            'title': (topic.get('title') or '')[:60],
            'url': topic.get('url') or '',
            'total_comments': len(comments),
            'image_count': len(images),
            'new_count': int(result.get('new_count') or 0),
            'incremental': bool(result.get('incremental')),
        })
        # 只保留最近一帖的完整结果，供批量结束后直接展示
        self._last_batch_result = result
        n_cmt = len(comments)
        n_img = len(images)
        if result.get('incremental'):
            self.statusBar.showMessage(
                f'[{self._batch_current_index + 1}/{len(self._batch_queue)}] #{tid} 增量完成'
                f' — 新增 {result.get("new_count", 0)} 条, 共 {n_cmt} 评论, {n_img} 图片')
        else:
            self.statusBar.showMessage(f'[{self._batch_current_index + 1}/{len(self._batch_queue)}] #{tid} 完成 — {n_cmt} 评论, {n_img} 图片')
        self._batch_current_index += 1
        batch_total = len(self._batch_queue)
        self.progress_bar.setValue(int(self._batch_current_index * (100 / batch_total)) if batch_total else 0)
        self._schedule_batch_next(800)

    def _on_batch_one_error(self, msg):
        '''单个帖子爬取失败（批量模式）— 跳过继续下一个'''

        if self._batch_stop_requested or msg == '已取消' or not self._is_batch_mode:
            return
        batch_total = len(self._batch_queue)
        tid = self._batch_tid()
        self._batch_errors[str(tid)] = msg
        self.statusBar.showMessage(f'[{self._batch_current_index + 1}/{batch_total}] #{tid} 失败: {msg}')
        self._batch_current_index += 1
        self.progress_bar.setValue(int(self._batch_current_index * (100 / batch_total)) if batch_total else 0)
        self._schedule_batch_next(500)

    def _on_batch_all_done(self):
        '''全部批量任务完成'''

        if self._batch_stop_requested or self._scrape_stop_requested:
            return
        self._stop_batch_timer()
        self.btn_scrape.setEnabled(True)
        self.btn_incremental.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setVisible(False)
        self._batch_incremental = False
        ok = len(self._batch_results)
        fail = len(self._batch_errors)
        summary = f'批量完成: {ok} 成功'
        if fail > 0:
            summary += f', {fail} 失败'
        self.statusBar.showMessage(summary, 5000)
        # 必须先复位：_display_result 看到 _is_batch_mode 为 True 就不会恢复按钮状态
        self._is_batch_mode = False
        last_result = getattr(self, '_last_batch_result', None)
        self._last_batch_result = None
        if last_result:
            self.result_data = last_result
            last_tid = (last_result.get('topic') or {}).get('id', 0)
            # 切到最后一帖要重新载入它自己的 OCR 结果，否则会沿用上一帖的 _ocr_results
            if last_tid:
                self._load_ocr_from_db(last_tid)
            self._display_result(last_result)
        elif ok + fail:
            self._reset_result_ui(
                '批量完成',
                '<div class="welcome"><span class="welcome-mark">批量完成</span>'
                f'<h1>{fail} 个帖子全部抓取失败</h1>'
                '<p>可以查看结果摘要或日志了解失败原因。</p></div>',
                summary,
            )
        if ok + fail > 1:
            self._show_batch_summary()

    def _show_batch_summary(self):
        '''批量完成后显示结果摘要对话框（双击行可加载到主窗口）'''

        dlg = QDialog(self)
        dlg.setWindowTitle(f'批量爬取结果 — {len(self._batch_results)} 成功 / {len(self._batch_errors)} 失败')
        dlg.resize(680, 380)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(['状态', 'ID', '标题', '评论数', '图片数'])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.setAlternatingRowColors(True)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        lay.addWidget(table)
        hint = QLabel('双击成功的行可加载该帖子到主窗口')
        hint.setStyleSheet(f'color: {C['text2']}; font-size: 11px; border: none;')
        lay.addWidget(hint)
        buttons = _cn_buttons(QDialogButtonBox(QDialogButtonBox.Close))
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        ok_ids = {item['topic_id'] for item in self._batch_results}
        rows = []
        for item in self._batch_results:
            rows.append(('✓ 成功', item['topic_id'], (item['title'] or '')[:40],
                         str(item['total_comments']), str(item['image_count'])))
        for tid, msg in self._batch_errors.items():
            rows.append(('✗ 失败', str(tid), msg[:60], '-', '-'))
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, val in enumerate(row):
                item = QTableWidgetItem(val)
                if j == 0:
                    item.setForeground(QColor(C['green'] if val.startswith('✓') else C['red']))
                elif j == 2:
                    item.setForeground(QColor(C['text2']))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(i, j, item)

        def on_cell_double_clicked(row, _col):
            if row < 0 or row >= len(rows):
                return
            status, tid = rows[row][0], rows[row][1]
            if not status.startswith('✓') or tid not in ok_ids:
                return
            loaded = self._load_topic_from_db(tid)
            if loaded is None:
                return
            self.result_data = loaded
            self._ocr_results.clear()
            self._load_ocr_from_db(int(tid))
            self._display_result(loaded)
            dlg.accept()

        table.cellDoubleClicked.connect(on_cell_double_clicked)
        dlg.exec()

    def _load_topic_from_db(self, topic_id):
        '''从数据库重载一个帖子的完整结果（批量摘要双击用）

        批量模式不再把每个帖子的完整 result 留在内存里，需要展示时按数据库记录
        重新组装成和爬虫 result 相同的结构（与数据库浏览器加载逻辑一致）。
        '''

        try:
            tid = int(str(topic_id).strip())
        except (TypeError, ValueError):
            QMessageBox.warning(self, '提示', f'无法识别帖子 ID: {topic_id}')
            return None
        try:
            topic = get_topic(tid)
            if not topic:
                QMessageBox.information(self, '提示', f'数据库中没有帖子 #{tid}')
                return None
            comments = get_comment_dicts(tid) or []
            image_urls = []
            seen = set()
            for img in get_images(tid) or []:
                url = img.get('url') if isinstance(img, dict) else ''
                if url and url not in seen:
                    seen.add(url)
                    image_urls.append(url)
            return {
                'topic': topic,
                'comments': list(comments),
                'image_urls': image_urls,
                'total_comments': len(comments),
                'incremental': False,
                'new_count': 0,
            }
        except Exception:
            log_error(f'从数据库加载帖子 #{tid} 失败:\n' + traceback.format_exc())
            QMessageBox.warning(self, '加载失败', f'从数据库读取帖子 #{tid} 失败，请查看日志。')
            return None

    # ---------- 线程生命周期 / 停止 ----------

    @staticmethod
    def _thread_running(th):
        '''安全判断 QThread 是否仍在运行（C++ 对象可能已被 deleteLater 删除）'''

        if th is None:
            return False
        try:
            return th.isRunning()
        except RuntimeError:
            return False

    def _has_running_workers(self):
        '''是否有任何后台 worker 线程仍在运行'''

        return any(self._thread_running(th) for th in
                   (self.worker_thread, self.image_worker_thread, self.ocr_worker_thread))

    def _running_worker_threads(self):
        '''返回仍在运行的后台线程列表（关闭窗口时逐个接 finished）'''

        out = []
        for th in (self.worker_thread, self.image_worker_thread, self.ocr_worker_thread):
            if self._thread_running(th):
                out.append(th)
        return out

    def _watch_thread_finished(self, thread, thread_attr, worker_attr):
        '''线程结束时做「主线程安全的收尾」（清引用 + 停止态判定）

        不能用无接收者的 lambda：PySide6 里它会在发信号的 worker 线程执行。
        这里统一用主线程 QObject 中继承接 finished 信号。
        '''

        relay = _ThreadDoneRelay(self, thread_attr, worker_attr, thread)
        thread.finished.connect(relay.on_done, Qt.QueuedConnection)
        return relay

    def _finalize_worker(self, thread_attr, worker_attr, thread):
        '''线程结束后清空引用；仅当引用仍是这个线程时（防止旧信号踩新任务）'''

        if getattr(self, thread_attr, None) is thread:
            setattr(self, thread_attr, None)
            setattr(self, worker_attr, None)

    def _after_worker_cleanup(self):
        '''后台线程退出后（主线程）：按停止请求决定是否进入「已停止」终态

        只有「停止请求已发出 且 所有后台线程都已退出」才切终态；批量模式中途
        退出一个线程时不能提前把界面重置（还有后续线程/定时器在跑）。
        '''

        if not self._scrape_stop_requested:
            return
        if self._has_running_workers():
            return
        self._stop_batch_timer()
        if not getattr(self, '_stop_clears_result', False):
            # 只是停止了 OCR / 图片下载：保留已展示的抓取结果，别清空界面
            self._scrape_stop_requested = False
            self._batch_stop_requested = False
            self.btn_stop.setEnabled(False)
            self.statusBar.showMessage('就绪', 2000)
            return
        self._batch_queue = []
        self._batch_current_index = 0
        self._batch_incremental = False
        self.show_stopped_state()

    def _request_workers_stop(self):
        '''请求停止所有后台 worker（只置停止标志，绝不 wait()/terminate()）

        QThread.quit() 只退出线程的事件循环，而 worker.run() 是 started 信号
        直接调用的阻塞函数（不跑事件循环），所以 quit() 对它无效；必须靠
        worker.stop() + 可中断 sleep 让 run() 自己返回。
        '''

        self._scrape_stop_requested = True
        self._batch_stop_requested = True
        self._stop_batch_timer()
        if self._thread_running(self.ocr_worker_thread):
            self._ocr_cancel_requested = True
        for w in (self.worker, self.image_worker, self.ocr_worker):
            if w is None:
                continue
            try:
                w.stop()
            except Exception:
                log_error('停止后台任务失败:\n' + traceback.format_exc())

    def _ensure_batch_timer(self):
        '''批量模式使用的单次 QTimer（成员对象，可以随时 stop 取消）'''

        timer = getattr(self, '_batch_timer', None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._start_batch_next)
            self._batch_timer = timer
        return timer

    def _stop_batch_timer(self):
        timer = getattr(self, '_batch_timer', None)
        if timer is None:
            return
        try:
            timer.stop()
        except RuntimeError:
            return

    def _schedule_batch_next(self, delay_ms):
        '''安排批量模式下的下一个帖子：总是先停旧的单次定时器，避免重复触发'''

        self._stop_batch_timer()
        timer = self._ensure_batch_timer()
        timer.start(max(0, int(delay_ms)))

    def stop_scrape(self):
        '''请求停止当前任务（只发请求立刻返回，不阻塞界面）

        旧实现会同步 _stop_workers_and_wait(5000) 甚至在超时后 terminate() 线程：
        停止大批量任务时界面直接卡死，terminate 还可能让 QtWebEngine 崩溃。
        现在只置停止标志 + 通知 worker 自己退出，真正的界面复位在最后一个线程
        finished 后的 _after_worker_cleanup 里做。
        '''

        scrape_active = bool(self._is_batch_mode or self._thread_running(self.worker_thread))
        self._stop_clears_result = scrape_active
        self._request_workers_stop()
        self.btn_stop.setEnabled(False)
        self.statusBar.showMessage('正在停止…')
        if not self._has_running_workers():
            # 没有线程在跑（例如批量两个帖子之间）：立即收尾
            self._after_worker_cleanup()

    def _on_log(self, msg):
        self.statusBar.showMessage(msg)
        log_info(msg)

    def _on_progress(self, current, total):
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
            return
        self.progress_bar.setMaximum(0)
