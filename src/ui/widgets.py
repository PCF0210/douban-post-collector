# -*- coding: utf-8 -*-
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QObject, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .ui_theme import PALETTE as C

class _ProxyTestResult(QObject):
    '''代理测试结果信号中继（后台线程 → UI 线程）'''

    done = Signal(bool, str)

class _OcrProbeResult(QObject):
    '''OCR 引擎探测结果信号中继（后台线程 → UI 线程）'''

    done = Signal(bool)

class _OcrSignalSink(QObject):
    '''OCRWorker 信号 → 主窗口槽的中继

    直接给 connect 传 lambda 时 PySide6 找不到接收者对象，即使写了
    Qt.QueuedConnection 也会在发信号的线程（OCR 线程）里执行，槽里碰控件
    就会跨线程。这里用绑定到窗口的 QObject 方法做接收者，才能真正排队到主线程。
    '''

    def __init__(self, window, job_id):
        super().__init__(window)
        self._window = window
        self._job_id = job_id

    def on_image_done(self, url, text):
        self._window._on_ocr_image_done(url, text, self._job_id)

    def on_image_failed(self, url):
        self._window._on_ocr_image_failed(url, self._job_id)

    def on_finished(self, results):
        self._window._on_ocr_finished(results, self._job_id)

    def on_error(self, message):
        self._window._on_ocr_error(message, self._job_id)

def _make_icon(kind, color, size=14):
    '''用 QPainter 绘制简单矢量图标（无需资源文件）

    kind: search / download / scan / db / chevron_l / chevron_r / x
    '''

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.5)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    cx = size / 2
    if kind == 'search':
        p.drawEllipse(QRectF(2.2, 2.2, 6.8, 6.8))
        p.drawLine(QPointF(8.4, 8.4), QPointF(12.2, 12.2))
    elif kind == 'download':
        p.drawLine(QPointF(cx, 2.0), QPointF(cx, 8.8))
        p.drawLine(QPointF(cx - 3.0, 5.8), QPointF(cx, 8.8))
        p.drawLine(QPointF(cx + 3.0, 5.8), QPointF(cx, 8.8))
        p.drawLine(QPointF(2.5, 12.2), QPointF(11.5, 12.2))
    elif kind == 'scan':
        p.drawRoundedRect(QRectF(1.5, 1.5, size - 3, size - 3), 3, 3)
        p.drawLine(QPointF(3.5, 9.5), QPointF(size - 3.5, 9.5))
    elif kind == 'db':
        p.drawEllipse(QRectF(3.0, 2.0, size - 6, 3.6))
        p.drawLine(QPointF(3.0, 3.8), QPointF(3.0, size - 3.8))
        p.drawLine(QPointF(size - 3.0, 3.8), QPointF(size - 3.0, size - 3.8))
        p.drawArc(QRectF(3.0, size - 7.6, size - 6, 7.6), 2880, 2880)
    elif kind == 'chevron_l':
        p.drawLine(QPointF(8.6, 2.6), QPointF(4.4, 7.0))
        p.drawLine(QPointF(4.4, 7.0), QPointF(8.6, 11.4))
    elif kind == 'chevron_r':
        p.drawLine(QPointF(5.4, 2.6), QPointF(9.6, 7.0))
        p.drawLine(QPointF(9.6, 7.0), QPointF(5.4, 11.4))
    elif kind == 'x':
        p.drawLine(QPointF(4.0, 4.0), QPointF(10.0, 10.0))
        p.drawLine(QPointF(10.0, 4.0), QPointF(4.0, 10.0))
    elif kind == 'doc':
        p.drawRoundedRect(QRectF(3.0, 1.8, 8.0, 10.4), 1.6, 1.6)
        p.drawLine(QPointF(5.0, 4.8), QPointF(9.0, 4.8))
        p.drawLine(QPointF(5.0, 7.0), QPointF(9.0, 7.0))
        p.drawLine(QPointF(5.0, 9.2), QPointF(7.8, 9.2))
    elif kind == 'chat':
        p.drawRoundedRect(QRectF(1.8, 2.4, 10.4, 7.2), 2.4, 2.4)
        p.drawLine(QPointF(4.6, 9.4), QPointF(3.6, 12.0))
        p.drawLine(QPointF(4.6, 9.4), QPointF(7.0, 9.4))
    elif kind == 'image':
        p.drawRoundedRect(QRectF(1.8, 2.6, 10.4, 8.8), 1.8, 1.8)
        p.drawEllipse(QRectF(4.0, 4.6, 1.9, 1.9))
        p.drawLine(QPointF(3.2, 10.2), QPointF(6.4, 7.0))
        p.drawLine(QPointF(6.4, 7.0), QPointF(8.4, 9.0))
        p.drawLine(QPointF(8.4, 9.0), QPointF(10.0, 7.4))
        p.drawLine(QPointF(10.0, 7.4), QPointF(10.9, 8.3))
    elif kind == 'sparkle':
        p.drawLine(QPointF(cx, 1.6), QPointF(cx, 12.4))
        p.drawLine(QPointF(1.6, cx), QPointF(12.4, cx))
        p.drawLine(QPointF(cx - 2.6, cx - 2.6), QPointF(cx + 2.6, cx + 2.6))
        p.drawLine(QPointF(cx - 2.6, cx + 2.6), QPointF(cx + 2.6, cx - 2.6))
    elif kind == 'play':
        p.drawLine(QPointF(4.4, 2.8), QPointF(11.2, 7.0))
        p.drawLine(QPointF(11.2, 7.0), QPointF(4.4, 11.2))
        p.drawLine(QPointF(4.4, 11.2), QPointF(4.4, 2.8))
    elif kind == 'stop':
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(3.4, 3.4, 7.2, 7.2), 1.4, 1.4)
    elif kind == 'settings':
        p.drawLine(QPointF(2.0, 3.6), QPointF(12.0, 3.6))
        p.drawLine(QPointF(2.0, 8.0), QPointF(12.0, 8.0))
        p.drawLine(QPointF(2.0, 12.4), QPointF(12.0, 12.4))
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(4.3, 2.0, 3.2, 3.2))
        p.drawEllipse(QRectF(7.4, 6.4, 3.2, 3.2))
        p.drawEllipse(QRectF(3.2, 10.8, 3.2, 3.2))
    elif kind == 'refresh':
        # 循环箭头：增量更新用
        p.drawArc(QRectF(2.6, 2.6, 8.8, 8.8), 320 * 16, 260 * 16)
        p.drawLine(QPointF(11.2, 2.4), QPointF(11.2, 5.6))
        p.drawLine(QPointF(11.2, 5.6), QPointF(8.0, 5.6))
    p.end()
    return QIcon(pm)

