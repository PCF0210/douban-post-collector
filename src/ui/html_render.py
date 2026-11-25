# -*- coding: utf-8 -*-
import html as _html_mod
import re
from html.parser import HTMLParser

from .ui_theme import content_css, comments_css

_ALLOWED_TAGS = frozenset({'a', 'b', 'i', 'p', 'br', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'ol', 'td', 'th', 'tr', 'ul', 'col', 'div', 'img', 'pre', 'code', 'span', 'table', 'tbody', 'thead', 'strong', 'caption', 'colgroup', 'blockquote'})

_VOID_TAGS = frozenset({'br', 'hr', 'col', 'img'})

_SKIP_CONTENT_TAGS = frozenset({'svg', 'base', 'head', 'link', 'math', 'meta', 'embed', 'frame', 'style', 'title', 'iframe', 'object', 'script', 'frameset', 'noscript', 'template'})

_SAFE_STYLE_PROPS = frozenset({'font-size', 'max-width', 'border-top', 'font-style', 'margin-top', 'max-height', 'text-align', 'word-break', 'border-left', 'font-weight', 'line-height', 'margin-left', 'padding-top', 'text-indent', 'white-space', 'border-color', 'border-right', 'border-style', 'border-width', 'margin-right', 'padding-left', 'border-bottom', 'border-radius', 'margin-bottom', 'overflow-wrap', 'padding-right', 'letter-spacing', 'padding-bottom', 'vertical-align', 'text-decoration', 'background-color', 'color', 'width', 'border', 'height', 'margin', 'padding'})

_ALIGN_VALUES = frozenset({'left', 'right', 'center', 'justify'})

