# -*- coding: utf-8 -*-

import html as _html_mod
import os
import re
import json
import csv
import threading
import time
import traceback
from html.parser import HTMLParser
from PySide6.QtCore import (Qt, QThread, QTimer, QUrl, QSettings, QObject, Signal, QRectF, QPointF, QSize)
from PySide6.QtGui import (QAction, QColor, QGuiApplication, QKeySequence, QShortcut, QPixmap, QDesktopServices, QIcon, QPainter, QPen, QPalette)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QTableWidget, QTableWidgetItem, QProgressBar, QHeaderView, QMessageBox, QFileDialog, QStatusBar, QFrame, QAbstractItemView, QScrollArea, QDialog, QCheckBox, QFormLayout, QListWidget, QListWidgetItem, QDialogButtonBox, QStackedWidget, QButtonGroup, QMenu)
from ..crawl.scraper import ScraperWorker
from ..crawl.image_downloader import ImageDownloadWorker, extract_image_urls
from ..ocr.ocr_extractor import OCRWorker, OCRExtractor, tidy_text
from ..ai.ai_analyzer import AIAnalysisDialog
from ..core.config import OCR_LANGUAGE
from ..core.paths import asset
from ..data.db_manager import (get_topic, get_topics, get_topics_count, get_comment_dicts, get_images, get_stats, delete_topic as db_delete_topic, save_ocr_results_batch, get_ocr_map, get_downloaded_image_map)
from ..core.file_log import (log_info, log_error, LOG_DIR)
from ..core.tls import make_session
from .ui_theme import PALETTE as C, stylesheet, content_css, comments_css, gallery_css

from .dialogs import DialogMixin
from .exporters import ExportMixin
from .html_render import (COMMENT_RENDER_CHUNK, _NO_BODY_HTML, _clean_html_for_display,
                          _format_comment_time, _photo_url, _plain_text, _reply_ref,
                          _reply_target, _strip_html, annotate_body_ocr, build_base_html,
                          build_comment_card, build_comments_html, build_content_html,
                          ocr_inject_js)
from .images import ImageMixin
from .ocr_control import OcrControlMixin
from .renderer import RenderMixin
from .scraping import ScrapeMixin
from .search import SearchMixin
from .widgets import InfoRow, _OcrProbeResult, _OcrSignalSink, _ProxyTestResult, _cn_buttons, _make_icon


