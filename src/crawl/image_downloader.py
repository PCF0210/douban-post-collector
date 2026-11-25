# -*- coding: utf-8 -*-
"""图片下载工作线程 - 增强反反爬"""
import html as _html_mod
import os
import re
import time
import random
import hashlib
import threading

import requests

try:
    from PIL import Image
except ImportError:  # Pillow 缺失时退化为只检查文件大小
    Image = None

from PySide6.QtCore import QObject, Signal

from ..core.config import (
    DEFAULT_TIMEOUT, IMAGE_DOWNLOAD_CONCURRENCY, MAX_IMAGE_BYTES, MAX_RETRIES,
    RETRY_BACKOFF_BASE,
)
from ..core.file_log import log_error
from ..data.db_manager import mark_images_downloaded
from ..core.identity import (
    create_session, image_headers, pick_language, pick_profile,
)
from ..core.tls import (
    HAS_CURL_CFFI,
    CurlConnectionError, CurlTimeout, CurlError,
)
from ..core.limiter import AdaptiveRateLimiter


def extract_image_urls(html_or_text):
    """从 HTML 或文本中提取豆瓣图片 URL"""
    if not html_or_text:
        return []
    text = html_or_text if isinstance(html_or_text, str) else str(html_or_text)
    # 先反转义 HTML 实体，否则 &amp; 之类的 query 会把 URL 截断
    text = _html_mod.unescape(text)

    urls = []
    seen = set()

    def _add(url):
        url = _normalize_image_url(url)
        if url and url not in seen and _is_valid_image_url(url):
            seen.add(url)
            urls.append(url)

    # 匹配 <img src="..."> / srcset / data-* 懒加载属性
    attr_pattern = re.compile(
        r'(?:src|data-src|data-original|data-actualsrc|data-lazy-src)\s*=\s*["\']'
        r'([^"\']+)["\']',
        re.IGNORECASE,
    )
    for m in attr_pattern.finditer(text):
        _add(m.group(1))

    # 匹配裸 URL（豆瓣图床链接，路径里可能没有扩展名）
    bare_pattern = re.compile(
        r'https?://[\w\-.]*doubanio\.com/[^\s"\'<>)\\]+',
        re.IGNORECASE
    )
    for m in bare_pattern.finditer(text):
        _add(m.group(0).rstrip('.,;'))

    return urls


def _normalize_image_url(url):
    """补全协议相对（//img...）与站内相对（/pics/...）的图片地址

    豆瓣正文/评论里这两种写法都出现过，直接按「必须以 http 开头」过滤会漏图。
    """
    url = (url or '').strip()
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('/'):
        return 'https://www.douban.com' + url
    return url


def _is_valid_image_url(url):
    """检查是否为有效的图片 URL（应传入 _normalize_image_url 处理后的地址）"""
    if not url or not url.startswith('http'):
        return False
    skip_exts = ('.svg', '.ico', '.css', '.js')
    lower = url.lower().split('?')[0]
    if any(lower.endswith(ext) for ext in skip_exts):
        return False
    return True


def _image_filename(url, index):
    """根据 URL 生成文件名（同名不同图不会互相覆盖）

    豆瓣图床常用 `.../public/p1234567.jpg?w=600` 这类形式，同一个 basename
    可能对应多张不同的图（仅靠 query 区分）。若只取 basename，后一张会被
    「文件已存在 -> 跳过」逻辑当成已下载，并把 local_path 写成前一张图的路径，
    造成数据库记录的 URL 与实际文件不是同一张图。
    这里在文件名里拼入完整 URL 的哈希，保证 URL 与文件一一对应。
    """
    path = url.split('?')[0]
    basename = os.path.basename(path)
    digest = hashlib.md5(url.encode('utf-8', 'ignore')).hexdigest()[:10]

    if basename and '.' in basename:
        # 清理文件名
        basename = re.sub(r'[^\w.\-]', '_', basename)
        stem, dot, ext = basename.rpartition('.')
        if not stem:
            stem, _, ext = basename, '', 'jpg'
        # 控制长度：主干过长时只留哈希
        if len(stem) > 40:
            stem = stem[:40]
        return f'{stem}_{digest}.{ext}'

    # 回退：序号 + hash
    ext = 'jpg'
    for e in ('png', 'gif', 'webp', 'jpeg'):
        if e in url.lower():
            ext = e
            break
    return f'{index:03d}_{digest}.{ext}'


