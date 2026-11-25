# -*- coding: utf-8 -*-
import html as _html_mod
import json
import os
import re
import traceback
from urllib.parse import quote

from PySide6.QtCore import QPoint, QTimer, QUrl
from PySide6.QtWidgets import QMenu, QMessageBox

from ..core.file_log import log_error, log_info
from ..crawl.image_downloader import _image_filename
from ..data.db_manager import get_downloaded_image_map
from .html_render import (COMMENT_RENDER_CHUNK, _LIGHTBOX_JS, _NO_BODY_HTML, _clean_html_for_display,
                          _photo_url, annotate_body_ocr, build_base_html,
                          build_comment_card, build_comments_html, build_content_html,
                          comment_date, ocr_inject_js)
from .ui_theme import PALETTE as C, gallery_css


def _gallery_placeholder_uri():
    '''画廊图片加载失败时的内联占位图（data URI，不依赖网络）'''

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
        '<rect width="120" height="120" fill="#EEF2EE"/>'
        '<text x="60" y="64" font-size="11" fill="#93A99B" text-anchor="middle">图片加载失败</text>'
        '</svg>'
    )
    return 'data:image/svg+xml;charset=utf-8,' + quote(svg)


class RenderMixin:
    """RenderMixin"""

    def _on_finished(self, result):
        if self._scrape_stop_requested:
            return
        self.result_data = result
        topic_id = result.get('topic', {}).get('id', 0)
        if topic_id:
            self._load_ocr_from_db(topic_id)
        self._display_result(result)

    def _current_topic_id(self):
        '''当前界面展示的帖子 ID（没有结果时为 0）'''
        if not self.result_data:
            return 0
        topic = self.result_data.get('topic', {})
        return topic.get('id', 0) if isinstance(topic, dict) else 0

    def _display_result(self, result):
        '''通用结果展示（单帖/批量共用）'''

        topic = result.get('topic', {})
        comments = result.get('comments', [])
        # 成功展示结果说明本次任务已经结束，停止态标记复位
        self._stopped_state_shown = False
        try:
            self._update_info(topic)
            self._update_topic_content(topic)
            # 换帖时清掉上一帖的日期筛选；同一帖重新加载则保留用户选中的日期
            tid = topic.get('id') if isinstance(topic, dict) else None
            if tid != self._comment_filter_topic:
                self._comment_filter_topic = tid
                self._comment_filter_date = ''
            self._refresh_comment_dates(comments)
            self._update_comment_view(comments)
            self._update_gallery(result.get('image_urls', []))
        except Exception:
            # 渲染出错时不能假装「完成」：清掉结果、禁用导出，切到错误占位视图，
            # 否则用户会导出半新半旧的数据，也会以为抓取成功。
            log_error('结果渲染失败:\n' + traceback.format_exc())
            self._reset_result_ui(
                '渲染失败',
                '<div class="welcome"><span class="welcome-mark">渲染失败</span>'
                '<h1>结果渲染出错</h1><p>数据已抓取，但界面显示时出错，请重试或查看日志。</p></div>',
                '渲染失败：界面显示结果时出错',
            )
            return
        n_cmt = len(comments)
        n_img = len(result.get('image_urls', []))
        batch_active = self._is_batch_mode and not self._batch_stop_requested
        if not batch_active:
            self.btn_scrape.setEnabled(True)
            self.btn_incremental.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.progress_bar.setVisible(False)
        self.btn_export_json.setEnabled(True)
        self.btn_export_txt.setEnabled(True)
        self.btn_export_csv.setEnabled(True)
        self.btn_export_md.setEnabled(True)
        self.btn_download_images.setEnabled(n_img > 0)
        self.btn_ai.setEnabled(True)
        self._refresh_ocr_button(n_img)
        if n_img:
            # 帖子里有图就顺手预热 OCR 模型：RapidOCR 首次要读 2~4 秒模型，
            # 在用户看评论的空档预热好，点按钮就是满速。
            self._prewarm_ocr()
        if result.get('incremental'):
            self.statusBar.showMessage(
                f'增量更新完成 — 新增 {result.get("new_count", 0)} 条，'
                f'共 {n_cmt} 条评论 · {n_img} 张图片', 6000)
        else:
            self.statusBar.showMessage(f'完成 — {n_cmt} 条评论 · {n_img} 张图片', 5000)
        if self.search_input.text().strip():
            QTimer.singleShot(300, self._on_search)

    def _reset_result_ui(self, title, body_html, status):
        '''抓取失败 / 停止 / 渲染失败时的统一复位

        清掉结果与导出能力，把正文和评论视图切到占位页，避免界面停在半成品状态。
        '''

        self.result_data = None
        self._ocr_results.clear()
        self._reset_comment_dates()
        self._reset_info(title)
        self.btn_scrape.setEnabled(True)
        self.btn_incremental.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setVisible(False)
        for btn in (self.btn_export_json, self.btn_export_txt, self.btn_export_csv, self.btn_export_md):
            btn.setEnabled(False)
        self.btn_download_images.setEnabled(False)
        self.btn_ocr.setEnabled(False)
        self.btn_ai.setEnabled(False)
        self.content_text.setHtml(build_content_html(body_html), baseUrl=QUrl('https://m.douban.com/'))
        self.comment_view.setHtml(build_comments_html(None), baseUrl=QUrl('https://m.douban.com/'))
        self.comment_count_label.setText('')
        self._update_gallery([])
        self.statusBar.showMessage(status)

    def show_stopped_state(self):
        '''显示「本次抓取已停止」终态（停止请求的所有后台工作退出后调用）

        停止是异步的：stop_scrape 只发请求立刻返回，worker 的 error('已取消')
        与 thread.finished 稍后才到。这里统一复位按钮/进度条/占位视图；重复调用
        直接忽略（幂等），避免批量模式每个线程结束都刷一次界面。
        '''

        if getattr(self, '_stopped_state_shown', False):
            return
        self._stopped_state_shown = True
        self._batch_stop_requested = True
        self._is_batch_mode = False
        self._reset_result_ui(
            '已停止',
            '<div class="welcome"><span class="welcome-mark">已停止</span>'
            '<h1>本次抓取已停止</h1>'
            '<p>已中断当前任务，可以修改链接后重新开始。</p></div>',
            '本次抓取已停止',
        )

    def _gallery_local_map(self, image_urls):
        '''返回 {远程图片URL: 本地文件路径}：已下载过的图片优先用本地文件显示

        优先读数据库记录（下载时写入的 local_path，最权威）；库里没有记录时再按
        图片下载目录约定推导：images.py 把图存到 <选择的目录>/douban_images_<帖子ID>/
        下，文件名由 image_downloader._image_filename(url, index) 生成。
        '''

        local = {}
        topic_id = self._current_topic_id()
        if not topic_id or not image_urls:
            return local
        try:
            local.update(get_downloaded_image_map(topic_id) or {})
        except Exception:
            log_error('读取本地图片记录失败:\n' + traceback.format_exc())
        base = ''
        try:
            settings = self._settings()
            base = str(settings.value('paths/image_dir', '') or '')
            if not base:
                # 兼容旧版本只写了 'images/last_dir' 的设置
                base = str(settings.value('images/last_dir', '') or '')
        except Exception:
            base = ''
        if not base:
            return local
        folder = os.path.join(base, f'douban_images_{topic_id}')
        for i, url in enumerate(image_urls):
            if not url or url in local:
                continue
            try:
                path = os.path.join(folder, _image_filename(url, i + 1))
            except Exception:
                continue
            if os.path.isfile(path):
                local[url] = path
        return local

    def _update_gallery(self, image_urls):
        '''更新图片画廊视图（自适应网格，点击放大，懒加载）

        本地已下载的图片用 file:// 显示（离线可用、打开更快），远程图加
        loading="lazy" 并用内联 SVG 占位兜底；所有 URL 都转义后再拼进 HTML。
        '''

        image_urls = [u for u in (image_urls or []) if u]
        n = len(image_urls)
        self.gallery_count_label.setText(f'{n} 张图片')
        if not n:
            # 空状态不放进 .grid：.grid 是多栏瀑布流容器，display:flex 的空状态
            # 放进去会被浏览器按栏拆散（「图片」标签、标题、说明分到不同列，
            # 表现为标签飘在左边、标题跑到右边）。直接挂在 body 下即可居中。
            body_content = (
                '<div class="empty"><span class="empty-mark">图片</span>'
                '<div class="empty-title">还没有图片</div>'
                '<div class="empty-sub">抓取后，帖子与评论中的图片会显示在这里</div>'
                '</div>'
            )
        else:
            local_map = self._gallery_local_map(image_urls)
            placeholder = _gallery_placeholder_uri()
            cells = []
            for i, u in enumerate(image_urls):
                local_path = local_map.get(u) or ''
                src = QUrl.fromLocalFile(local_path).toString() if local_path else u
                safe_src = _html_mod.escape(src, quote=True)
                safe_alt = _html_mod.escape(f'帖子图片 {i + 1}', quote=True)
                cells.append(
                    f'<div class="cell"><img src="{safe_src}" alt="{safe_alt}" loading="lazy" '
                    f"onerror=\"this.onerror=null;this.src='{placeholder}'\"></div>"
                )
            cards = ''.join(cells)
            body_content = f'<div class="grid">{cards}</div>'
        css = gallery_css()
        html = (
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="referrer" content="unsafe-url">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            f'<style>{css}</style>\n</head>\n<body>\n'
            f'{body_content}\n</body>\n</html>'
        ) + _LIGHTBOX_JS
        self.gallery_view.setHtml(html, baseUrl=QUrl('https://m.douban.com/'))

    def _on_error(self, msg):
        # 停止请求下（worker 以 error('已取消') 收尾）也要进入「已停止」终态，
        # 不能只 return：否则进度条/按钮会一直停在「正在停止…」。
        if self._scrape_stop_requested:
            self.show_stopped_state()
            return
        cancelled = msg == '已取消'
        if cancelled:
            self.show_stopped_state()
            return
        log_error(msg)
        self._reset_result_ui(
            '抓取失败',
            '<div class="welcome"><span class="welcome-mark">抓取失败</span>'
            '<h1>没有抓取到内容</h1>'
            '<p>可以检查链接后重试，或换一个帖子。</p></div>',
            '抓取失败',
        )
        QMessageBox.warning(self, '爬取失败', msg)

    def _reset_info(self, title='等待抓取'):
        self.info_title.setText(title)
        for w in self.info_author, self.info_group, self.info_comments, self.info_likes, self.info_time:
            w.reset()
        self.info_link.reset()
        self.info_link.hide()

    def _update_info(self, topic):
        author = topic.get('author', {})
        self.info_title.setText(topic.get('title') or '--')
        self.info_author.set_value(author.get('name', '--') if isinstance(author, dict) else '--')
        group = topic.get('group', {})
        self.info_group.set_value(group.get('name', '--') if isinstance(group, dict) else '--')
        self.info_comments.set_value(str(topic.get('comments_count', 0)))
        self.info_likes.set_value(str(topic.get('like_count', 0)))
        ct = topic.get('create_time', '--')
        if isinstance(ct, str) and 'T' in ct:
            ct = ct.split('T')[0]
        elif isinstance(ct, str) and ' ' in ct:
            ct = ct.split(' ')[0]
        self.info_time.set_value(str(ct))
        url = str(topic.get('url') or '').strip()
        if url:
            short = _html_mod.unescape(url)
            short = re.sub(r'^https?://(www\.)?', '', short).split('?')[0]
            link = _html_mod.escape(url, quote=True)
            self.info_link.val.setText(
                f'<a href="{link}" style="color:{C["primary_text"]}; text-decoration:none;">{_html_mod.escape(short)}</a>'
            )
            self.info_link.val.setToolTip(url)
            self.info_link.show()
        else:
            self.info_link.reset()
            self.info_link.hide()

    def _update_topic_content(self, topic):
        '''更新帖子内容显示（QWebEngineView）'''

        content = topic.get('content', '')
        abstract = topic.get('abstract', '')
        if content and content.strip():
            display_html = annotate_body_ocr(_clean_html_for_display(content))
            if display_html:
                html = build_content_html(display_html)
            elif abstract:
                html = build_content_html(f'<p>[摘要] {_html_mod.escape(abstract)}</p>')
            else:
                html = build_content_html(_NO_BODY_HTML)
        elif abstract:
            html = build_content_html(f'<p>[摘要] {_html_mod.escape(abstract)}</p>')
        else:
            html = build_content_html(_NO_BODY_HTML)
        self.content_text.setHtml(html, baseUrl=QUrl('https://m.douban.com/'))
        self._switch_view(0)
        self._rail_group.button(0).setChecked(True)

    def _update_comment_view(self, comments):
        '''更新评论视图（QWebEngineView）

        评论数超过 COMMENT_RENDER_CHUNK 时分批渲染：
        先渲染首批（首屏立即可见），其余通过 JS 分批注入 DOM，避免大帖一次性渲染卡顿。
        若用户在顶部选择了某个日期，只渲染该日期的评论。
        '''

        all_comments = list(self.result_data.get('comments') or []) if comments is None else list(comments)
        visible = self._filtered_comments(all_comments)
        self._update_comment_count_label(len(visible), len(all_comments))
        self._render_gen += 1
        gen = self._render_gen
        if comments is None or len(visible) <= COMMENT_RENDER_CHUNK:
            html = build_comments_html(visible, self._ocr_results)
            self.comment_view.setHtml(html, baseUrl=QUrl('https://m.douban.com/'))
            return
        ocr = self._ocr_results
        first_html = ''.join((build_comment_card(i, c, ocr) for i, c in enumerate(visible[:COMMENT_RENDER_CHUNK])))
        body = f'<div id="dbp-comments">{first_html}</div>'
        self.comment_view.setHtml(build_base_html(body), baseUrl=QUrl('https://m.douban.com/'))
        rest = visible[COMMENT_RENDER_CHUNK:]
        self._inject_comment_chunk(gen, COMMENT_RENDER_CHUNK, rest, ocr)

    def _filtered_comments(self, comments=None):
        '''按当前日期筛选条件过滤评论（空条件返回全部）'''

        all_comments = list(self.result_data.get('comments') or []) if comments is None else list(comments)
        date = getattr(self, '_comment_filter_date', '')
        if not date:
            return all_comments
        return [c for c in all_comments if comment_date(c) == date]

    def _update_comment_count_label(self, shown, total):
        '''更新「N 条评论」标签：筛选时显示 已显示/总数 + 日期'''

        date = getattr(self, '_comment_filter_date', '')
        if date and shown != total:
            self.comment_count_label.setText(f'{shown} / {total} 条评论 · {date}')
            self.comment_count_label.setToolTip(f'当前只显示 {date} 发布的评论，共 {shown} 条；全部评论 {total} 条')
        elif date:
            self.comment_count_label.setText(f'{total} 条评论 · {date}')
            self.comment_count_label.setToolTip(f'当前只显示 {date} 发布的评论')
        else:
            self.comment_count_label.setText(f'{total} 条评论')
            self.comment_count_label.setToolTip('')

    def _refresh_comment_dates(self, comments):
        '''根据评论时间刷新日期按钮（含每天的评论数），并尽量保持原选择'''

        counts = {}
        for c in comments or []:
            day = comment_date(c)
            if day:
                counts[day] = counts.get(day, 0) + 1
        self._comment_date_counts = counts
        total = len(comments or [])
        self._comment_total = total
        wanted = getattr(self, '_comment_filter_date', '')
        if wanted and wanted not in counts:
            wanted = ''
            self._comment_filter_date = ''
        self._comment_date_counts = counts
        self._update_comment_date_button()
        self.comment_date_btn.setEnabled(bool(counts))
        self.comment_date_label.setVisible(bool(counts))

    def _update_comment_date_button(self):
        '''刷新日期按钮上的文案（全部 / 某一天 + 条数）'''

        date = getattr(self, '_comment_filter_date', '')
        counts = getattr(self, '_comment_date_counts', {})
        total = getattr(self, '_comment_total', 0)
        if date:
            self.comment_date_btn.setText(f'{date}（{counts.get(date, 0)}）  ▾')
        else:
            self.comment_date_btn.setText(f'全部日期（{total}）  ▾')

    def _show_comment_date_menu(self):
        '''弹出日期选择菜单'''

        counts = getattr(self, '_comment_date_counts', {}) or {}
        if not counts:
            return
        menu = QMenu(self)
        total = getattr(self, '_comment_total', 0)
        current = getattr(self, '_comment_filter_date', '')
        act = menu.addAction(f'全部日期（{total}）')
        act.setCheckable(True)
        act.setChecked(not current)
        act.setData('')
        menu.addSeparator()
        for day in sorted(counts, reverse=True):
            a = menu.addAction(f'{day}（{counts[day]}）')
            a.setCheckable(True)
            a.setChecked(day == current)
            a.setData(day)
        chosen = menu.exec(self.comment_date_btn.mapToGlobal(QPoint(0, self.comment_date_btn.height() + 4)))
        if chosen is None:
            return
        self._set_comment_date(chosen.data() or '')

    def _set_comment_date(self, date):
        '''应用日期筛选：重渲染评论视图，并保持搜索高亮'''

        date = date or ''
        if date == getattr(self, '_comment_filter_date', ''):
            return
        self._comment_filter_date = date
        self._update_comment_date_button()
        self._update_comment_view(None)
        if self.search_input.text().strip():
            QTimer.singleShot(300, self._on_search)

    def _reset_comment_dates(self):
        '''清空日期筛选（开始抓取 / 抓取失败时回到「全部日期」）'''

        self._comment_filter_date = ''
        self._comment_filter_topic = None
        self._comment_date_counts = {}
        self._comment_total = 0
        btn = getattr(self, 'comment_date_btn', None)
        if btn is None:
            return
        btn.setText('全部日期')
        btn.setEnabled(False)
        self.comment_date_label.setVisible(False)

    def _inject_comment_chunk(self, gen, start_idx, comments, ocr, attempt=0):
        '''将下一批评论注入页面（页面未就绪时有限次重试；代次号过期时放弃）'''

        if gen != self._render_gen:
            return
        chunk = comments[:COMMENT_RENDER_CHUNK]
        html = ''.join((build_comment_card(start_idx + j, c, ocr) for j, c in enumerate(chunk)))
        payload = json.dumps(html, ensure_ascii=False)
        js = '(function(){var el=document.getElementById("dbp-comments");if(!el){return false;}el.insertAdjacentHTML("beforeend",' + payload + ');return true;})()'
        total_done = start_idx + len(chunk)
        rest = comments[len(chunk):]
        def _on_injected(ok):
            if gen != self._render_gen:
                return
            if not ok:
                # 页面迟迟没有 #dbp-comments（加载失败/被替换）时不能无限重试
                if attempt >= 8:
                    log_error(f'评论注入失败：重试 {attempt} 次后 #dbp-comments 仍不存在，剩余 {len(comments)} 条未渲染')
                    self.statusBar.showMessage(f'评论渲染中断（已显示 {start_idx} 条），请向下滚动或重新加载', 6000)
                    return
                QTimer.singleShot(120, (lambda: self._inject_comment_chunk(gen, start_idx, comments, ocr, attempt + 1)))
                return
            if rest:
                self.statusBar.showMessage(f'评论渲染中 {total_done} 条...')
                QTimer.singleShot(10, (lambda: self._inject_comment_chunk(gen, total_done, rest, ocr)))
                return
            self.statusBar.showMessage(f'评论渲染完成（{total_done} 条）', 3000)
            if self.search_input.text().strip():
                QTimer.singleShot(200, self._on_search)

        self.comment_view.page().runJavaScript(js, _on_injected)

    def _run_ocr_inject(self, view, results):
        '''把 OCR 结果注入指定视图（只走 DOM 更新，避免整页重渲染）'''

        payload = json.dumps({u: t for (u, t) in (results or {}).items() if t}, ensure_ascii=False)
        if payload == '{}':
            return
        view.page().runJavaScript(ocr_inject_js(payload))

    def _apply_ocr_to_comment_view(self, results):
        '''OCR 完成后原地补写评论图片的识别文字（不重新渲染整页）

        只走 DOM 更新：大帖重新 setHtml 会丢掉滚动位置，而且在 OCR 线程刚结束
        的时机整页重渲染会让 QtWebEngine 原生崩溃（0x80000003）。
        '''

        self._run_ocr_inject(self.comment_view, results)

    def _apply_ocr_to_content_view(self, results=None):
        '''把已识别的文字补写到正文图片下方（正文里的图同样有 OCR 结果）'''

        self._run_ocr_inject(self.content_text, results or self._ocr_results)

    def _on_content_loaded(self, ok):
        '''正文页面加载完成后补写正文图片的 OCR 文字'''

        if ok:
            self._apply_ocr_to_content_view()