class DoubanScraperWindow(ScrapeMixin, RenderMixin, OcrControlMixin, ImageMixin,
                          ExportMixin, SearchMixin, DialogMixin, QMainWindow):
    '''主窗口：负责界面搭建、菜单与少量跨模块编排'''

    def __init__(self):
        super().__init__()
        self.worker = None
        self.worker_thread = None
        self.result_data = None
        self.image_worker = None
        self.image_worker_thread = None
        self.ocr_worker = None
        self.ocr_worker_thread = None
        self._ocr_results = {}
        self._batch_queue = []
        self._batch_current_index = 0
        self._batch_results = []
        self._batch_errors = {}
        self._batch_stop_requested = False
        self._is_batch_mode = False
        self._batch_incremental = False
        self._scrape_stop_requested = False
        self._stopped_state_shown = False
        self._stop_clears_result = False
        self._pending_close = False
        self._closing_ready = False
        self._export_busy = False
        self._export_relay = None
        self._export_actions = []
        self._last_batch_result = None
        # 批量模式改用成员单次定时器：QTimer.singleShot 无法取消，停止/关闭后
        # 旧定时器仍会触发 _start_batch_next，容易和下一次任务串台。
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.timeout.connect(self._start_batch_next)
        self._search_matches = 0
        self._search_current = 0
        self._search_view_counts = {'content': 0, 'comments': 0}
        self._search_gen = 0
        self._render_gen = 0
        self._comment_filter_date = ''
        self._comment_filter_topic = None
        self._comment_date_counts = {}
        self._ocr_topic_id = 0
        self._ocr_job_id = 0
        self._ocr_sink = None
        self._ocr_cancel_requested = False
        self._ocr_running = False
        self._ocr_probe_running = False
        self._ocr_probe_attempts = 0
        self._ocr_warmed = False
        self._ocr_probe_relay = _OcrProbeResult(self)
        self._ocr_probe_relay.done.connect(self._on_ocr_probe_done)
        self._init_ui()
        self._init_menu()
        self._init_shortcuts()
        # 恢复上次的窗口位置/大小/视图；没有历史记录时才居中
        if not self._restore_window_state():
            self._center_window()
        # OCR 引擎探测放到后台线程：tesseract --version 偶尔会因杀毒扫描/系统繁忙
        # 卡住数秒，若在 UI 线程同步探测，整个界面（含按钮）都会跟着卡死。
        self._start_ocr_probe()

    def _init_ui(self):
        self.setWindowTitle('豆瓣小组帖子采集器')
        icon_path = asset('logo.ico')
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setMinimumSize(1080, 700)
        self.resize(1360, 860)
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # ===== 左侧导航 =====
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(240)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(18, 22, 18, 18)
        sl.setSpacing(4)

        brand = QHBoxLayout()
        brand.setSpacing(12)
        mark = QLabel()
        mark.setObjectName('brandLogo')
        mark.setFixedSize(40, 40)
        mark.setAlignment(Qt.AlignCenter)
        logo_path = asset('logo_256.png')
        if os.path.isfile(logo_path):
            dpr = self.devicePixelRatioF() or 1.0
            side = int(round(40 * dpr))
            pm = QPixmap(logo_path).scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            pm.setDevicePixelRatio(dpr)
            mark.setPixmap(pm)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        brand_name = QLabel('豆瓣小组')
        brand_name.setObjectName('brandName')
        brand_sub = QLabel('帖子采集器')
        brand_sub.setObjectName('brandSub')
        brand_text.addWidget(brand_name)
        brand_text.addWidget(brand_sub)
        brand.addWidget(mark)
        brand.addLayout(brand_text)
        brand.addStretch()
        sl.addLayout(brand)
        sl.addSpacing(22)

        nav_label = QLabel('浏览')
        nav_label.setObjectName('navSection')
        sl.addWidget(nav_label)
        sl.addSpacing(4)

        self.view_stack = QStackedWidget()
        self._rail_group = QButtonGroup(self)
        self._rail_group.setExclusive(True)
        icon_gray = C['text3']
        nav_items = [('doc', '帖子内容', '阅读正文'), ('chat', '评论', '查看评论'), ('image', '图片', '图片画廊')]
        for i, (kind, text, tip) in enumerate(nav_items):
            btn = self._make_nav_button(kind, text, tip)
            btn.setCheckable(True)
            btn.clicked.connect((lambda _=False, idx=i: self._switch_view(idx)))
            btn.toggled.connect((lambda on, b=btn, k=kind: b.setIcon(_make_icon(k, C['primary'] if on else icon_gray, 18))))
            self._rail_group.addButton(btn, i)
            sl.addWidget(btn)
        self._rail_group.button(0).setChecked(True)

        sl.addSpacing(16)
        tools_label = QLabel('工具')
        tools_label.setObjectName('navSection')
        sl.addWidget(tools_label)
        sl.addSpacing(4)
        self.btn_browse_db = self._make_nav_button('db', '数据库', '浏览已保存的帖子')
        self.btn_browse_db.clicked.connect(self._show_db_browser)
        sl.addWidget(self.btn_browse_db)
        sl.addStretch()
        self.btn_settings = QPushButton('设置与更多')
        self.btn_settings.setObjectName('settingsBtn')
        self.btn_settings.setIcon(_make_icon('settings', icon_gray, 18))
        self.btn_settings.setIconSize(QSize(18, 18))
        self.btn_settings.setFixedHeight(40)
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        sl.addWidget(self.btn_settings)
        sl.addSpacing(8)
        footer = QLabel('本地 SQLite · v1.0')
        footer.setObjectName('sidebarFooter')
        sl.addWidget(footer)
        shell.addWidget(sidebar)

        # ===== 主列 =====
        main_col = QWidget()
        main_col.setObjectName('mainCol')
        ml = QVBoxLayout(main_col)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName('topBar')
        tb = QHBoxLayout(topbar)
        tb.setContentsMargins(22, 18, 22, 18)
        tb.setSpacing(12)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        page_title = QLabel('抓取任务')
        page_title.setObjectName('pageTitle')
        page_sub = QLabel('粘贴链接，支持多行批量')
        page_sub.setObjectName('pageSub')
        title_box.addWidget(page_title)
        title_box.addWidget(page_sub)
        tb.addLayout(title_box)
        tb.addSpacing(8)
        self.url_input = QTextEdit()
        self.url_input.setObjectName('urlInput')
        self.url_input.setPlaceholderText('粘贴豆瓣链接 / 帖子 ID（可多行）')
        self.url_input.setFixedHeight(46)
        self.url_input.setAcceptRichText(False)
        pal = self.url_input.palette()
        pal.setColor(QPalette.PlaceholderText, QColor(C['text3']))
        pal.setColor(QPalette.Text, QColor(C['text']))
        self.url_input.setPalette(pal)
        tb.addWidget(self.url_input, stretch=1)
        self.btn_scrape = QPushButton('开始爬取')
        self.btn_scrape.setObjectName('btnScrape')
        self.btn_scrape.setIcon(_make_icon('play', '#FFFFFF', 15))
        self.btn_scrape.setIconSize(QSize(15, 15))
        self.btn_scrape.clicked.connect(self.start_scrape)
        self.btn_scrape.setCursor(Qt.PointingHandCursor)
        self.btn_scrape.setFixedHeight(44)
        self.btn_scrape.setMinimumWidth(122)
        tb.addWidget(self.btn_scrape)
        self.btn_stop = QPushButton('停止')
        self.btn_stop.setObjectName('btnStop')
        self.btn_stop.setIcon(_make_icon('stop', C['red_h'], 14))
        self.btn_stop.setIconSize(QSize(14, 14))
        self.btn_stop.clicked.connect(self.stop_scrape)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        self.btn_stop.setFixedHeight(44)
        self.btn_stop.setMinimumWidth(86)
        tb.addWidget(self.btn_stop)
        ml.addWidget(topbar)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName('progressBar')
        self.progress_bar.setVisible(False)
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setTextVisible(False)
        ml.addWidget(self.progress_bar)

        body = QWidget()
        body.setObjectName('bodyArea')
        bl = QHBoxLayout(body)
        bl.setContentsMargins(22, 20, 22, 18)
        bl.setSpacing(18)

        content_area = QFrame()
        content_area.setObjectName('contentArea')
        ca = QVBoxLayout(content_area)
        ca.setContentsMargins(20, 16, 20, 18)
        ca.setSpacing(12)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.view_title = QLabel('帖子内容')
        self.view_title.setObjectName('viewTitle')
        self.view_hint = QLabel('清洗后的正文')
        self.view_hint.setObjectName('viewHint')
        header.addWidget(self.view_title)
        header.addWidget(self.view_hint)
        header.addStretch()
        ca.addLayout(header)

        self.content_text = QWebEngineView()
        self.content_text.setObjectName('contentText')
        self.content_text.loadFinished.connect(self._on_content_loaded)
        self.view_stack.addWidget(self.content_text)

        comments_page = QWidget()
        ct = QVBoxLayout(comments_page)
        ct.setContentsMargins(0, 0, 0, 0)
        ct.setSpacing(6)
        comment_head = QHBoxLayout()
        comment_head.setContentsMargins(0, 0, 0, 0)
        comment_head.setSpacing(8)
        self.comment_count_label = QLabel('')
        self.comment_count_label.setObjectName('commentCount')
        comment_head.addWidget(self.comment_count_label)
        comment_head.addStretch()
        self.comment_date_label = QLabel('按日期')
        self.comment_date_label.setObjectName('viewHint')
        comment_head.addWidget(self.comment_date_label)
        self.comment_date_btn = QPushButton('全部日期')
        self.comment_date_btn.setObjectName('commentDate')
        self.comment_date_btn.setCursor(Qt.PointingHandCursor)
        self.comment_date_btn.setEnabled(False)
        self.comment_date_btn.setToolTip('只看某一天发布的评论')
        self.comment_date_btn.clicked.connect(self._show_comment_date_menu)
        comment_head.addWidget(self.comment_date_btn)
        ct.addLayout(comment_head)
        self.comment_view = QWebEngineView()
        self.comment_view.setObjectName('commentView')
        ct.addWidget(self.comment_view, stretch=1)
        self.view_stack.addWidget(comments_page)

        gallery_page = QWidget()
        gp = QVBoxLayout(gallery_page)
        gp.setContentsMargins(0, 0, 0, 0)
        gp.setSpacing(6)
        self.gallery_count_label = QLabel('')
        self.gallery_count_label.setObjectName('commentCount')
        gp.addWidget(self.gallery_count_label)
        self.gallery_view = QWebEngineView()
        self.gallery_view.setObjectName('galleryView')
        gp.addWidget(self.gallery_view, stretch=1)
        self.view_stack.addWidget(gallery_page)
        ca.addWidget(self.view_stack, stretch=1)
        bl.addWidget(content_area, stretch=1)

        # 初始空状态：三个视图都给出可读的引导，而不是白板
        self.content_text.setHtml(build_content_html(
            '<div class="welcome">'
            '<span class="welcome-mark">豆瓣小组 · 帖子采集</span>'
            '<h1>粘贴链接，开始一次采集</h1>'
            '<p>支持单帖链接、帖子 ID 与多行批量。抓取完成后，正文会显示在这里。</p>'
            '<div class="welcome-tips"><span>Ctrl + Enter 开始</span><span>Esc 停止</span></div>'
            '</div>'
        ), baseUrl=QUrl('https://m.douban.com/'))
        self.comment_view.setHtml(build_comments_html(None), baseUrl=QUrl('https://m.douban.com/'))
        self.comment_count_label.setText('0 条评论')
        self._update_gallery([])

        # ===== 右侧上下文面板 =====
        ctx = QWidget()
        ctx.setObjectName('ctxPanel')
        ctx.setMinimumWidth(300)
        cp = QVBoxLayout(ctx)
        cp.setContentsMargins(0, 0, 2, 0)
        cp.setSpacing(10)

        card, cv = self._make_panel_card('帖子概览')
        self.info_title = QLabel('--')
        self.info_title.setObjectName('ctxTitle')
        self.info_title.setWordWrap(True)
        cv.addWidget(self.info_title)
        self.info_comments = InfoRow('回复', C['blue'], big=True)
        self.info_likes = InfoRow('点赞', C['orange'], big=True)
        stat_row = QHBoxLayout()
        stat_row.setContentsMargins(0, 0, 0, 0)
        stat_row.setSpacing(8)
        stat_row.addWidget(self.info_comments, stretch=1)
        stat_row.addWidget(self.info_likes, stretch=1)
        cv.addLayout(stat_row)
        self.info_author = InfoRow('作者', C['green'])
        self.info_group = InfoRow('小组', C['purple'])
        self.info_time = InfoRow('发布', C['text2'])
        self.info_link = InfoRow('链接', C['primary'])
        self.info_link.val.setTextFormat(Qt.RichText)
        self.info_link.val.setOpenExternalLinks(True)
        self.info_link.val.setCursor(Qt.PointingHandCursor)
        self.info_link.hide()
        meta_box = QVBoxLayout()
        meta_box.setContentsMargins(0, 0, 0, 0)
        meta_box.setSpacing(4)
        for w in self.info_author, self.info_group, self.info_time, self.info_link:
            meta_box.addWidget(w)
        cv.addLayout(meta_box)
        cp.addWidget(card)
        self._reset_info()

        card, cv = self._make_panel_card('快捷操作')
        self.btn_incremental = QPushButton('增量更新（只拉新评论）')
        self.btn_incremental.setObjectName('btnAction')
        self.btn_incremental.setIcon(_make_icon('refresh', icon_gray))
        self.btn_incremental.clicked.connect(self.start_incremental)
        self.btn_incremental.setCursor(Qt.PointingHandCursor)
        self.btn_incremental.setToolTip(
            '只抓取上次入库之后的新评论，并更新帖子正文与图片；\n'
            '本地没有该帖时会自动改为完整抓取'
        )
        cv.addWidget(self.btn_incremental)
        self.btn_download_images = QPushButton('下载图片')
        self.btn_download_images.setObjectName('btnAction')
        self.btn_download_images.setIcon(_make_icon('download', icon_gray))
        self.btn_download_images.clicked.connect(self.download_images)
        self.btn_download_images.setEnabled(False)
        self.btn_download_images.setCursor(Qt.PointingHandCursor)
        cv.addWidget(self.btn_download_images)
        self.btn_ocr = QPushButton('OCR 识别图片文字')
        self.btn_ocr.setObjectName('btnAction')
        self.btn_ocr.setIcon(_make_icon('scan', icon_gray))
        self.btn_ocr.clicked.connect(self.run_ocr)
        self.btn_ocr.setEnabled(False)
        self.btn_ocr.setCursor(Qt.PointingHandCursor)
        cv.addWidget(self.btn_ocr)
        self.btn_ai = QPushButton('AI 分析总结')
        self.btn_ai.setObjectName('btnAIPrimary')
        self.btn_ai.setIcon(_make_icon('sparkle', C['primary_text']))
        self.btn_ai.clicked.connect(self.run_ai_analysis)
        self.btn_ai.setEnabled(False)
        self.btn_ai.setCursor(Qt.PointingHandCursor)
        self.btn_ai.setToolTip('将帖子内容（Markdown）发送给 DeepSeek AI 总结分析')
        cv.addWidget(self.btn_ai)
        cp.addWidget(card)

        card, cv = self._make_panel_card('导出')
        self.btn_export_json = QPushButton('JSON')
        self.btn_export_json.setObjectName('btnMini')
        self.btn_export_json.clicked.connect(self.export_json)
        self.btn_export_json.setEnabled(False)
        self.btn_export_json.setCursor(Qt.PointingHandCursor)
        self.btn_export_txt = QPushButton('TXT')
        self.btn_export_txt.setObjectName('btnMini')
        self.btn_export_txt.clicked.connect(self.export_txt)
        self.btn_export_txt.setEnabled(False)
        self.btn_export_txt.setCursor(Qt.PointingHandCursor)
        self.btn_export_csv = QPushButton('CSV')
        self.btn_export_csv.setObjectName('btnMini')
        self.btn_export_csv.clicked.connect(self.export_csv)
        self.btn_export_csv.setEnabled(False)
        self.btn_export_csv.setCursor(Qt.PointingHandCursor)
        self.btn_export_md = QPushButton('MD')
        self.btn_export_md.setObjectName('btnMini')
        self.btn_export_md.clicked.connect(self.export_md)
        self.btn_export_md.setEnabled(False)
        self.btn_export_md.setCursor(Qt.PointingHandCursor)
        self.btn_export_json.setToolTip('导出完整 JSON（含原始字段）')
        self.btn_export_txt.setToolTip('导出纯文本（正文 + 评论）')
        self.btn_export_csv.setToolTip('导出评论 CSV（Excel 可直接打开）')
        self.btn_export_md.setToolTip('导出 Markdown（含正文与评论引用）')
        export_row = QHBoxLayout()
        export_row.setContentsMargins(0, 0, 0, 0)
        export_row.setSpacing(6)
        for _btn in (self.btn_export_json, self.btn_export_txt, self.btn_export_csv, self.btn_export_md):
            export_row.addWidget(_btn, 1)
        cv.addLayout(export_row)
        cp.addWidget(card)

        card = QFrame()
        card.setObjectName('panelCard')
        cv = QVBoxLayout(card)
        cv.setContentsMargins(15, 13, 15, 13)
        cv.setSpacing(9)
        search_head = QHBoxLayout()
        search_head.setSpacing(8)
        search_title = QLabel('全文搜索')
        search_title.setObjectName('cardTitle')
        self.search_info = QLabel('')
        self.search_info.setObjectName('searchInfo')
        search_head.addWidget(search_title)
        search_head.addStretch()
        search_head.addWidget(self.search_info)
        cv.addLayout(search_head)
        self.search_input = QLineEdit()
        self.search_input.setObjectName('searchInput')
        self.search_input.setPlaceholderText('搜索关键词…')
        self.search_input.addAction(_make_icon('search', C['text3']), QLineEdit.LeadingPosition)
        self.search_input.returnPressed.connect(self._on_search)
        spal = self.search_input.palette()
        spal.setColor(QPalette.PlaceholderText, QColor(C['text3']))
        self.search_input.setPalette(spal)
        self.btn_search_prev = QPushButton()
        self.btn_search_prev.setObjectName('btnGhost')
        self.btn_search_prev.setIcon(_make_icon('chevron_l', C['text2']))
        self.btn_search_prev.setFixedSize(30, 28)
        self.btn_search_prev.setToolTip('上一个匹配')
        self.btn_search_prev.setCursor(Qt.PointingHandCursor)
        self.btn_search_prev.setEnabled(False)
        self.btn_search_prev.clicked.connect(self._on_search_prev)
        self.btn_search_next = QPushButton()
        self.btn_search_next.setObjectName('btnGhost')
        self.btn_search_next.setIcon(_make_icon('chevron_r', C['text2']))
        self.btn_search_next.setFixedSize(30, 28)
        self.btn_search_next.setToolTip('下一个匹配')
        self.btn_search_next.setCursor(Qt.PointingHandCursor)
        self.btn_search_next.setEnabled(False)
        self.btn_search_next.clicked.connect(self._on_search_next)
        self.btn_search_clear = QPushButton()
        self.btn_search_clear.setObjectName('btnGhost')
        self.btn_search_clear.setIcon(_make_icon('x', C['text2']))
        self.btn_search_clear.setFixedSize(30, 28)
        self.btn_search_clear.setToolTip('清除搜索')
        self.btn_search_clear.setCursor(Qt.PointingHandCursor)
        self.btn_search_clear.setEnabled(False)
        self.btn_search_clear.clicked.connect(self._on_search_clear)
        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(self.btn_search_prev)
        search_row.addWidget(self.btn_search_next)
        search_row.addWidget(self.btn_search_clear)
        cv.addLayout(search_row)
        cp.addWidget(card)
        cp.addStretch()

        ctx_scroll = QScrollArea()
        ctx_scroll.setObjectName('ctxScroll')
        ctx_scroll.setWidget(ctx)
        ctx_scroll.setWidgetResizable(True)
        ctx_scroll.setFixedWidth(344)
        ctx_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ctx_scroll.setFrameShape(QFrame.NoFrame)
        bl.addWidget(ctx_scroll)
        ml.addWidget(body, stretch=1)
        shell.addWidget(main_col, stretch=1)

        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.setObjectName('statusBar')
        self.statusBar.setSizeGripEnabled(False)
        self.statusBar.showMessage('就绪')
        self.status_hint = QLabel('Ctrl+Enter 开始  ·  Esc 停止')
        self.status_hint.setObjectName('statusHint')
        self.statusBar.addPermanentWidget(self.status_hint)
        self._apply_styles()

    def _make_nav_button(self, kind, text, tip=''):
        btn = QPushButton(text)
        btn.setObjectName('navBtn')
        btn.setIcon(_make_icon(kind, '#93A99B', 18))
        btn.setIconSize(QSize(18, 18))
        btn.setFixedHeight(42)
        btn.setCursor(Qt.PointingHandCursor)
        if tip:
            btn.setToolTip(tip)
        return btn

    @staticmethod

    def _make_panel_card(title, subtitle=''):
        card = QFrame()
        card.setObjectName('panelCard')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(15, 13, 15, 13)
        layout.setSpacing(9)
        head = QLabel(title)
        head.setObjectName('cardTitle')
        layout.addWidget(head)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName('cardSub')
            sub.setWordWrap(True)
            layout.addWidget(sub)
        return card, layout

    def _switch_view(self, idx):
        '''切换主视图（内容 / 评论 / 图片）'''

        self.view_stack.setCurrentIndex(idx)
        btn = self._rail_group.button(idx) if hasattr(self, '_rail_group') else None
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        titles = [('帖子内容', '清洗后的正文'), ('评论', '按楼层排序'), ('图片画廊', '点击图片放大')]
        if 0 <= idx < len(titles):
            self.view_title.setText(titles[idx][0])
            self.view_hint.setText(titles[idx][1])

    def _apply_styles(self):
        self.setStyleSheet(stylesheet())

    def _init_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu('文件(&F)')
        act_export_json = QAction('导出 JSON...', self)
        act_export_json.triggered.connect(self.export_json)
        file_menu.addAction(act_export_json)
        act_export_txt = QAction('导出 TXT...', self)
        act_export_txt.triggered.connect(self.export_txt)
        file_menu.addAction(act_export_txt)
        act_export_csv = QAction('导出 CSV...', self)
        act_export_csv.triggered.connect(self.export_csv)
        file_menu.addAction(act_export_csv)
        act_export_md = QAction('导出 Markdown...', self)
        act_export_md.triggered.connect(self.export_md)
        file_menu.addAction(act_export_md)
        # 导出进行中要能统一禁用这 4 个菜单项（与按钮同步 busy 状态）
        self._export_actions = [act_export_json, act_export_txt, act_export_csv, act_export_md]
        file_menu.addSeparator()
        act_download_images = QAction('下载图片...', self)
        act_download_images.triggered.connect(self.download_images)
        file_menu.addAction(act_download_images)
        act_ocr = QAction('OCR 识别图片文字...', self)
        act_ocr.triggered.connect(self.run_ocr)
        file_menu.addAction(act_ocr)
        act_ai = QAction('AI 分析总结...', self)
        act_ai.triggered.connect(self.run_ai_analysis)
        file_menu.addAction(act_ai)
        file_menu.addSeparator()
        act_quit = QAction('退出', self)
        act_quit.setShortcut('Ctrl+Q')
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)
        settings_menu = menubar.addMenu('设置(&S)')
        act_proxy = QAction('代理设置...', self)
        act_proxy.triggered.connect(self._show_proxy_settings)
        settings_menu.addAction(act_proxy)
        act_ocr_lang = QAction('OCR 识别语言...', self)
        act_ocr_lang.triggered.connect(self._show_ocr_language_settings)
        settings_menu.addAction(act_ocr_lang)
        help_menu = menubar.addMenu('帮助(&H)')
        act_open_log = QAction('打开日志文件', self)
        act_open_log.triggered.connect(self._open_log_file)
        help_menu.addAction(act_open_log)
        act_ocr_status = QAction('OCR 引擎状态...', self)
        act_ocr_status.triggered.connect(self._show_ocr_status)
        help_menu.addAction(act_ocr_status)
        act_about = QAction('关于', self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

        # 隐藏原生菜单栏，入口收进左下角「设置与更多」
        self.menuBar().hide()
        self._more_menu = QMenu(self)
        self._more_menu.addAction('代理设置...', self._show_proxy_settings)
        self._more_menu.addAction('OCR 识别语言...', self._show_ocr_language_settings)
        self._more_menu.addSeparator()
        self._more_menu.addAction('打开日志文件', self._open_log_file)
        self._more_menu.addAction('OCR 引擎状态...', self._show_ocr_status)
        self._more_menu.addSeparator()
        self._more_menu.addAction('关于', self._show_about)
        self._more_menu.addAction('退出', self.close)
        self.btn_settings.clicked.connect(self._show_more_menu)

    def _show_more_menu(self):
        pos = self.btn_settings.mapToGlobal(self.btn_settings.rect().topLeft())
        self._more_menu.exec(pos)

    def _init_shortcuts(self):
        QShortcut(QKeySequence('Ctrl+Return'), self, activated=self.start_scrape)
        QShortcut(QKeySequence('Esc'), self, activated=self.stop_scrape)

    def _center_window(self):
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        self.move((geo.width() - self.width()) // 2, (geo.height() - self.height()) // 2)

    def run_ai_analysis(self):
        '''打开 AI 分析对话框：将当前帖子（Markdown）发送给 DeepSeek 总结'''

        if not self.result_data:
            QMessageBox.information(self, '提示', '请先爬取帖子内容后再进行 AI 分析。')
            return
        md_text = self._build_md(include_images=False)
        proxy_enabled, proxy_url = self._read_proxy()
        topic = (self.result_data or {}).get('topic') or {}
        dlg = AIAnalysisDialog(md_text, proxy=proxy_url if proxy_enabled else None,
                               parent=self, title=topic.get('title') or '')
        dlg.exec()
        # exec() 返回后对话框已关闭，但 Python 引用若继续持有会拖住整个对象树
        # （含富文本内容），显式交给 Qt 回收。
        dlg.deleteLater()

    # ---------- 窗口状态持久化 ----------

    def _restore_window_state(self):
        '''恢复上次关闭时的窗口位置/大小与视图选择

        返回 True 表示成功恢复了窗口几何信息（此时不再做居中）。
        '''

        try:
            settings = self._settings()
        except Exception:
            log_error('读取窗口设置失败:\n' + traceback.format_exc())
            return False
        restored = False
        geo = settings.value('window/geometry')
        if geo is not None:
            try:
                restored = bool(self.restoreGeometry(geo))
            except Exception:
                log_error('恢复窗口位置失败:\n' + traceback.format_exc())
        state = settings.value('window/state')
        if state is not None:
            try:
                self.restoreState(state)
            except Exception:
                log_error('恢复窗口布局失败:\n' + traceback.format_exc())
        try:
            view = settings.value('window/view_index')
            if view is not None:
                idx = int(view)
                if 0 <= idx < self.view_stack.count():
                    self._switch_view(idx)
        except (TypeError, ValueError):
            pass
        return restored

    def _save_window_state(self):
        '''保存窗口位置/大小与当前视图（关闭窗口时调用）'''

        try:
            settings = self._settings()
            settings.setValue('window/geometry', self.saveGeometry())
            settings.setValue('window/state', self.saveState())
            settings.setValue('window/view_index', int(self.view_stack.currentIndex()))
        except Exception:
            log_error('保存窗口状态失败:\n' + traceback.format_exc())

    # ---------- 关闭窗口 ----------

    def closeEvent(self, event):
        '''关闭窗口：先异步请求停止，等所有后台线程退出后再真正关闭

        旧实现同步 _stop_workers_and_wait(5000, force=True)（超时还会 terminate
        线程）：关闭大批量任务时窗口卡住，terminate 掉有事件循环/GUI 资源的线程
        还可能直接崩溃。现在改为：请求停止 → 挂起关闭（event.ignore）→ 每个运行
        中的线程 finished 后续关 → 全部退出后用 QTimer.singleShot 重新 close。
        '''

        if getattr(self, '_closing_ready', False):
            self._save_window_state()
            event.accept()
            return
        self._pending_close = True
        threads = self._running_worker_threads()
        if threads:
            self._request_workers_stop()
            # 关闭时只停止、不刷新「已停止」界面，避免白白更新一堆控件
            self._stop_clears_result = False
            log_error(f'关闭窗口：等待 {len(threads)} 个后台线程退出')
            for th in threads:
                try:
                    th.finished.connect(self._try_finish_close, Qt.QueuedConnection)
                except RuntimeError:
                    continue
            self.statusBar.showMessage('正在停止后台任务…')
            event.ignore()
            return
        if getattr(self, '_export_busy', False):
            # 导出线程是一次性写盘任务，写完后 _on_export_done 会续关
            self.statusBar.showMessage('正在导出，完成后自动关闭…')
            event.ignore()
            return
        self._closing_ready = True
        self._save_window_state()
        event.accept()

    def _try_finish_close(self):
        '''后台线程/导出任务退出后继续关闭窗口（可重复触发，幂等）'''

        if not getattr(self, '_pending_close', False) or getattr(self, '_closing_ready', False):
            return
        if self._running_worker_threads() or getattr(self, '_export_busy', False):
            return
        self._closing_ready = True
        self._save_window_state()
        # 在事件循环下一拍重新 close，避免在 finished 信号处理里直接销毁窗口
        QTimer.singleShot(0, self.close)

