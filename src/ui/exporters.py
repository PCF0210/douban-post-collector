# -*- coding: utf-8 -*-
"""导出模块：把当前抓取结果写成 JSON / TXT / CSV / Markdown。

写文件动作全部放到后台线程执行：大帖（上千条评论 + OCR 文字）在主线程里拼
字符串、写盘会让窗口明显卡顿。后台线程只读取主线程事先做好的快照（纯 Python
数据），不碰任何 Qt 控件；完成后通过 _ExportRelay 的信号排队回主线程恢复界面。
"""
import csv
import io
import json
import os
import re
import threading
import traceback

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..crawl.image_downloader import extract_image_urls
from ..core.file_log import log_error, log_info
from .html_render import (_format_comment_time, _photo_url, _plain_text, _reply_ref,
                          _reply_target, _strip_html, comment_date)

# 最近一次导出目录（QSettings 键）：下次打开保存对话框时默认定位到这里
_EXPORT_DIR_KEY = 'paths/export_dir'


def _md_escape(text):
    '''最小化 Markdown 转义：避免正文行首符号被误解析为标题/列表'''

    lines = []
    for ln in (text or '').split('\n'):
        if re.match('^\\s*(#|>|[-*+] |\\d+\\. )', ln):
            ln = '\\' + ln
        lines.append(ln)
    return '\n'.join(lines)


def _write_text_file(path, text, newline=None):
    '''把文本一次性归一化后写成 utf-8-sig（带 BOM）

    utf-8-sig：Windows 记事本/写字板/Excel 才能正确识别为 UTF-8，否则默认按
    GBK 打开会整篇乱码。旧实现是「先写文件 → 再整文件读回 → 替换 → 再写回」，
    大文件会白白多读写一遍；这里直接在内存里归一化后一次写盘。
    '''

    fixed = _plain_text(text or '')
    with open(path, 'w', encoding='utf-8-sig', newline=newline) as f:
        f.write(fixed)


def _write_json_file(path, snapshot):
    '''写 JSON 导出文件（后台线程执行）'''

    data = dict(snapshot.get('result') or {})
    comments = snapshot.get('comments') or []
    scope = snapshot.get('scope') or ''
    if scope:
        data['comments'] = comments
        data['export_scope'] = scope
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _build_txt_text(topic, comments, scope, ocr):
    '''构建 TXT 导出的完整文本（纯 Python，可安全放在后台线程）'''

    ocr = ocr or {}
    topic = topic or {}
    comments = comments or []
    out = []
    out.append('============================================================\n')
    out.append('豆瓣小组帖子\n')
    out.append('============================================================\n\n')
    out.append(f'标题: {topic.get('title', 'N/A')}\n')
    if topic.get('url'):
        out.append(f'链接: {topic.get('url')}\n')
    out.append(f'创建时间: {topic.get('create_time', 'N/A')}\n')
    author = topic.get('author', {})
    out.append(f'作者: {author.get('name', 'N/A') if isinstance(author, dict) else 'N/A'}\n')
    out.append(f'回复数: {topic.get('comments_count', 'N/A')}\n')
    out.append(f'点赞数: {topic.get('like_count', 'N/A')}\n')
    group = topic.get('group', {})
    out.append(f'所在小组: {group.get('name', 'N/A') if isinstance(group, dict) else 'N/A'}\n')
    if scope:
        out.append(f'评论范围: {scope}（{len(comments)} 条）\n')
    out.append('\n------------------------------------------------------------\n')
    out.append('帖子内容:\n')
    out.append('------------------------------------------------------------\n\n')
    content = topic.get('content', '')
    body_text = _strip_html(content)
    body_images = extract_image_urls(content or '')
    if body_text:
        out.append(body_text + '\n\n')
    elif body_images:
        out.append('[正文为图片，没有文字内容]\n\n')
    else:
        out.append('[无内容]\n\n')
    if body_images:
        out.append(f'正文图片（{len(body_images)} 张）:\n')
        for u in body_images:
            out.append(f'  {u}\n')
            if u not in ocr:
                continue
            ocr_text = ocr[u]
            if ocr_text:
                out.append('  [图片 OCR 文字]:\n')
                for line in ocr_text.split('\n'):
                    out.append(f'    {line}\n')
            else:
                out.append('  [图片 OCR 文字]: (未识别到文字)\n')
        out.append('\n')
    out.append('============================================================\n')
    out.append(f'评论区 ({len(comments)} 条):\n')
    out.append('============================================================\n\n')
    for i, c in enumerate(comments):
        c_author = c.get('author', {})
        c_name = c_author.get('name', '匿名') if isinstance(c_author, dict) else '匿名'
        c_time = c.get('create_time', c.get('time', ''))
        c_content = c.get('content', c.get('text', ''))
        c_floor = c.get('floor', i + 1)
        c_ip = c.get('ip_location', '')
        ip_str = f' [{c_ip}]' if c_ip else ''
        out.append(f'[{c_floor}] {c_name}{ip_str} ({c_time}):\n')
        out.append(f'  {_strip_html(c_content)}\n')
        photos = c.get('photos') or []
        if isinstance(photos, list):
            for photo in photos:
                if not isinstance(photo, dict):
                    continue
                photo_url = _photo_url(photo)
                if not photo_url:
                    continue
                if photo_url in ocr:
                    ocr_text = ocr[photo_url]
                    if ocr_text:
                        out.append('  [图片 OCR 文字]:\n')
                        for line in ocr_text.split('\n'):
                            out.append(f'    {line}\n')
                    else:
                        out.append('  [图片 OCR 文字]: (未识别到文字)\n')
        rt = _reply_ref(c)
        if rt:
            target = _reply_target(rt) or '某条评论'
            out.append(f'  (回复 {target})\n')
            rt_text = _strip_html(rt.get('text', '') or '')
            if rt_text:
                excerpt = rt_text if len(rt_text) <= 60 else rt_text[:60] + '…'
                out.append(f'  > {excerpt}\n')
        out.append('\n')
    return ''.join(out)


