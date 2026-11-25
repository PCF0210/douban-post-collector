# -*- coding: utf-8 -*-
import threading
import traceback

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import QMessageBox

from ..data.db_manager import get_downloaded_image_map, get_ocr_map, save_ocr_results_batch
from ..core.file_log import log_error, log_info
from ..ocr.ocr_extractor import OCRExtractor, OCRWorker, tidy_text
from .widgets import _OcrSignalSink


class OcrControlMixin:
    """OcrControlMixin"""

    def _prewarm_ocr(self):
        '''后台预热 OCR 模型（只做一次，失败不影响任何功能）'''
        if self._ocr_warmed:
            return
        extractor = OCRExtractor.peek_instance()
        if extractor is None or not extractor.available:
            return
        self._ocr_warmed = True

        def _work():
            try:
                extractor.warmup()
            except Exception:
                log_error('OCR 预热异常:\n' + traceback.format_exc())

        threading.Thread(target=_work, daemon=True).start()

    def _start_ocr_probe(self, force=False):
        '''后台探测 OCR 引擎（绝不阻塞 UI 线程）

        force=True 时先重置单例，用于「设置 → OCR 引擎状态」里的手动重新检测。
        '''
        if self._ocr_probe_running:
            return
        self._ocr_probe_running = True
        if force:
            OCRExtractor.reset_instance()

        def _work():
            available = False
            try:
                available = OCRExtractor.get_instance().available
            except Exception:
                log_error('OCR 引擎探测异常:\n' + traceback.format_exc())
            try:
                self._ocr_probe_relay.done.emit(available)
            except RuntimeError:
                pass  # 窗口已销毁

        threading.Thread(target=_work, daemon=True).start()

    def _on_ocr_probe_done(self, available):
        self._ocr_probe_running = False
        if available:
            self._ocr_probe_attempts = 0
        elif self._ocr_probe_attempts < 2:
            # 第一次探测可能正好撞上 tesseract 启动超时（有日志为证），
            # 自动重试两次，避免按钮一直停在「OCR 不可用」。
            self._ocr_probe_attempts += 1
            QTimer.singleShot(1200, lambda: self._start_ocr_probe(force=True))
        self._refresh_ocr_button()

    def _refresh_ocr_button(self, n_img=None):
        '''刷新 OCR 按钮的可用状态与文案

        只读取后台探测的结果（peek_instance），绝不在这里同步探测引擎——
        否则 tesseract 启动慢就会把整个界面卡住，并把「暂时超时」永久记住。
        '''

        if n_img is None and self.result_data:
            n_img = len(self.result_data.get('image_urls', []))
        if self._ocr_running:
            # OCR 正在跑时按钮必须是「停止识别」：后台探测完成等刷新不能把它改回统计文案
            self.btn_ocr.setEnabled(True)
            self.btn_ocr.setText('停止识别')
            return
        extractor = OCRExtractor.peek_instance()
        if extractor is None:
            self.btn_ocr.setEnabled(False)
            self.btn_ocr.setText('OCR 检测中…')
            self.btn_ocr.setToolTip('正在后台检测 OCR 引擎，稍候自动更新')
            return
        if not extractor.available:
            self.btn_ocr.setEnabled(False)
            self.btn_ocr.setText('OCR 不可用')
            self.btn_ocr.setToolTip('未检测到可用的 OCR 引擎（RapidOCR / Tesseract），请查看 设置 → OCR 引擎状态')
            return
        if not n_img:
            self.btn_ocr.setEnabled(False)
            self.btn_ocr.setText('OCR 识别图片文字')
            self.btn_ocr.setToolTip('当前帖子无图片' if self.result_data else '请先抓取帖子')
            return
        recognized = sum((1 for v in self._ocr_results.values() if v))
        if recognized > 0:
            self.btn_ocr.setText(f'OCR 重新识别 ({recognized}/{n_img} 已识别)')
        else:
            self.btn_ocr.setText(f'OCR 识别图片文字 ({n_img} 张)')
        self.btn_ocr.setEnabled(True)
        self.btn_ocr.setToolTip('对所有评论图片运行 OCR 文字识别')

    def run_ocr(self):
        '''对所有评论图片运行 OCR 识别（进行中再次点击 = 停止识别）'''

        if self._thread_running(self.ocr_worker_thread):
            if self.ocr_worker is not None:
                try:
                    self.ocr_worker.stop()
                except RuntimeError:
                    pass
            self.btn_ocr.setText('正在停止...')
            self._ocr_cancel_requested = True
            return
        if not self.result_data:
            QMessageBox.information(self, '提示', '请先爬取帖子内容后再进行 OCR 识别。')
            return
        image_urls = self.result_data.get('image_urls', [])
        if not image_urls:
            QMessageBox.information(self, '提示', '该帖子中没有发现图片。')
            return
        extractor = OCRExtractor.peek_instance()
        if extractor is None:
            # 检测在后台线程跑，这里不能用模态框：弹框会卡住用户（也会让脚本化验证挂死）
            self._start_ocr_probe()
            self.btn_ocr.setText('OCR 检测中…')
            self.statusBar.showMessage('正在后台检测 OCR 引擎，完成后按钮会自动恢复，请稍候再试', 5000)
            return
        if not extractor.available:
            QMessageBox.warning(self, 'OCR 不可用', f'OCR 引擎不可用：\n\n{extractor.status_message()}\n\n配置完成后重启程序生效。')
            return
        topic_id = self.result_data.get('topic', {}).get('id', 0)
        self._ocr_job_id += 1
        job_id = self._ocr_job_id
        self._ocr_cancel_requested = False
        self._ocr_running = True
        self._ocr_topic_id = topic_id
        pending_urls = [u for u in image_urls if u not in self._ocr_results]
        if not pending_urls:
            reply = QMessageBox.question(self, '已识别完成', f'所有 {len(image_urls)} 张图片已识别过。\n是否要重新识别？', QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                pending_urls = list(image_urls)
            else:
                return
        try:
            local_map = get_downloaded_image_map(topic_id)
        except Exception:
            local_map = {}
        self.btn_ocr.setEnabled(True)
        self.btn_ocr.setText('停止识别')
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.statusBar.showMessage(f'开始 OCR 识别 {len(pending_urls)} 张图片...')
        self.ocr_worker_thread = QThread()
        proxy_enabled, proxy_url = self._read_proxy()
        self.ocr_worker = OCRWorker(pending_urls, language=self._read_ocr_language(), use_proxy=proxy_enabled, proxy=proxy_url, local_paths=local_map)
        if local_map:
            n_local = sum((1 for u in pending_urls if u in local_map))
            if n_local:
                self.statusBar.showMessage(f'{n_local} 张图片将使用本地缓存（无需下载）', 3000)
        self.ocr_worker.moveToThread(self.ocr_worker_thread)
        self.ocr_worker_thread.started.connect(self.ocr_worker.run)
        self.ocr_worker.log.connect(self._on_ocr_log, Qt.QueuedConnection)
        self.ocr_worker.progress.connect(self._on_ocr_progress, Qt.QueuedConnection)
        # lambda 没有接收者对象，QueuedConnection 也会在 OCR 线程里执行；用绑定到
        # 窗口的 _OcrSignalSink 做接收者，信号才会真正排队到主线程。
        self._ocr_sink = _OcrSignalSink(self, job_id)
        self.ocr_worker.image_ocr_done.connect(self._ocr_sink.on_image_done, Qt.QueuedConnection)
        self.ocr_worker.image_ocr_failed.connect(self._ocr_sink.on_image_failed, Qt.QueuedConnection)
        self.ocr_worker.finished.connect(self._ocr_sink.on_finished, Qt.QueuedConnection)
        self.ocr_worker.error.connect(self._ocr_sink.on_error, Qt.QueuedConnection)
        self.ocr_worker.finished.connect(self.ocr_worker_thread.quit)
        self.ocr_worker.error.connect(self.ocr_worker_thread.quit)
        self.ocr_worker_thread.finished.connect(self.ocr_worker.deleteLater)
        self.ocr_worker_thread.finished.connect(self.ocr_worker_thread.deleteLater)
        # 收尾（清引用 + 关闭窗口续关）走主线程中继，不能用无接收者 lambda
        self._watch_thread_finished(self.ocr_worker_thread, 'ocr_worker_thread', 'ocr_worker')
        self.ocr_worker_thread.start()

    def _on_ocr_log(self, msg):
        self.statusBar.showMessage(msg)
        log_info(msg)

    def _on_ocr_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def _on_ocr_image_done(self, url, text, job_id=None):
        '''单张图片 OCR 完成，实时更新内存缓存'''

        if job_id is not None and job_id != self._ocr_job_id:
            return
        if self._ocr_topic_id and str(self._current_topic_id()) != str(self._ocr_topic_id):
            return
        self._ocr_results[url] = text

    def _on_ocr_image_failed(self, url, job_id=None):
        '''单张图片下载或识别失败：移出缓存，下次识别会重试'''

        if job_id is not None and job_id != self._ocr_job_id:
            return
        if self._ocr_topic_id and str(self._current_topic_id()) != str(self._ocr_topic_id):
            return
        self._ocr_results.pop(url, None)

    def _on_ocr_finished(self, results, job_id=None):
        '''全部 OCR 完成'''

        if job_id is not None and job_id != self._ocr_job_id:
            return
        self._ocr_running = False
        ocr_topic = self._ocr_topic_id
        # 失败项（None）既不写库也不进内存缓存：写空串会让失败的图片
        # 永久显示为「已识别但无文字」，之后再也不会重试
        done = {u: t for u, t in results.items() if t is not None}
        failed = len(results) - len(done)
        # 结果始终写回开始识别时的帖子，避免识别期间切换到别的帖子后写错库。
        if ocr_topic:
            try:
                save_ocr_results_batch(done, topic_id=ocr_topic)
            except Exception as e:
                self.statusBar.showMessage(f'OCR 结果保存失败: {e}', 3000)
        if ocr_topic and str(self._current_topic_id()) != str(ocr_topic):
            self.progress_bar.setVisible(False)
            self.statusBar.showMessage(f'后台 OCR 完成：帖子 #{ocr_topic} 的结果已保存（当前显示的是其他帖子）', 5000)
            return
        self._ocr_results.update(done)
        if self.result_data:
            # 原地补写识别文字；必须延后到事件循环空闲：
            # 在 OCR finished 信号的处理过程中直接调用 WebEngine 原生崩溃（0x80000003）。
            QTimer.singleShot(500, lambda: self._apply_ocr_to_comment_view(done))
            QTimer.singleShot(500, self._apply_ocr_to_content_view)
        n_img = len(self.result_data.get('image_urls', [])) if self.result_data else 0
        self._refresh_ocr_button(n_img)
        self.progress_bar.setVisible(False)
        recognized = sum((1 for v in done.values() if v))
        prefix = 'OCR 已取消' if self._ocr_cancel_requested else 'OCR 完成'
        msg = f'{prefix}: {len(done)} 张已处理, {recognized} 张含文字'
        if failed:
            msg += f', {failed} 张识别失败（可重试）'
        self.statusBar.showMessage(msg, 5000)

    def _on_ocr_error(self, msg, job_id=None):
        if job_id is not None and job_id != self._ocr_job_id:
            return
        self._ocr_running = False
        if self._ocr_topic_id and str(self._current_topic_id()) != str(self._ocr_topic_id):
            self.statusBar.showMessage(f'后台 OCR 失败：{msg}', 5000)
            log_error(msg)
            return
        self.progress_bar.setVisible(False)
        self._refresh_ocr_button()
        QMessageBox.warning(self, 'OCR 识别失败', f'OCR 识别出错：\n\n{msg}\n\n请检查 OCR 引擎是否正常（设置 → OCR 引擎状态）。')
        log_error(msg)

    def _load_ocr_from_db(self, topic_id):
        '''从数据库加载已保存的 OCR 结果到内存缓存（先清空旧帖缓存）'''

        # 必须先清空：否则新帖没有 OCR 记录时，上一条帖子的识别文字会
        # 继续显示在新帖的评论/图片上（测试中已确认这条路径存在）。
        self._ocr_results.clear()
        if not topic_id:
            return
        try:
            db_map = get_ocr_map(topic_id)
            if db_map:
                # 旧记录是按老规则存的（如「2026.11.0213:36:32」缺空格），
                # 显示前统一过一遍后处理，不必回写数据库。
                self._ocr_results.update({u: tidy_text(t) if t else '' for u, t in db_map.items()})
                return
        except Exception:
            # 读库失败不应影响主流程，但必须留下日志，避免「OCR 结果莫名不显示」
            # 这类问题完全无从排查（旧实现这里是静默 pass）。
            log_error('加载数据库 OCR 结果失败:\n' + traceback.format_exc())