def _sniff_image_type(head):
    """按文件头魔数判断图片格式，返回 'jpeg'/'png'/'gif'/'webp'/'bmp'，不认识返回 None。

    Content-Type 缺失、为 application/octet-stream，或 CDN 把错误页标成 image/*
    时，只能靠文件头判断真实类型。head 至少传前 12 个字节（WEBP 需要看到
    RIFF....WEBP 才能确认）。
    """
    if not head:
        return None
    head = bytes(head)
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return 'gif'
    # BMP 的魔数只有 'BM' 两个字节，较容易误判，但后续 Pillow verify 会兜底
    if head.startswith(b'BM'):
        return 'bmp'
    if len(head) >= 12 and head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return 'webp'
    return None


def _content_length(resp):
    """取响应 Content-Length（缺失/非法时返回 0）"""
    try:
        return int(resp.headers.get('Content-Length') or 0)
    except (TypeError, ValueError):
        return 0


def _is_valid_local_image(path):
    """本地文件是否是完整图片（用于复用旧文件、校验 .part）

    优先用 Pillow open+verify（能发现截断/解不开的图）；Pillow 缺失时至少
    校验文件头魔数，避免把 HTML 错误页/半截文件当成图片永久标记。
    大小也按 MAX_IMAGE_BYTES 上限检查。
    """
    try:
        if not os.path.isfile(path):
            return False
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_IMAGE_BYTES:
            return False
        with open(path, 'rb') as f:
            head = f.read(12)
        if _sniff_image_type(head) is None:
            return False
        if Image is None:
            return True
        try:
            with Image.open(path) as im:
                im.verify()
            return True
        except Exception:
            return False
    except Exception:
        return False