def _build_csv_text(comments, ocr):
    '''构建 CSV 导出的完整文本（后台线程可用）'''

    ocr = ocr or {}
    buf = io.StringIO(newline='')
    w = csv.writer(buf)
    w.writerow(['楼层', '作者', '作者ID', '发布时间', 'IP属地', '点赞数', '回复楼层', '回复作者', '内容', '图片OCR文字'])
    for i, c in enumerate(comments or []):
        author = c.get('author', {})
        reply_to = _reply_ref(c)
        ocr_texts = []
        for photo in c.get('photos') or []:
            if not isinstance(photo, dict):
                continue
            pu = _photo_url(photo)
            if not pu:
                continue
            if pu not in ocr:
                continue
            if ocr[pu]:
                ocr_texts.append(ocr[pu].replace('\n', ' ').strip())
        content = _strip_html(c.get('content', c.get('text', '')) or '')
        w.writerow([
            c.get('floor', i + 1),
            author.get('name', '') if isinstance(author, dict) else str(author),
            author.get('uid', '') if isinstance(author, dict) else '',
            _format_comment_time(c.get('create_time', c.get('time', ''))),
            c.get('ip_location', '') or '',
            c.get('likes', 0) or 0,
            reply_to.get('floor', '') if reply_to else '',
            (reply_to.get('author', {}) or {}).get('name', '') if reply_to else '',
            content.replace('\r', ''),
            ' | '.join(ocr_texts),
        ])
    return buf.getvalue()