class _DisplayHTMLSanitizer(HTMLParser):
    '''基于标准库 HTMLParser 的正文清洗器。

    与旧正则实现相比：
    - 文本节点单独转义，正文里的 "<" 不会被当成标签起点；
    - 属性按 token 解析，未加引号的 href/src 同样会经过协议白名单；
    - 不在白名单的标签只丢标签、保留文字；危险容器连内容一起丢。
    '''

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._stack = []
        self._skip_tag = ''
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        self._start(tag.lower(), attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag.lower(), attrs, self_closing=True)

    def _start(self, tag, attrs, self_closing):
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return
        attrs_html = self._build_attrs(tag, attrs)
        if attrs_html is None:
            return
        self.parts.append(f'<{tag}{attrs_html}>')
        if tag in _VOID_TAGS:
            return
        if self_closing:
            self.parts.append(f'</{tag}>')
            return
        self._stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = ''
                    self._skip_depth = 0
            return
        if tag in _VOID_TAGS or tag not in _ALLOWED_TAGS:
            return
        self._close_until(tag)

    def _close_until(self, tag):
        if tag not in self._stack:
            return
        while self._stack:
            top = self._stack.pop()
            self.parts.append(f'</{top}>')
            if top == tag:
                return

    def handle_data(self, data):
        if self._skip_tag or not data:
            return
        self.parts.append(_html_mod.escape(data, quote=False))

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def finish(self):
        super().close()
        while self._stack:
            self.parts.append(f'</{self._stack.pop()}>')
        return ''.join(self.parts)

    def _build_attrs(self, tag, attrs):
        if tag == 'img':
            return self._build_img_attrs(attrs)
        pieces = []
        seen = set()
        href = ''
        for raw_name, raw_value in attrs:
            if not raw_name:
                continue
            name = raw_name.lower()
            if name in seen:
                continue
            seen.add(name)
            value = '' if raw_value is None else raw_value
            if name.startswith('on'):
                continue
            if name == 'href' and tag == 'a':
                href = self._safe_url(value)
                continue
            if name in ('alt', 'title'):
                value = value.strip()
                if not value:
                    continue
                pieces.append((name, value[:2000]))
                continue
            if name == 'class':
                value = re.sub('[^A-Za-z0-9_\\-\\s]', '', value)
                value = ' '.join(value.split())[:400]
                if not value:
                    continue
                pieces.append(('class', value))
                continue
            if name == 'style':
                safe = self._sanitize_style(value)
                if not safe:
                    continue
                pieces.append(('style', safe))
                continue
            if name in ('width', 'height') and tag in ('table', 'td', 'th', 'col'):
                value = value.strip()
                if not re.fullmatch('\\d{1,4}%?', value):
                    continue
                pieces.append((name, value))
                continue
            if name in ('colspan', 'rowspan') and tag in ('td', 'th'):
                value = value.strip()
                if not value.isdigit():
                    continue
                if 1 <= int(value):
                    if not int(value) <= 100:
                        continue
                else:
                    continue
                pieces.append((name, value))
                continue
            if name != 'align':
                continue
            if tag not in ('p', 'div', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                continue
            value = value.strip().lower()
            if value not in _ALIGN_VALUES:
                continue
            pieces.append(('align', value))
            continue
        if href:
            pieces.insert(0, ('href', href))
        if not pieces:
            return ''
        return ''.join((f' {key}="{_html_mod.escape(str(val), quote=True)}"' for key, val in pieces))

    def _build_img_attrs(self, attrs):
        src = ''
        alt = ''
        title = ''
        for raw_name, raw_value in attrs:
            name = (raw_name or '').lower()
            value = '' if raw_value is None else raw_value
            if name == 'src' and not src:
                src = self._safe_url(value, allow_data=True)
            elif name == 'alt' and not alt:
                alt = value.strip()[:500]
            else:
                if name != 'title':
                    continue
                if title:
                    continue
                title = value.strip()[:500]
        if not src:
            return
        style = 'max-width:100%; height:auto; border-radius:6px; margin:8px 0;'
        out = f' src="{_html_mod.escape(src, quote=True)}" alt="{_html_mod.escape(alt, quote=True)}" style="{style}" loading="lazy"'
        if title:
            out += f' title="{_html_mod.escape(title, quote=True)}"'
        return out

    @staticmethod
    def _safe_url(value, allow_data=False):
        text = value or ''
        for _ in range(3):
            decoded = _html_mod.unescape(text)
            if decoded == text:
                break
            text = decoded
        text = ''.join((ch for ch in text
             if ord(ch) >= 32 and ch != '\x7f' and ch != '\x00')).strip()
        if not text:
            return ''
        low = text.lower()
        if low.startswith(('http://', 'https://')):
            return text
        if allow_data:
            if re.fullmatch('data:image/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\\s]+', text, flags=re.IGNORECASE):
                return text
        return ''

    @staticmethod
    def _sanitize_style(style):
        if not style:
            return ''
        kept = []
        for decl in style.split(';'):
            if ':' not in decl:
                continue
            prop, value = decl.split(':', 1)
            prop = prop.strip().lower()
            value = value.strip()
            if prop not in _SAFE_STYLE_PROPS or not value:
                continue
            low = value.lower()
            if any((bad in low for bad in ('url(', 'expression', 'javascript:', '@import', 'behavior:', '\\'))):
                continue
            if len(value) > 200:
                continue
            if re.fullmatch('[#A-Za-z0-9\\s.,%()\\-+/]+', value):
                kept.append(f'{prop}: {value}')
        return '; '.join(kept)

def _clean_html_for_display(html):
    '''清洗 HTML 用于 QWebEngineView 显示。

    - 白名单标签/属性，丢弃脚本、样式、iframe、事件属性；
    - href/src 只允许 http/https（img 额外允许 data:image）；
    - 正文按文本节点转义，"1 < 2 and 3 > 2" 不会被吞；
    - 为 <img> 注入响应式样式，配合 _LIGHTBOX_JS 点击放大。
    '''

    if not html:
        return ''
    parser = _DisplayHTMLSanitizer()
    try:
        parser.feed(str(html))
        result = parser.finish()
    except Exception:
        return _html_mod.escape(str(html), quote=False)
    result = re.sub('\\n{3,}', '\n\n', result)
    return result.strip()

_PLAIN_BLOCK_TAGS = frozenset({'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'li', 'ol', 'tr', 'ul', 'div', 'pre', 'table', 'blockquote'})

class _PlainTextExtractor(HTMLParser):
    '''把豆瓣正文 HTML 转成纯文本，保留 "<" 等正文符号。'''

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_tag = ''
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_tag = tag
            self._skip_depth = 1
            return
        if tag in _PLAIN_BLOCK_TAGS:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in _PLAIN_BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = ''
                    self._skip_depth = 0
            return
        if tag in _PLAIN_BLOCK_TAGS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self._skip_tag:
            if data:
                self.parts.append(data)

    def handle_comment(self, data):
        pass

    def finish(self):
        super().close()
        text = ''.join(self.parts).replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub('[ \\t]+\\n', '\n', text)
        text = re.sub('\\n{3,}', '\n\n', text)
        return text.strip()

def _strip_html(text):
    if not text:
        return ''
    parser = _PlainTextExtractor()
    try:
        parser.feed(str(text))
        return parser.finish()
    except Exception:
        return re.sub('<[^>]+>', '', str(text)).strip()

_KEYCAP_RE = re.compile('([0-9#*])\ufe0f?\u20e3')

def _plain_text(text):
    '''导出前把「8️⃣」这类组合键帽降级成普通字符

    Windows 记事本（以及部分编辑器）遇到 U+FE0F + U+20E3 组合后，会把同一行
    后续的中文渲染成空心方块，看起来像乱码。键帽本身只有装饰意义，降级为
    「8」不影响阅读。
    '''

    if not text:
        return text or ''
    text = _KEYCAP_RE.sub(r'\1', str(text))
    return text.replace('\ufe0f', '')

def _reply_ref(c):
    '''取评论的回复引用：优先 reply_to（已归一化、带被回复的楼层），否则退回 ref_comment'''

    if not isinstance(c, dict):
        return None
    rt = c.get('reply_to')
    if isinstance(rt, dict):
        return rt
    ref = c.get('ref_comment')
    return ref if isinstance(ref, dict) else None

def _reply_target(ref):
    '''把回复引用格式化成「#楼层 @名字」（楼层未知时只有 @名字）'''

    if not ref or not isinstance(ref, dict):
        return ''
    rt_author = ref.get('author')
    name = rt_author.get('name', '') if isinstance(rt_author, dict) else str(rt_author or '')
    floor = ref.get('floor')
    parts = []
    if floor not in (None, ''):
        parts.append(f'#{floor}')
    if name:
        parts.append(f'@{name}')
    return ' '.join(parts)

def _format_comment_time(raw):
    """将 ISO 时间字符串格式化为 'YYYY-MM-DD HH:MM:SS'"""

    if not isinstance(raw, str):
        return raw or ''
    if 'T' in raw:
        return raw.replace('T', ' ').split('+')[0].split('.')[0]
    return raw

def comment_date(comment):
    '''评论所属日期（'YYYY-MM-DD'），无法解析时返回空串（用于按日期筛选）'''

    if not isinstance(comment, dict):
        return ''
    raw = _format_comment_time(comment.get('create_time') or comment.get('time') or '')
    match = re.match(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', str(raw).strip())
    if not match:
        return ''
    return f'{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}'

def _photo_url(photo):
    '''从评论 photo 对象提取图片 URL（豆瓣 API 嵌套在 image.normal.url）'''

    img_obj = photo.get('image') or {}
    if isinstance(img_obj, dict):
        normal = img_obj.get('normal') or {}
        if isinstance(normal, dict) and normal.get('url'):
            return normal['url']
    return photo.get('url') or photo.get('src') or ''

COMMENT_RENDER_CHUNK = 300

_BODY_IMG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)

_BODY_IMG_SRC_RE = re.compile(r'''src\s*=\s*(?:"([^"]*)"|'([^']*)')''', re.IGNORECASE)

_NO_BODY_HTML = ('<div class="welcome"><span class="welcome-mark">正文</span>'
                 '<h1>本帖没有正文内容</h1>'
                 '<p>可能是纯图片帖或投票帖，可以切换到「评论」「图片」查看。</p></div>')

_LIGHTBOX_JS = "\n<script>\n(function () {\n    var ov = document.createElement('div');\n    ov.style.cssText = 'display:none;position:fixed;inset:0;'\n        + 'background:rgba(15,18,25,0.88);z-index:99999;cursor:zoom-out;'\n        + '-webkit-user-select:none;align-items:center;justify-content:center;';\n    var im = document.createElement('img');\n    im.style.cssText = 'max-width:94%;max-height:94%;border-radius:8px;'\n        + 'box-shadow:0 8px 40px rgba(0,0,0,0.5);';\n    ov.appendChild(im);\n    document.body.appendChild(ov);\n    function close() { ov.style.display = 'none'; im.src = ''; }\n    ov.addEventListener('click', close);\n    document.addEventListener('keydown', function (e) {\n        if (e.key === 'Escape') close();\n    });\n    document.addEventListener('click', function (e) {\n        var t = e.target;\n        if (t && t.tagName === 'IMG' && t.src) {\n            e.preventDefault();\n            im.src = t.src;\n            ov.style.display = 'flex';\n        }\n    }, true);\n})();\n</script>\n"

def build_content_html(body_html):
    '''将清洗后的 body HTML 包裹为完整 HTML5 文档，内嵌阅读样式'''

    safe_body = body_html or ''
    css = content_css()
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="referrer" content="unsafe-url">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<style>{css}</style>\n</head>\n<body>\n'
        f'<main class="reader">{safe_body}</main>\n</body>\n</html>'
    ) + _LIGHTBOX_JS

def annotate_body_ocr(body_html):
    '''给正文里的图片打上 data-ocr-url，供 OCR 结果注入识别文字'''

    if not body_html:
        return body_html

    def _repl(match):
        tag = match.group(0)
        if 'data-ocr-url' in tag:
            return tag
        src_m = _BODY_IMG_SRC_RE.search(tag)
        if not src_m:
            return tag
        url = src_m.group(1) if src_m.group(1) is not None else src_m.group(2)
        if not url:
            return tag
        # HTML 里的 &amp; 要还原成 &，否则和数据库里的 URL 对不上
        url = _html_mod.unescape(url).strip()
        if not url:
            return tag
        attr = f' data-ocr-url="{_html_mod.escape(url, quote=True)}"'
        if tag.endswith('/>'):
            return tag[:-2] + attr + ' />'
        return tag[:-1] + attr + '>'

    return _BODY_IMG_RE.sub(_repl, body_html)

def build_comments_html(comments, ocr_results=None):
    '''生成评论列表的完整 HTML 文档（支持图片内联 + 回复链 + OCR 文字）'''

    if comments is None:
        return build_base_html(
            '<div class="empty"><span class="empty-mark">评论</span>'
            '<div class="empty-title">还没有抓取评论</div>'
            '<div class="empty-sub">粘贴链接开始采集，评论会按楼层显示在这里</div></div>'
        )
    if not comments:
        return build_base_html(
            '<div class="empty"><span class="empty-mark">评论</span>'
            '<div class="empty-title">暂无评论</div>'
            '<div class="empty-sub">这个帖子还没有人回复</div></div>'
        )
    ocr_results = ocr_results or {}
    parts = [build_comment_card(i, c, ocr_results) for i, c in enumerate(comments)]
    cards = ''.join(parts)
    return build_base_html(f'<div class="comments" id="dbp-comments">{cards}</div>')

def build_comment_card(i, c, ocr_results):
    '''生成单条评论的 HTML 卡片'''

    c_author = c.get('author', {})
    c_name = c_author.get('name', '匿名') if isinstance(c_author, dict) else str(c_author)
    c_time = _format_comment_time(c.get('create_time', c.get('time', '')))
    c_content = c.get('content') or c.get('text', '')
    c_floor = c.get('floor', i + 1)
    if c_floor in (None, ''):
        c_floor = i + 1
    c_ip = c.get('ip_location', '')
    content_html = _clean_html_for_display(c_content) if c_content else ''
    comment_image_urls = []
    photos = c.get('photos') or []
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, dict):
                photo_url = _photo_url(photo)
                if photo_url:
                    comment_image_urls.append(photo_url)
                    alt = photo.get('description') or photo.get('alt', '图片')
                    # data-ocr-url / data-ocr-done 供 OCR 完成后原地补写识别文字
                    # （不重新 setHtml 整页，避免界面跳动与 WebEngine 重渲染崩溃）
                    done_attr = ' data-ocr-done="1"' if photo_url in ocr_results else ''
                    content_html += f'<img src="{_html_mod.escape(photo_url)}" alt="{_html_mod.escape(str(alt))}" data-ocr-url="{_html_mod.escape(photo_url)}"{done_attr} style="max-width:100%; max-height:420px; height:auto; width:auto; object-fit:contain; background:#F7F8F5; border-radius:8px; margin:8px auto; display:block;">'
    ocr_blocks = []
    for img_url in comment_image_urls:
        if img_url in ocr_results:
            text = ocr_results[img_url]
            if text:
                escaped_text = _html_mod.escape(text).replace('\n', '<br>')
                ocr_blocks.append(f'<div class="ocr-block"><span class="ocr-label">OCR 识别文字</span><div class="ocr-text">{escaped_text}</div></div>')
            else:
                ocr_blocks.append('<div class="ocr-block ocr-empty"><span class="ocr-label">OCR 识别文字</span><div class="ocr-text">[图片中未识别到文字]</div></div>')
    if ocr_blocks:
        content_html += '<div class="ocr-group">' + ''.join(ocr_blocks) + '</div>'
    # 楼中楼回复：豆瓣 API 的字段是 ref_comment（没有 floor），scraper/DB 已
    # 归一化成 reply_to 并补好楼层，这里只负责展示。
    reply_badge = ''
    quote_html = ''
    ref = _reply_ref(c)
    if ref:
        target = _reply_target(ref) or '某条评论'
        rt_text = _strip_html(ref.get('text', '') or '')
        badge_title = f'回复 {target}'
        reply_badge = (
            f'<span class="reply-badge" title="{_html_mod.escape(badge_title)}">'
            f'↳ 回复 {_html_mod.escape(target)}</span>'
        )
        if rt_text:
            snippet = rt_text if len(rt_text) <= 90 else rt_text[:90] + '…'
            quote_html = (
                '<div class="reply-quote">'
                f'<span class="reply-quote-who">回复 {_html_mod.escape(target)}</span>'
                f'<div class="reply-quote-text">{_html_mod.escape(snippet)}</div>'
                '</div>'
            )
    ip_part = ''
    if c_ip and str(c_ip).strip():
        ip_part = f'<span class="ip">{_html_mod.escape(str(c_ip))}</span>'
    is_reply = bool(ref)
    card_class = 'comment-card reply-card' if is_reply else 'comment-card'
    body = quote_html + (content_html if content_html else '<span class="muted">[无内容]</span>')
    initial = (str(c_name).strip() or '?')[:2].upper()
    avatar_colors = (
        ('#E9F7EF', '#1B8A50'),
        ('#EAF2FF', '#3C6FD6'),
        ('#FFF3E6', '#C77A2E'),
        ('#F3EDFF', '#7A5AC8'),
    )
    avatar_bg, avatar_fg = avatar_colors[i % len(avatar_colors)]
    avatar_html = (
        f'<div class="avatar" style="background:{avatar_bg};color:{avatar_fg};">'
        f'{_html_mod.escape(initial)}</div>'
    )
    return f'''
    <article class="{card_class}">
        {avatar_html}
        <div class="c-main">
            <div class="comment-header">
                <span class="author">{_html_mod.escape(str(c_name))}</span>
                <span class="floor">#{c_floor}</span>
                {reply_badge}
                <span class="meta-right">
                    {ip_part}
                    <span class="time">{_html_mod.escape(str(c_time))}</span>
                </span>
            </div>
            <div class="comment-body">{body}</div>
        </div>
    </article>'''

def build_base_html(body_html):
    '''包裹 body HTML 为完整 HTML5 文档（评论区基础样式）'''

    safe_body = body_html or ''
    css = comments_css()
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="referrer" content="unsafe-url">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f'<style>{css}</style>\n</head>\n<body>\n'
        f'{safe_body}\n</body>\n</html>'
    ) + _LIGHTBOX_JS

def ocr_inject_js(payload):
    '''OCR 文字注入脚本：在每张 data-ocr-url 图片后插入识别文字块'''

    return (
        '(function(map){'
        'var imgs=document.querySelectorAll("img[data-ocr-url]");'
        'for(var i=0;i<imgs.length;i++){'
        'var img=imgs[i];var url=img.getAttribute("data-ocr-url");'
        'if(!url||!(url in map)){continue;}'
        'if(img.getAttribute("data-ocr-done")==="1"){continue;}'
        'var box=document.createElement("div");box.className="ocr-block";'
        'var label=document.createElement("span");label.className="ocr-label";label.textContent="OCR 识别文字";'
        'var text=document.createElement("div");text.className="ocr-text";'
        'text.style.whiteSpace="pre-wrap";text.textContent=map[url];'
        'box.appendChild(label);box.appendChild(text);'
        'img.insertAdjacentElement("afterend",box);'
        'img.setAttribute("data-ocr-done","1");'
        '}return imgs.length;})(' + payload + ')'
    )
