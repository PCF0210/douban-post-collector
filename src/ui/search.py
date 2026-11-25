# -*- coding: utf-8 -*-
import json

from PySide6.QtCore import QTimer

from .ui_theme import PALETTE as C


class SearchMixin:
    """SearchMixin"""

    def _search_js(self, keyword, action='highlight', index=0):
        '''生成搜索高亮的 JavaScript 代码'''

        if action == 'highlight':
            escaped = json.dumps(keyword)
            regex_chars = set('\\^$.*+?()[]{}|')
            regex_literal = ''.join(('\\' + ch if ch in regex_chars else ch for ch in keyword))
            escaped_regex = json.dumps(regex_literal)
            return f"\n(function() {{\n    // 清除旧高亮\n    document.querySelectorAll('mark.dbp-hl, mark.dbp-hl-active').forEach(function(m) {{\n        m.replaceWith(m.textContent);\n    }});\n\n    var keyword = {escaped};\n    if (!keyword) return JSON.stringify({{total: 0}});\n\n    function highlightText(root) {{\n        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);\n        var nodes = [];\n        while (walker.nextNode()) {{\n            var el = walker.currentNode.parentElement;\n            if (el && (el.closest('script') || el.closest('style') || el.closest('mark'))) continue;\n            nodes.push(walker.currentNode);\n        }}\n        nodes.forEach(function(n) {{\n            var parent = n.parentElement;\n            var text = n.textContent;\n            var regex = new RegExp({escaped_regex}, 'gi');\n            var match, last = 0;\n            var frag = document.createDocumentFragment();\n            while ((match = regex.exec(text)) !== null) {{\n                if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));\n                var mark = document.createElement('mark');\n                mark.className = 'dbp-hl';\n                mark.textContent = match[0];\n                frag.appendChild(mark);\n                last = regex.lastIndex;\n            }}\n            if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));\n            if (last > 0 && parent) parent.replaceChild(frag, n);\n        }});\n    }}\n    highlightText(document.body);\n\n    var marks = document.querySelectorAll('mark.dbp-hl');\n    marks.forEach(function(m, i) {{ m.setAttribute('data-idx', i); }});\n    return JSON.stringify({{total: marks.length}});\n}})();\n"
        if action == 'focus':
            return f"\n(function() {{\n    var marks = document.querySelectorAll('mark.dbp-hl, mark.dbp-hl-active');\n    marks.forEach(function(m) {{ m.className = 'dbp-hl'; }});\n    var i = {int(index)};\n    if (i < 0 || i >= marks.length) return JSON.stringify({{current: 0, total: marks.length}});\n    marks[i].className = 'dbp-hl-active';\n    marks[i].scrollIntoView({{behavior: 'smooth', block: 'center'}});\n    return JSON.stringify({{current: i + 1, total: marks.length}});\n}})();\n"
        if action == 'next':
            return "\n(function() {\n    var marks = document.querySelectorAll('mark.dbp-hl, mark.dbp-hl-active');\n    if (!marks.length) return JSON.stringify({current: 0, total: 0});\n    var cur = document.querySelector('mark.dbp-hl-active');\n    var idx = cur ? parseInt(cur.getAttribute('data-idx')) : -1;\n    if (cur) cur.className = 'dbp-hl';\n    var nextIdx = (idx + 1) % marks.length;\n    marks[nextIdx].className = 'dbp-hl-active';\n    marks[nextIdx].scrollIntoView({behavior: 'smooth', block: 'center'});\n    return JSON.stringify({current: nextIdx + 1, total: marks.length});\n})();\n"
        if action == 'prev':
            return "\n(function() {\n    var marks = document.querySelectorAll('mark.dbp-hl, mark.dbp-hl-active');\n    if (!marks.length) return JSON.stringify({current: 0, total: 0});\n    var cur = document.querySelector('mark.dbp-hl-active');\n    var idx = cur ? parseInt(cur.getAttribute('data-idx')) : marks.length;\n    if (cur) cur.className = 'dbp-hl';\n    var prevIdx = (idx - 1 + marks.length) % marks.length;\n    marks[prevIdx].className = 'dbp-hl-active';\n    marks[prevIdx].scrollIntoView({behavior: 'smooth', block: 'center'});\n    return JSON.stringify({current: prevIdx + 1, total: marks.length});\n})();\n"
        if action == 'clear':
            return "\n(function() {\n    document.querySelectorAll('mark.dbp-hl, mark.dbp-hl-active').forEach(function(m) {\n        m.replaceWith(m.textContent);\n    });\n    return JSON.stringify({total: 0});\n})();\n"
        return ''

    @staticmethod

    def _parse_search_result(result):
        try:
            data = json.loads(result) if isinstance(result, str) else result or {}
            if not isinstance(data, dict):
                return {}
            return {'total': int(data.get('total', 0) or 0), 'current': int(data.get('current', 0) or 0)}
        except Exception:
            return {}

    def _run_search_js(self, code, callback=None):
        self._run_search_js_pair(code, code, callback)

    def _run_search_js_pair(self, content_code, comments_code, callback=None):
        '''分别向两个视图注入 JS 并汇总回调。

        旧实现必须两个视图都回调才更新 UI；任一视图未加载/JS 抛错时，
        搜索状态就永远停在旧值。这里加入 1.5s 超时兜底，并用代次号
        丢弃旧搜索的迟到回调。
        '''

        results = {}
        finished = False
        self._search_gen = getattr(self, '_search_gen', 0) + 1
        gen = self._search_gen
        def finish():
            nonlocal finished
            if finished or gen != self._search_gen:
                return
            finished = True
            if callback:
                callback(results)

        def make_handler(key):
            def _on_result(result):
                if gen != self._search_gen:
                    return
                results[key] = result
                if len(results) >= 2:
                    finish()

            return _on_result

        try:
            self.content_text.page().runJavaScript(content_code, make_handler('content'))
            self.comment_view.page().runJavaScript(comments_code, make_handler('comments'))
        except RuntimeError:
            pass
        QTimer.singleShot(1500, finish)

    def _on_search(self):
        '''执行搜索高亮'''

        keyword = self.search_input.text().strip()
        if not keyword:
            self._on_search_clear()
            return
        def on_done(results):
            content = self._parse_search_result(results.get('content'))
            comments = self._parse_search_result(results.get('comments'))
            self._search_view_counts = {'content': content.get('total', 0), 'comments': comments.get('total', 0)}
            total = self._search_view_counts['content'] + self._search_view_counts['comments']
            self._search_matches = total
            self._search_current = 1 if total > 0 else 0
            self._update_search_ui()
            if total > 0:
                self._focus_search_index(1)

        self._run_search_js(self._search_js(keyword, 'highlight'), on_done)

    def _focus_search_index(self, target):
        '''在「正文 + 评论」两个视图之间按全局序号定位匹配'''

        counts = getattr(self, '_search_view_counts', {}) or {}
        c_total = int(counts.get('content', 0) or 0)
        m_total = int(counts.get('comments', 0) or 0)
        total = c_total + m_total
        if total <= 0:
            return
        target = max(1, min(total, int(target)))
        content_idx = target - 1 if target <= c_total else -1
        comment_idx = target - c_total - 1 if target > c_total else -1
        def on_done(results):
            content = self._parse_search_result(results.get('content'))
            comments = self._parse_search_result(results.get('comments'))
            new_counts = {'content': content.get('total', c_total), 'comments': comments.get('total', m_total)}
            new_total = new_counts['content'] + new_counts['comments']
            self._search_view_counts = new_counts
            self._search_matches = new_total
            self._search_current = min(target, new_total) if new_total > 0 else 0
            self._update_search_ui()

        self._run_search_js_pair(self._search_js('', 'focus', content_idx), self._search_js('', 'focus', comment_idx), on_done)

    def _on_search_next(self):
        '''跳转到下一个匹配（跨正文/评论统一编号）'''

        if self._search_matches <= 0:
            return
        current = self._search_current or 0
        target = current + 1
        if target > self._search_matches:
            target = 1
        self._focus_search_index(target)

    def _on_search_prev(self):
        '''跳转到上一个匹配（跨正文/评论统一编号）'''

        if self._search_matches <= 0:
            return
        current = self._search_current or 0
        target = current - 1
        if target < 1:
            target = self._search_matches
        self._focus_search_index(target)

    def _on_search_clear(self):
        '''清除所有高亮'''

        self.search_input.clear()
        self._search_matches = 0
        self._search_current = 0
        self._search_view_counts = {'content': 0, 'comments': 0}
        self._update_search_ui()
        def _ignore(r):
            pass

        self._run_search_js(self._search_js('', 'clear'), _ignore)

    def _update_search_ui(self):
        '''更新搜索 UI 状态'''

        total = self._search_matches
        cur = self._search_current
        if total > 0:
            self.search_info.setText(f'{cur}/{total} 匹配')
            self.search_info.setStyleSheet(f'color: {C['green']}; font-size: 11px; border: none; padding: 2px 0;')
        elif self.search_input.text().strip():
            self.search_info.setText('0 匹配')
            self.search_info.setStyleSheet(f'color: {C['sidebar_text2']}; font-size: 11px; border: none; padding: 2px 0;')
        else:
            self.search_info.setText('')
            self.search_info.setStyleSheet('')
        has_match = total > 0
        self.btn_search_prev.setEnabled(has_match)
        self.btn_search_next.setEnabled(has_match)
        self.btn_search_clear.setEnabled(bool(self.search_input.text().strip()))