def _build_md_text(topic, comments, scope, ocr, include_images=True):
    '''构建 Markdown 文本（导出 / AI 分析共用）

    include_images=False 时省略图片链接（AI 无法查看图片，节省 token，
    图片中的文字仍通过 OCR 结果包含）。
    '''

    topic = topic or {}
    comments = comments or []
    ocr = ocr or {}
    author = topic.get('author', {})
    group = topic.get('group', {})
    out = []
    out.append(f'# {topic.get('title', 'N/A')}')
    out.append('')
    out.append(f'> 作者：**{author.get('name', 'N/A') if isinstance(author, dict) else 'N/A'}** · 小组：**{group.get('name', 'N/A') if isinstance(group, dict) else 'N/A'}** · 发布：{topic.get('create_time', 'N/A')} · 回复：{topic.get('comments_count', 0)} · 点赞：{topic.get('like_count', 0)}')
    out.append('')
    if topic.get('url'):
        out.append(f'> 原文：{topic.get('url')}')
        out.append('')
    if scope:
        out.append(f'> 评论范围：{scope}（{len(comments)} 条）')
        out.append('')
    out.append('## 正文')
    out.append('')
    content_html = topic.get('content', '') or ''
    body_images = extract_image_urls(content_html)
    body_text = _strip_html(content_html)
    if body_text:
        out.append(_md_escape(body_text))
    elif body_images:
        out.append('*[正文为图片，没有文字内容]*')
    else:
        out.append('[无内容]')
    out.append('')
    for u in body_images:
        if include_images:
            out.append(f'![帖子图片]({u})')
            out.append('')
        if u not in ocr:
            continue
        ocr_text = ocr[u]
        if ocr_text:
            out.append('> **OCR 识别文字**：')
            out.append('>')
            out.append('\n'.join(('> ' + ln for ln in ocr_text.split('\n'))))
            out.append('')
    out.append(f'## 评论（{len(comments)} 条）')
    out.append('')
    for i, c in enumerate(comments):
        c_author = c.get('author', {})
        c_name = c_author.get('name', '匿名') if isinstance(c_author, dict) else str(c_author)
        c_time = _format_comment_time(c.get('create_time', c.get('time', '')))
        c_floor = c.get('floor', i + 1)
        c_ip = c.get('ip_location', '') or ''
        ip_part = f' · `{c_ip}`' if c_ip else ''
        out.append(f'### #{c_floor} {c_name}')
        out.append('')
        out.append(f'`{c_time}`{ip_part}')
        out.append('')
        reply_to = _reply_ref(c)
        if reply_to:
            target = _reply_target(reply_to) or '某条评论'
            out.append(f'> ↳ 回复 {target}')
            rt_text = _strip_html(reply_to.get('text', '') or '')
            if rt_text:
                excerpt = rt_text if len(rt_text) <= 90 else rt_text[:90] + '…'
                out.append(f'> {_md_escape(excerpt)}')
            out.append('')
        content = _strip_html(c.get('content', c.get('text', '')) or '')
        out.append(_md_escape(content) if content else '[无内容]')
        out.append('')
        for photo in c.get('photos') or []:
            if not isinstance(photo, dict):
                continue
            pu = _photo_url(photo)
            if not pu:
                continue
            if include_images:
                out.append(f'![评论图片]({pu})')
                out.append('')
            if pu not in ocr:
                continue
            if ocr[pu]:
                ocr_quoted = '\n'.join(('> ' + ln for ln in ocr[pu].split('\n')))
                out.append('> **OCR 识别文字**：')
                out.append('>')
                out.append(ocr_quoted)
                out.append('')
    return '\n'.join(out)


def _write_txt_file(path, snapshot):
    snapshot = snapshot or {}
    text = _build_txt_text(snapshot.get('topic') or {},
                           snapshot.get('comments') or [],
                           snapshot.get('scope') or '',
                           snapshot.get('ocr') or {})
    _write_text_file(path, text)


def _write_csv_file(path, snapshot):
    snapshot = snapshot or {}
    text = _build_csv_text(snapshot.get('comments') or [],
                           snapshot.get('ocr') or {})
    _write_text_file(path, text, newline='')


def _write_md_file(path, snapshot):
    snapshot = snapshot or {}
    text = _build_md_text(snapshot.get('topic') or {},
                          snapshot.get('comments') or [],
                          snapshot.get('scope') or '',
                          snapshot.get('ocr') or {})
    _write_text_file(path, text)


class _ExportRelay(QObject):
    '''导出线程 → 主线程的完成信号中继

    后台线程里直接碰 Qt 控件会跨线程崩溃；这里用绑定到主窗口的 QObject 承接，
    done 信号从后台线程 emit 后自动排队到主线程执行 _on_export_done。
    '''

    done = Signal(bool, str, str)  # 是否成功, 错误信息, 保存路径


