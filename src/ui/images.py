# -*- coding: utf-8 -*-
import os
import traceback

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..crawl.image_downloader import ImageDownloadWorker
from ..core.file_log import log_error, log_info


class ImageMixin:
    """ImageMixin"""

    def download_images(self):
        if self._thread_running(self.image_worker_thread):
            QMessageBox.information(self, '提示', '图片正在下载中，请等待完成或先停止当前任务。')
            return
        if not self.result_data:
            QMessageBox.information(self, '提示', '请先爬取帖子内容后再下载图片。')
            return
        image_urls = self.result_data.get('image_urls', [])
        if not image_urls:
            QMessageBox.information(self, '提示', '该帖子中没有发现图片。')
            return
        topic_id = self.result_data.get('topic', {}).get('id', 'unknown')
        s = self._settings()
        # 图片保存目录持久化：新键 paths/image_dir 供画廊推导本地文件用；
        # 旧键 images/last_dir 继续写一份，兼容旧版本的设置。
        last_dir = s.value('paths/image_dir', '') or s.value('images/last_dir', '')
        save_dir = QFileDialog.getExistingDirectory(self, '选择图片保存目录', last_dir)
        if not save_dir:
            return
        s.setValue('paths/image_dir', save_dir)
        s.setValue('images/last_dir', save_dir)
        save_dir = os.path.join(save_dir, f'douban_images_{topic_id}')
        self.btn_download_images.setEnabled(False)
        self.btn_download_images.setText('下载中...')
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.statusBar.showMessage(f'开始下载 {len(image_urls)} 张图片...')
        self.image_worker_thread = QThread()
        proxy_enabled, proxy_url = self._read_proxy()
        self.image_worker = ImageDownloadWorker(image_urls, save_dir, use_proxy=proxy_enabled, proxy=proxy_url, topic_id=topic_id)
        self.image_worker.moveToThread(self.image_worker_thread)
        self.image_worker_thread.started.connect(self.image_worker.run)
        self.image_worker.log.connect(self._on_image_log, Qt.QueuedConnection)
        self.image_worker.progress.connect(self._on_image_progress, Qt.QueuedConnection)
        self.image_worker.finished.connect(self._on_image_finished, Qt.QueuedConnection)
        self.image_worker.error.connect(self._on_image_error, Qt.QueuedConnection)
        self.image_worker.finished.connect(self.image_worker_thread.quit)
        self.image_worker.error.connect(self.image_worker_thread.quit)
        # 收尾（清引用 + 关闭窗口续关）必须走主线程中继，不能再用无接收者 lambda
        self._watch_thread_finished(self.image_worker_thread, 'image_worker_thread', 'image_worker')
        self.image_worker_thread.finished.connect(self.image_worker.deleteLater)
        self.image_worker_thread.finished.connect(self.image_worker_thread.deleteLater)
        self.image_worker_thread.start()

    def _on_image_log(self, msg):
        self.statusBar.showMessage(msg)
        log_info(msg)

    def _on_image_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def _on_image_finished(self, count, save_dir):
        self.btn_download_images.setEnabled(True)
        self.btn_download_images.setText('下载图片')
        self.progress_bar.setVisible(False)
        self.statusBar.showMessage(f'图片下载完成: {count} 张已保存到 {save_dir}', 5000)
        # 下载完成后刷新画廊：已下载的图片改用本地文件显示，无需再走网络
        if not getattr(self, '_pending_close', False) and self.result_data:
            try:
                self._update_gallery(self.result_data.get('image_urls', []))
            except Exception:
                log_error('刷新图片画廊失败:\n' + traceback.format_exc())

    def _on_image_error(self, msg):
        self.btn_download_images.setEnabled(True)
        self.btn_download_images.setText('下载图片')
        self.progress_bar.setVisible(False)
        self.statusBar.showMessage(f'图片下载失败: {msg}')
        log_error(msg)