def _remove_file(path):
    """删除临时/损坏文件，失败不抛异常"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


class ImageDownloadWorker(QObject):
    """后台图片下载工作线程（线程级独立 session 与身份）"""

    log = Signal(str)
    progress = Signal(int, int)       # current, total
    finished = Signal(int, str)       # count, save_dir
    error = Signal(str)
    image_downloaded = Signal(str, str)  # url, filepath

    # 并发数由 config.IMAGE_DOWNLOAD_CONCURRENCY 控制（见该常量的实测说明）
    DOWNLOAD_CONCURRENCY = IMAGE_DOWNLOAD_CONCURRENCY
    # 图片下载状态攒够这么多条再统一写库，避免每张图一次连接 + 一次 commit
    _MARK_BATCH = 20
    # 写库失败后的重试退避（秒）：最多重试 3 次，失败也不影响已完成的下载
    _MARK_RETRY_DELAYS = (0.2, 0.5, 1.0)

    def __init__(self, image_urls, save_dir, use_proxy=False, proxy=None, topic_id=None):
        super().__init__()
        self.image_urls = image_urls
        self.save_dir = save_dir
        self.use_proxy = use_proxy
        self.proxy = proxy
        self.topic_id = topic_id
        self._stop = False
        # 自适应限速（图片下载用更小的基础延迟，线程安全）
        self._rate_limiter = AdaptiveRateLimiter(
            log_callback=lambda msg: self.log.emit(msg), base_delay=0.3)
        # 每个下载线程独立的 session 与设备身份（thread-local）
        self._local = threading.local()
        # 待写库的 (url, local_path)，由 _mark_lock 保护（多线程下载）
        self._pending_marks = []
        self._mark_lock = threading.Lock()

    def stop(self):
        self._stop = True

    @property
    def is_running(self):
        """是否仍在运行（尚未收到停止请求）"""
        return not self._stop

    def _sleep(self, seconds):
        """可被 stop() 打断的等待（避免「停止」等满整个退避时间）"""
        remaining = float(seconds)
        while remaining > 0:
            if self._stop:
                return False
            chunk = 0.1 if remaining > 0.1 else remaining
            time.sleep(chunk)
            remaining -= chunk
        return not self._stop

    def _mark_db_downloaded(self, url, filepath):
        """回写数据库：标记图片已下载（攒批写入，失败不影响下载流程）

        原来每张图各开一次连接 + commit，图片多时大部分时间耗在建连上；
        这里先缓存，凑够 _MARK_BATCH 条或下载结束时统一提交。
        """
        batch = None
        with self._mark_lock:
            self._pending_marks.append((url, filepath))
            if len(self._pending_marks) >= self._MARK_BATCH:
                batch = self._pending_marks
                self._pending_marks = []
        if batch:
            self._flush_marks(batch)

    def _flush_marks(self, batch):
        """把一批「已下载」状态写库：失败按指数退避重试，最终失败只告警不中断。

        图片文件本身已经落盘，写库失败只是少一条记录（OCR 阶段可能会重复下载），
        不值得让整个下载任务报错；但一定要在日志和界面上明确提示，不能静默吞掉。
        """
        if not batch:
            return
        last_error = None
        total_attempts = len(self._MARK_RETRY_DELAYS) + 1
        for attempt in range(total_attempts):
            try:
                mark_images_downloaded(batch, topic_id=self.topic_id)
                if attempt:
                    self.log.emit(f'  图片状态写库重试成功（第 {attempt + 1} 次尝试）')
                return
            except Exception as e:
                last_error = e
                if attempt < total_attempts - 1:
                    delay = self._MARK_RETRY_DELAYS[attempt]
                    self.log.emit(f'  图片状态写库失败（第 {attempt + 1}/{total_attempts} 次），'
                                  f'{delay}s 后重试: {e}')
                    # 这里用 time.sleep 而不是 self._sleep：用户点「停止」时也值得把
                    # 已经下载好的状态落库，最多多等 1.7s。
                    time.sleep(delay)
        log_error(f'图片下载状态写库失败（已尝试 {total_attempts} 次）: {last_error!r}')
        self.log.emit('⚠ 图片下载状态未能写入数据库（OCR 可能重复下载这些图片）')

    def _flush_db_marks(self):
        """把还没写库的下载状态全部提交（下载结束/取消时调用）"""
        with self._mark_lock:
            batch = self._pending_marks
            self._pending_marks = []
        self._flush_marks(batch)

    def _ensure_thread_identity(self):
        """初始化当前线程的设备档位与 session（首次调用时）

        一个档位同时决定 TLS 指纹 / UA / Client Hints，三者不会互相矛盾；
        图片下载是无登录的普通静态请求，不需要预热，但 Referer 要指向帖子页。
        """
        if getattr(self._local, 'session', None) is None:
            profile = pick_profile()
            self._local.profile = profile
            self._local.lang = pick_language()
            self._local.session = create_session(
                profile, proxy=self.proxy if self.use_proxy else None)
            self._local.referer = self._image_referer()

    def _image_referer(self):
        """图片所在页面：帖子页（短链/非数字 ID 时退回 m.douban.com 首页）"""
        tid = str(self.topic_id or '')
        if tid.isdigit():
            return f'https://m.douban.com/group/topic/{tid}/'
        return 'https://m.douban.com/'

    def _build_headers(self, url=''):
        """图片请求头（Sec-Fetch-Site 按图片域名与帖子页域名的真实关系计算）"""
        return image_headers(
            self._local.profile, referer=self._local.referer,
            lang=self._local.lang, url=url,
        )

    def _content_type_ok(self, resp, head=b''):
        """HTTP 200 也可能是 HTML 错误页：Content-Type 与文件头魔数结合判断。

        - image/* 先接受（落盘后还会用 _is_valid_local_image 再校验文件头）；
        - 空 / application/octet-stream / binary/octet-stream 不可信，
          只有文件头魔数能识别为图片才接受；
        - 其它类型（text/html、application/json 等）直接判失败。
        """
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if ctype.startswith('image/'):
            return True
        if ctype in ('application/octet-stream', 'binary/octet-stream') or not ctype:
            return _sniff_image_type(head) is not None
        return False

    def _download_one(self, url, i, total):
        """下载单张图片（线程池工作函数），返回 (ok, skipped)

        ok: 下载/校验成功；skipped: 本地已存在且通过校验，直接复用
        任何异常都在这里吞掉，避免线程池 future.result() 把整个 run() 打崩。
        """
        try:
            return self._download_one_impl(url, i, total)
        except Exception as e:
            self.log.emit(f'  图片 {i+1} 下载异常: {e}')
            return False, False

    def _download_one_impl(self, url, i, total):
        self._ensure_thread_identity()
        filename = _image_filename(url, i + 1)
        filepath = os.path.join(self.save_dir, filename)
        part_path = filepath + '.part'

        # 复用已存在的文件前先校验，防止把上次下到一半/HTML 错误页
        # 当成完整图片写进数据库（坏图且无法自愈）。
        if os.path.exists(filepath):
            if _is_valid_local_image(filepath):
                self.image_downloaded.emit(url, filepath)
                self._mark_db_downloaded(url, filepath)
                return True, True
            self.log.emit(f'  图片 {i+1} 本地文件损坏，重新下载')
            _remove_file(filepath)

        # 上次异常退出留下的临时文件
        _remove_file(part_path)

        # 重试下载（集成自适应延迟与增强反爬检测）
        for attempt in range(MAX_RETRIES):
            if self._stop:
                _remove_file(part_path)
                return False, False
            resp = None
            try:
                if attempt > 0:
                    # 只做退避，不单独换 UA：UA/TLS/Client Hints 是一体的，
                    # 只换 UA 会立刻造成指纹矛盾（这是旧版本的问题）。
                    d_min, _ = self._rate_limiter.get_delay()
                    backoff = max(RETRY_BACKOFF_BASE ** attempt, d_min) + random.uniform(0, 1)
                    if not self._sleep(backoff):
                        _remove_file(part_path)
                        return False, False
                else:
                    # 首次请求只做极短抖动：真人浏览器渲染页面时图片是并发立即加载的，
                    # 给每张图人为加 0.1~0.3s 延迟，在 250 张的帖子上会白等 10s+。
                    # 真正的限速交给并发上限与失败后的自适应退避。
                    if not self._sleep(random.uniform(0.01, 0.05)):
                        _remove_file(part_path)
                        return False, False

                resp = self._local.session.get(
                    url, headers=self._build_headers(url),
                    timeout=DEFAULT_TIMEOUT, stream=True
                )
                if resp.status_code != 200:
                    if resp.status_code in (403, 429, 418, 503):
                        self.log.emit(f'  图片 {i+1} 被限制 (HTTP {resp.status_code})，重试中...')
                        self._rate_limiter.report_failure()
                        if not self._sleep(random.uniform(2, 4)):
                            _remove_file(part_path)
                            return False, False
                        continue
                    self.log.emit(f'  图片 {i+1} 失败 (HTTP {resp.status_code})')
                    break

                # 响应头大小预检：Content-Length 超限直接判失败，不浪费流量整段下载
                length = _content_length(resp)
                if length > MAX_IMAGE_BYTES:
                    self.log.emit(f'  图片 {i+1} 超过大小上限 '
                                  f'({length / 1024 / 1024:.1f}MB > '
                                  f'{MAX_IMAGE_BYTES / 1024 / 1024:.0f}MB)，跳过')
                    break

                # 先写 .part，校验通过后原子替换，绝不让半截文件占住正式路径。
                # 首块数据顺便做文件头魔数校验：Content-Type 为空、是
                # application/octet-stream，或 CDN 把错误页标成 image/* 时，
                # 文件头是唯一可信的判断依据。
                written = 0
                head = b''
                bad_type = False
                oversize = False
                with open(part_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if self._stop:
                            break
                        if not chunk:
                            continue
                        if not head:
                            head = bytes(chunk[:12])
                            if not self._content_type_ok(resp, head):
                                bad_type = True
                                break
                        f.write(chunk)
                        written += len(chunk)
                        if written > MAX_IMAGE_BYTES:
                            oversize = True
                            break

                if self._stop:
                    _remove_file(part_path)
                    return False, False
                if oversize:
                    self.log.emit(f'  图片 {i+1} 超过大小上限 '
                                  f'({MAX_IMAGE_BYTES / 1024 / 1024:.0f}MB)，跳过')
                    _remove_file(part_path)
                    break
                if bad_type:
                    self.log.emit(f'  图片 {i+1} 返回非图片内容 '
                                  f'(Content-Type={resp.headers.get("Content-Type")})')
                    self._rate_limiter.report_failure()
                    _remove_file(part_path)
                    if not self._sleep(random.uniform(1, 2)):
                        return False, False
                    continue
                if written <= 0:
                    self.log.emit(f'  图片 {i+1} 返回空内容')
                    self._rate_limiter.report_failure()
                    _remove_file(part_path)
                    if not self._sleep(random.uniform(1, 2)):
                        return False, False
                    continue
                # 文件头魔数 + Pillow 完整性校验（.part 与正式文件同一套标准）
                if not _is_valid_local_image(part_path):
                    self.log.emit(f'  图片 {i+1} 内容校验失败（可能被反爬替换）')
                    self._rate_limiter.report_failure()
                    _remove_file(part_path)
                    if not self._sleep(random.uniform(1, 2)):
                        return False, False
                    continue

                os.replace(part_path, filepath)
                self.image_downloaded.emit(url, filepath)
                self._mark_db_downloaded(url, filepath)
                self._rate_limiter.report_success()
                return True, False
            except (requests.exceptions.RequestException,  # 已覆盖 ConnectionError/Timeout
                    CurlConnectionError, CurlTimeout, CurlError):
                _remove_file(part_path)
                if attempt < MAX_RETRIES - 1:
                    if not self._sleep(random.uniform(1, 3)):
                        return False, False
                else:
                    self.log.emit(f'  图片 {i+1} 下载超时')
            except OSError as e:
                # 磁盘满/无权限等本地错误，重试也没用
                self.log.emit(f'  图片 {i+1} 写入失败: {e}')
                _remove_file(part_path)
                break
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass

        _remove_file(part_path)
        return False, False

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        try:
            os.makedirs(self.save_dir, exist_ok=True)
        except OSError as e:
            self.error.emit(f'无法创建图片保存目录: {e}')
            return

        total = len(self.image_urls)
        downloaded = 0
        failed = 0

        self.log.emit(f'开始下载 {total} 张图片（{self.DOWNLOAD_CONCURRENCY} 并发）到: {self.save_dir}')
        self.progress.emit(0, total)
        self.log.emit('图片下载: 已启用 curl_cffi TLS 伪装' if HAS_CURL_CFFI
                      else '图片下载: 已启用 TLSAdapter 指纹伪装')

        pool = ThreadPoolExecutor(max_workers=self.DOWNLOAD_CONCURRENCY)
        try:
            futures = {
                pool.submit(self._download_one, url, i, total): url
                for i, url in enumerate(self.image_urls)
            }
            done = 0
            for fut in as_completed(futures):
                if self._stop:
                    break
                try:
                    ok, _skipped = fut.result()
                except Exception as e:
                    # future 被取消或线程内出现未预期异常，都只记一张失败
                    self.log.emit(f'  图片下载任务异常: {e}')
                    ok = False
                if ok:
                    downloaded += 1
                else:
                    failed += 1
                done += 1
                self.progress.emit(done, total)
        except Exception as e:
            self.error.emit(f'图片下载线程异常: {e}')
            return
        finally:
            # 取消时也在后台等正在进行的请求收尾；_download_one 会检查 _stop，
            # 不会写坏文件。这里必须等，否则 QThread 结束后仍有线程回调已销毁对象。
            pool.shutdown(wait=True, cancel_futures=self._stop)
            # 不管正常结束、异常还是取消，都把攒着的下载状态落库
            self._flush_db_marks()

        if self._stop:
            self.log.emit(f'已取消下载（完成 {downloaded}/{total} 张）')
            # 取消也要发 finished，否则主窗口按钮/进度条永远停在“下载中”
            self.finished.emit(downloaded, self.save_dir)
            return

        msg = f'下载完成: {downloaded} 张成功'
        if failed > 0:
            msg += f', {failed} 张失败'
        self.log.emit(msg)
        self.finished.emit(downloaded, self.save_dir)