class ExportMixin:
    """ExportMixin"""

    def _export_comments(self):
        '''本次导出使用的评论列表与范围说明（未选日期时就是全部评论）'''

        scope = getattr(self, '_export_scope', None)
        if scope:
            return scope
        return list((self.result_data or {}).get('comments', []) or []), ''

    def _prepare_export_scope(self):
        '''评论列表按日期筛选时询问导出范围；返回 False 表示用户取消'''

        self._export_scope = None
        all_comments = list((self.result_data or {}).get('comments', []) or [])
        date = getattr(self, '_comment_filter_date', '') or ''
        if not date:
            return True
        picked = [c for c in all_comments if comment_date(c) == date]
        if not picked or len(picked) == len(all_comments):
            return True
        box = QMessageBox(self)
        box.setWindowTitle('导出范围')
        box.setIcon(QMessageBox.Question)
        box.setText(f'评论列表当前筛选为 {date}，共 {len(picked)} 条。')
        box.setInformativeText(f'只导出这一天的评论，还是导出全部 {len(all_comments)} 条？')
        only_btn = box.addButton(f'只导出 {date}（{len(picked)} 条）', QMessageBox.AcceptRole)
        all_btn = box.addButton(f'导出全部（{len(all_comments)} 条）', QMessageBox.RejectRole)
        cancel_btn = box.addButton('取消', QMessageBox.DestructiveRole)
        box.setDefaultButton(only_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is None or clicked is cancel_btn:
            return False
        if clicked is all_btn:
            self._export_scope = (all_comments, '')
        else:
            self._export_scope = (picked, date)
        return True

    def _export_name(self, ext):
        '''导出默认文件名；按日期导出时带上日期后缀'''

        topic_id = (self.result_data or {}).get('topic', {}).get('id', '')
        scope = getattr(self, '_export_scope', None)
        date = scope[1] if scope else ''
        suffix = f'_{date}' if date else ''
        return f'douban_topic_{topic_id}{suffix}.{ext}'

    def _export_status(self, path):
        scope = getattr(self, '_export_scope', None)
        note = f'（{scope[1]}，{len(scope[0])} 条）' if scope and scope[1] else ''
        return f'已保存{note}: {path}'

    def _finish_export(self):
        self._export_scope = None

    # ---------- 导出状态 / 后台线程 ----------

    def _export_buttons(self):
        '''四个导出按钮（界面未初始化时返回空列表）'''

        names = ('btn_export_json', 'btn_export_txt', 'btn_export_csv', 'btn_export_md')
        return [w for w in (getattr(self, name, None) for name in names) if w is not None]

    def _set_export_busy(self, busy):
        '''导出期间禁用导出按钮与菜单项；结束后按是否有结果显示是否恢复'''

        self._export_busy = bool(busy)
        has_data = bool(getattr(self, 'result_data', None))
        for btn in self._export_buttons():
            btn.setEnabled(has_data and not busy)
        for act in getattr(self, '_export_actions', None) or []:
            try:
                act.setEnabled(not busy)
            except RuntimeError:
                continue

    def _export_default_path(self, ext):
        '''默认保存路径：最近一次导出目录 + 默认文件名'''

        name = self._export_name(ext)
        try:
            last_dir = str(self._settings().value(_EXPORT_DIR_KEY, '') or '')
        except Exception:
            last_dir = ''
        if last_dir and os.path.isdir(last_dir):
            return os.path.join(last_dir, name)
        return name

    def _choose_export_path(self, ext, title, file_filter):
        '''弹出保存对话框并记住本次选择的目录；取消时返回空串'''

        path, _ = QFileDialog.getSaveFileName(self, title, self._export_default_path(ext), file_filter)
        if not path:
            return ''
        try:
            self._settings().setValue(_EXPORT_DIR_KEY, os.path.dirname(os.path.abspath(path)))
        except Exception:
            log_error('保存导出目录设置失败:\n' + traceback.format_exc())
        return path

    def _export_snapshot(self):
        '''在主线程复制一份当前结果的纯 Python 快照，后台线程只读它

        这样导出过程中即使用户开始新的抓取（result_data 被整体替换）也不会
        读到半新半旧的数据，更不会跨线程访问 Qt 对象。
        '''

        result = self.result_data or {}
        comments, scope = self._export_comments()
        return {
            'topic': dict(result.get('topic') or {}),
            'comments': list(comments or []),
            'image_urls': list(result.get('image_urls') or []),
            'result': dict(result),
            'scope': scope or '',
            'ocr': dict(self._ocr_results or {}),
        }

    def _prepare_export_file(self, ext, dialog_title, file_filter):
        '''导出通用前置：校验 → 选路径 → 取快照；返回 (path, snapshot) 或 (None, None)'''

        if getattr(self, '_export_busy', False):
            QMessageBox.information(self, '提示', '正在导出，请稍候…')
            return None, None
        if not self.result_data:
            QMessageBox.information(self, '提示', '请先爬取帖子内容后再导出。')
            return None, None
        if not self._prepare_export_scope():
            return None, None
        path = self._choose_export_path(ext, dialog_title, file_filter)
        if not path:
            return None, None
        return path, self._export_snapshot()

    def _start_export_async(self, label, path, task):
        '''在后台线程执行导出任务，完成后排队回主线程恢复界面

        task 是只读快照的普通可调用对象，不接触 Qt 控件。
        '''

        relay = _ExportRelay(self)
        relay.done.connect(self._on_export_done)
        self._export_relay = relay
        self._set_export_busy(True)
        self.statusBar.showMessage(f'正在导出 {label}…')
        log_info(f'开始导出 {label}: {path}')

        def _work():
            ok = True
            err = ''
            try:
                task()
            except PermissionError:
                ok = False
                err = f'没有权限写入该路径:\n{path}\n\n请选择其他文件夹。'
                log_error(f'导出 {label} 失败（无写入权限）: {path}')
            except Exception as exc:
                ok = False
                err = str(exc)
                log_error(f'导出 {label} 失败:\n' + traceback.format_exc())
            try:
                relay.done.emit(ok, err, path)
            except RuntimeError:
                # 窗口已销毁（用户强退）时信号接收者没了，忽略即可
                pass

        threading.Thread(target=_work, name=f'export-{label}', daemon=True).start()

    def _on_export_done(self, ok, err, path):
        '''后台导出完成（主线程）：恢复按钮、提示结果，必要时继续关闭窗口'''

        self._set_export_busy(False)
        status = self._export_status(path) if ok else ''
        self._finish_export()
        relay = getattr(self, '_export_relay', None)
        self._export_relay = None
        if relay is not None:
            relay.deleteLater()
        if ok:
            self.statusBar.showMessage(status, 5000)
            log_info(f'导出完成: {path}')
        else:
            self.statusBar.showMessage('导出失败', 5000)
            QMessageBox.warning(self, '保存失败', f'保存文件时出错:\n{err}')
        # 关闭窗口时若正好在导出，closeEvent 会把关闭挂起，这里收尾后继续
        if getattr(self, '_pending_close', False):
            QTimer.singleShot(0, self._try_finish_close)

    # ---------- 对外导出入口 ----------

    def export_json(self):
        path, snapshot = self._prepare_export_file('json', '保存 JSON', 'JSON 文件 (*.json)')
        if not path:
            return
        self._start_export_async('JSON', path, lambda: _write_json_file(path, snapshot))

    def export_txt(self):
        path, snapshot = self._prepare_export_file('txt', '保存 TXT', '文本文件 (*.txt)')
        if not path:
            return
        self._start_export_async('TXT', path, lambda: _write_txt_file(path, snapshot))

    def export_csv(self):
        '''导出评论为 CSV（utf-8-sig BOM，Excel 可直接打开）'''

        path, snapshot = self._prepare_export_file('csv', '保存 CSV', 'CSV 文件 (*.csv)')
        if not path:
            return
        self._start_export_async('CSV', path, lambda: _write_csv_file(path, snapshot))

    def export_md(self):
        '''导出为 Markdown 文档'''

        path, snapshot = self._prepare_export_file('md', '保存 Markdown', 'Markdown 文件 (*.md)')
        if not path:
            return
        self._start_export_async('Markdown', path, lambda: _write_md_file(path, snapshot))

    # ---------- 同步构建接口（AI 分析等复用；正式导出走后台线程） ----------

    def _build_md(self, include_images=True):
        '''将当前帖子构建为 Markdown 文本（导出 / AI 分析共用）

        include_images=False 时省略图片链接（AI 无法查看图片，节省 token，
        图片中的文字仍通过 OCR 结果包含）
        '''

        topic = dict((self.result_data or {}).get('topic') or {})
        comments, scope = self._export_comments()
        return _build_md_text(topic, list(comments or []), scope or '',
                              dict(self._ocr_results or {}), include_images=include_images)

    _md_escape = staticmethod(_md_escape)

    def _write_txt(self, path):
        '''同步写 TXT（保留旧接口；界面导出走 _start_export_async）'''

        _write_txt_file(path, self._export_snapshot())

    def _write_csv(self, path):
        '''同步写 CSV（保留旧接口）'''

        _write_csv_file(path, self._export_snapshot())

    def _write_md(self, path):
        '''同步写 Markdown（保留旧接口）'''

        _write_md_file(path, self._export_snapshot())

    @staticmethod
    def _normalize_export_file(path):
        '''旧接口：把已写入文件里的组合键帽降级（避免记事本把后续中文显示成方块）

        新导出流程在内存里完成归一化（见 _write_text_file），不再整文件读回；
        保留该接口仅为兼容旧调用方。
        '''

        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as fp:
                data = fp.read()
            fixed = _plain_text(data)
            if fixed != data:
                with open(path, 'w', encoding='utf-8-sig', newline='') as fp:
                    fp.write(fixed)
        except Exception:
            log_error('导出文本后处理失败:\n' + traceback.format_exc())