def _cn_buttons(box):
    '''把 QDialogButtonBox 的标准按钮文字本地化为中文。'''

    for std, text in ((QDialogButtonBox.Ok, '确定'), (QDialogButtonBox.Cancel, '取消'),
                      (QDialogButtonBox.Close, '关闭'), (QDialogButtonBox.Yes, '是'),
                      (QDialogButtonBox.No, '否')):
        btn = box.button(std)
        if btn is not None:
            btn.setText(text)
            btn.setCursor(Qt.PointingHandCursor)
    ok = box.button(QDialogButtonBox.Ok)
    if ok is not None:
        ok.setDefault(True)
    return box

class InfoRow(QFrame):
    '''右侧信息行：大号统计卡（上标签下数值）或紧凑元信息行（左标签右数值）。'''

    def __init__(self, label, color, parent=None, big=False):
        super().__init__(parent)
        self.setObjectName('infoRow')
        self._big = big
        if big:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(12, 8, 12, 9)
            layout.setSpacing(1)
            lbl = QLabel(label)
            lbl.setStyleSheet(f'color: {color}; font-size: 10px; font-weight: 700; border: none; background: transparent;')
            self.val = QLabel('—')
            self.val.setStyleSheet(f'color: {C['text']}; font-size: 20px; font-weight: 800; border: none; background: transparent;')
            self.val.setWordWrap(True)
            layout.addWidget(lbl)
            layout.addWidget(self.val)
        else:
            layout = QHBoxLayout(self)
            layout.setContentsMargins(11, 6, 11, 6)
            layout.setSpacing(8)
            lbl = QLabel(label)
            lbl.setStyleSheet(f'color: {color}; font-size: 11px; font-weight: 700; border: none; background: transparent;')
            self.val = QLabel('—')
            self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.val.setWordWrap(True)
            self.val.setStyleSheet(f'color: {C['text']}; font-size: 12.5px; font-weight: 600; border: none; background: transparent;')
            layout.addWidget(lbl)
            layout.addWidget(self.val, 1)
        self.setStyleSheet(f'QFrame#infoRow {{ background-color: {C['row_alt']}; border: 1px solid {C['border_soft']}; border-radius: 11px; }}')

    def set_value(self, v):
        self.val.setText(str(v))

    def reset(self):
        self.val.setText('—')
