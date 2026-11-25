# -*- coding: utf-8 -*-
"""爬虫工作线程 - 增强反反爬

反反爬要点（与早期版本的区别）：
- 身份一致性：TLS 指纹（curl_cffi impersonate）、UA、Client Hints 来自同一个
  设备档位（src/core/identity.py），不再出现「Safari UA + Chrome TLS」矛盾；
- bid 只从服务端预热响应里拿，不再伪造 16 位 hex / 空 dbcl2 / 空 ck；
- 不发豆瓣没有申请的设备性能 client hints（Device-Memory / Downlink / ECT /
  RTT / Viewport-Width / Sec-GPC），这些反而是脚本特征；
- 被限时先指数冷却（尊重 Retry-After），连续被拦才换「完整身份」（新
  session + 重新预热），且两次换身份至少间隔 IDENTITY_ROTATE_MIN_INTERVAL；
- 单次任务内连续被拦达到 MAX_CONSECUTIVE_BLOCKS 直接熔断中止，避免把临时
  限流刷成长时间封禁。
"""
import json
import random
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

import requests

from PySide6.QtCore import QObject, Signal

from ..core.config import (
    API_BASE, DEFAULT_TIMEOUT, MAX_RETRIES, COMMENTS_PER_PAGE, COMMENTS_PAGE_STEP,
    MAX_CONCURRENT_PAGES, RETRY_BACKOFF_BASE,
    COOKIE_PAGE_DELAY_MIN, COOKIE_PAGE_DELAY_MAX,
    WARMUP_PATHS, WARMUP_TIMEOUT, SHORT_LINK_TIMEOUT,
    IDENTITY_ROTATE_MIN_INTERVAL, IDENTITY_ROTATE_AFTER_BLOCKS,
    MAX_CONSECUTIVE_BLOCKS, BLOCK_COOLDOWN_BASE, BLOCK_COOLDOWN_MAX,
    RETRY_AFTER_MAX,
)
from .image_downloader import extract_image_urls
from ..data.db_manager import (
    save_scrape_result,
    get_max_floor, get_comment_by_floor, get_comment_dicts,
    normalize_ref_comment,
)
from ..core.file_log import log_error
from ..core.tls import (
    HAS_CURL_CFFI,
    CurlConnectionError, CurlTimeout, CurlError,
)
from ..core.identity import (
    create_session, pick_profile, pick_language, nav_headers, xhr_headers,
)
from ..core.limiter import AdaptiveRateLimiter


_ANTI_BOT_KEYWORDS = ('rate_limit', 'too_many', 'verify', 'challenge',
                      'block', '访问过于频繁', '请稍后再试')

# 反爬/限流类状态码
_BLOCK_STATUS = (403, 429, 418, 503)

# 网络层异常（requests 与 curl_cffi 都要捕获）
_NETWORK_ERRORS = (requests.exceptions.RequestException,
                   CurlConnectionError, CurlTimeout, CurlError)


def _response_json(resp):
    """解析响应 JSON，并把结果缓存在响应对象上（同一响应只 loads 一次）

    反爬检测与业务解析都要读同一份 body，之前每个 200 响应会被 json.loads
    两次，评论页多时纯属浪费。
    """
    cached = getattr(resp, '_dbp_json', None)
    if cached is not None:
        return cached
    data = resp.json()
    try:
        resp._dbp_json = data
    except Exception:
        pass
    return data


def _is_anti_bot_response(resp, data=None):
    """只检查错误字段/非 JSON 文本，避免把正文里的 block/verify 当拦截。

    data: 调用方已解析好的 JSON（复用可避免重复 loads）。
    """
    if data is None:
        try:
            data = _response_json(resp)
        except Exception:
            data = None
    if isinstance(data, dict):
        for key in ('error', 'msg', 'message', 'detail'):
            value = data.get(key)
            if isinstance(value, str):
                lowered = value.lower()
                if any(kw in lowered for kw in _ANTI_BOT_KEYWORDS):
                    return True
        code = str(data.get('code', '')).lower()
        return code in ('rate_limit', 'too_many', 'captcha', 'challenge')
    text = (resp.text or '')[:512].lower()
    return any(kw in text for kw in ('rate_limit', 'too_many',
                                     '访问过于频繁', '请稍后再试', 'captcha'))


def _mask_bid(bid):
    """日志里不输出完整 bid（它等价于匿名会话凭据）"""
    if not bid:
        return ''
    if len(bid) <= 6:
        return bid[:2] + '***'
    return f'{bid[:3]}***{bid[-2:]}'


class ScraperWorker(QObject):
    """后台爬虫工作线程"""

    log = Signal(str)
    progress = Signal(int, int)          # current, total
    finished = Signal(dict)              # result dict
    error = Signal(str)

    def __init__(self, topic_id, use_proxy=False, proxy=None, incremental=False):
        super().__init__()
        self.topic_id = topic_id
        self._stop = False
        self.use_proxy = use_proxy
        self.proxy = proxy
        # 增量更新：只拉本地最后一条评论之后的新楼层
        self.incremental = bool(incremental)

        # 当前设备身份：profile 决定 impersonate + UA + Client Hints，三者天然一致
        self._profile = pick_profile()
        self._lang = pick_language()

        # 身份生命周期
        self._identity_lock = threading.Lock()
        self._session = None
        self._identity_epoch = 0
        self._last_rotate_ts = 0.0

        # 反爬共享状态锁：并发评论页线程会同时更新被拦/熔断字段，
        # 只保护内存字段的读改写；持锁期间绝不做 sleep / 网络请求（RLock 允许嵌套读）。
        self._state_lock = threading.RLock()

        # 被拦截/熔断状态
        self._consecutive_blocks = 0
        self._pending_cooldown = 0.0
        self._force_rotate = False
        self._abort_reason = ''

        # 自适应限速器
        self._rate_limiter = AdaptiveRateLimiter(log_callback=lambda msg: self.log.emit(msg))
        # Referer 链：模拟真实用户浏览路径
        self._referer_chain = []
        # 线程局部 session（并发获取评论页时每个线程独立复用）
        self._thread_local = threading.local()

    def stop(self):
        self._stop = True

    def _abort_message(self):
        """把内部熔断原因翻译成给用户看的中文提示；未熔断返回空串"""
        reason = self._abort_reason
        if not reason:
            return ''
        if '连续' in reason:
            return '连续被反爬拦截，已停止抓取，请稍后重试（建议配置代理）'
        if '评论' in reason or '接口' in reason:
            return '评论接口被拦截（若电脑开启代理/VPN，请关闭后重试）'
        if '限速' in reason:
            return '请求被限速，已停止抓取，请稍后重试'
        return f'因反爬限制中止：{reason}'

    def _sleep(self, seconds):
        """可被 stop()/熔断打断的等待

        用 time.sleep 会让「停止」按钮等满整个退避时间（最长 15~16s），
        期间 worker 线程无法退出；熔断后也不该再让后台线程慢慢等冷却。
        这里分片轮询，取消延迟 <= 0.1s。
        返回 False 表示等待期间收到了停止请求或已熔断。
        """
        if self._stop or self._abort_message():
            return False
        remaining = float(seconds)
        while remaining > 0:
            if self._stop or self._abort_message():
                return False
            chunk = 0.1 if remaining > 0.1 else remaining
            time.sleep(chunk)
            remaining -= chunk
        return not (self._stop or self._abort_message())

    # -------------------- 身份 / session --------------------

    def _proxy(self):
        return self.proxy if self.use_proxy else None

    def _referer(self):
        if self._referer_chain:
            return self._referer_chain[-1]
        return f'https://m.douban.com/group/topic/{self.topic_id}/'

    def _check_bid(self, session):
        """检查服务端是否下发了 bid；不要自己伪造（真实 bid 是 11 字符 base64url）"""
        try:
            bid = session.cookies.get('bid') or ''
        except Exception:
            bid = ''
        if bid:
            self.log.emit(f'  bid cookie: {_mask_bid(bid)}（服务端下发）')
            return True
        self.log.emit('  ⚠ 预热后仍未拿到 bid cookie，接口可能按匿名请求处理')
        return False

    def _warmup(self, session):
        """预热：模拟真实用户浏览路径，构建 Referer 链并从响应拿 bid"""
        if self._stop:
            return
        home_url = 'https://m.douban.com/'
        mid_choices = [u for u in WARMUP_PATHS if u != home_url]
        mid_url = random.choice(mid_choices) if mid_choices else home_url
        topic_page = f'https://m.douban.com/group/topic/{self.topic_id}/'

        steps = [
            ('首页', home_url, None, 'none'),
            ('子频道', mid_url, home_url, 'same-origin'),
        ]
        for label, url, referer, site in steps:
            if self._stop:
                return
            try:
                resp = session.get(
                    url,
                    headers=nav_headers(self._profile, referer, site, self._lang),
                    timeout=WARMUP_TIMEOUT, allow_redirects=True,
                )
                status = resp.status_code
                if status in _BLOCK_STATUS:
                    self.log.emit(f'  ⚠ 预热{label}被限制 (HTTP {status})')
                elif status >= 400:
                    self.log.emit(f'  ⚠ 预热{label}返回 HTTP {status}')
            except _NETWORK_ERRORS as e:
                self.log.emit(f'  ⚠ 预热{label}失败: {e}')
            except Exception as e:  # 预热失败不应中断整个任务
                self.log.emit(f'  ⚠ 预热{label}异常: {e}')
            self._referer_chain.append(url)
            if not self._sleep(random.uniform(COOKIE_PAGE_DELAY_MIN, COOKIE_PAGE_DELAY_MAX)):
                return

        # m.douban.com 已不再提供帖子页 HTML（实测各种 UA 下恒 404），
        # 不再请求它，避免每次抓取都留下一条打不开页面的记录；
        # 但接口 Referer 仍指向帖子页——与「用户正在帖子页里触发 XHR」一致。
        self._referer_chain.append(topic_page)

        if self._check_bid(session):
            return
        # bid 通常由首页下发；若前面首页请求失败，补一次首页
        if self._stop:
            return
        try:
            session.get(home_url,
                        headers=nav_headers(self._profile, None, 'none', self._lang),
                        timeout=WARMUP_TIMEOUT, allow_redirects=True)
        except Exception:
            pass
        self._sleep(random.uniform(COOKIE_PAGE_DELAY_MIN, COOKIE_PAGE_DELAY_MAX))
        self._check_bid(session)

    def _new_identity_session(self):
        """创建一套完整身份：新 profile + 新 session + 重新预热"""
        self._profile = pick_profile()
        self._lang = pick_language()
        self._referer_chain = []
        self.log.emit(f'身份档位: {self._profile["label"]} '
                      f'（TLS/UA/Client Hints 同族）')
        session = create_session(self._profile, proxy=self._proxy())
        if not HAS_CURL_CFFI:
            self.log.emit('未安装 curl_cffi，回退 TLSAdapter 伪装（指纹一致性弱于 impersonate）')
        self._warmup(session)
        return session

    def _create_session(self):
        """创建首个 session（持锁，保证并发页拿到一致的身份快照）"""
        with self._identity_lock:
            self._session = self._new_identity_session()
            self._identity_epoch += 1
            self._last_rotate_ts = time.time()
            return self._session

    def _rotate_identity(self):
        """更换完整身份：换档位 + 新建 session + 重新预热（受最小间隔限制）

        只换 UA / 伪造 bid 而沿用原 TLS 会话是没用的：cookie 与 TLS 会话绑定，
        服务端标记的往往是整个连接身份。这里真正重建 session。
        """
        if self._stop:
            return False
        if time.time() - self._last_rotate_ts < IDENTITY_ROTATE_MIN_INTERVAL:
            self.log.emit(f'  距上次换身份不足 {IDENTITY_ROTATE_MIN_INTERVAL:.0f}s，先冷却重试')
            return False
        with self._identity_lock:
            if self._stop:
                return False
            if time.time() - self._last_rotate_ts < IDENTITY_ROTATE_MIN_INTERVAL:
                return False
            old = self._session
            self.log.emit('  更换完整身份（新 session + 重新预热）...')
            try:
                self._session = self._new_identity_session()
            except Exception as e:
                self.log.emit(f'  更换身份失败: {e}')
                self._session = old
                return False
            self._identity_epoch += 1
            self._last_rotate_ts = time.time()
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self.log.emit(f'  已更换身份: {self._profile["label"]}')
        return True

    def _identity_snapshot(self):
        """并发页启动时取一份身份快照（session 换代后旧线程 session 会重建）"""
        with self._identity_lock:
            session = self._session
            cookies = {}
            if session is not None:
                try:
                    cookies = session.cookies.get_dict()
                except Exception:
                    cookies = {}
            return {
                'epoch': self._identity_epoch,
                'cookies': cookies,
                'profile': self._profile,
                'lang': self._lang,
                'referer': self._referer(),
            }

    # -------------------- 被拦检测 / 冷却 --------------------

    def _block_reason(self, resp):
        """返回被拦截/限流原因；正常响应返回空字符串"""
        status = resp.status_code
        if status in _BLOCK_STATUS:
            return f'被限制 (HTTP {status})'
        if 'login' in (resp.url or '').lower():
            return '被重定向到登录页'
        content_type = (resp.headers.get('Content-Type', '') or '').lower()
        if status == 200 and 'text/html' in content_type:
            return '返回 HTML 而非 JSON'
        if status == 200 and _is_anti_bot_response(resp):
            return '检测到反爬错误信息'
        if status == 200 and len((resp.text or '').strip()) < 20:
            return '响应体过短'
        set_cookie = resp.headers.get('Set-Cookie', '') or ''
        if set_cookie and any(s in set_cookie.lower()
                              for s in ('captcha', 'sec=', '_sec', 'challenge')):
            return '检测到安全验证 Cookie'
        return ''

    def _block_cooldown(self, resp=None):
        """本次被拦需要冷却多久：优先尊重 Retry-After，否则指数增长

        并发评论页线程可能同时命中拦截，这里在同一把状态锁内读改写
        _consecutive_blocks/_pending_cooldown，保证计数与冷却不丢失。
        """
        retry_after = ''
        if resp is not None:
            try:
                retry_after = (resp.headers.get('Retry-After', '') or '').strip()
            except Exception:
                retry_after = ''
        with self._state_lock:
            if retry_after.isdigit():
                cooldown = min(float(retry_after), RETRY_AFTER_MAX)
            else:
                count = max(1, self._consecutive_blocks)
                cooldown = min(BLOCK_COOLDOWN_BASE * (2 ** (count - 1)), BLOCK_COOLDOWN_MAX)
            self._pending_cooldown = max(self._pending_cooldown, cooldown)
            return cooldown

    def _note_block(self, resp=None, reason='被限制'):
        """记录一次被拦截，返回本次冷却秒数（None 表示已经熔断，调用方应立即中止）。

        并发评论页线程会同时调用，计数/冷却/熔断原因都在状态锁内读改写；
        冷却等待由调用方在锁外执行，避免持锁 sleep。
        """
        with self._state_lock:
            first = self._consecutive_blocks == 0
            self._consecutive_blocks += 1
            count = self._consecutive_blocks
            cooldown = self._block_cooldown(resp)
            if count >= MAX_CONSECUTIVE_BLOCKS:
                self._abort_reason = (
                    f'连续 {count} 次被豆瓣拦截/限流（最近: {reason}），'
                    '已中止本次抓取，避免触发更长时间封禁；建议 10~30 分钟后再试'
                )
                abort = True
            else:
                abort = False
                if count >= IDENTITY_ROTATE_AFTER_BLOCKS:
                    self._force_rotate = True
        self._rate_limiter.report_failure()
        if first:
            self.log.emit(f'  ⚠ 检测到反爬拦截（{reason}），切换身份并冷却')
        if abort:
            self.log.emit(f'  ⛔ {self._abort_reason}')
            return None
        return cooldown

    def _note_success(self):
        with self._state_lock:
            blocks = self._consecutive_blocks
            self._consecutive_blocks = 0
            self._pending_cooldown = 0.0
            self._force_rotate = False
            self._abort_reason = ''
        if blocks:
            self.log.emit(f'  已恢复正常（此前连续被拦 {blocks} 次）')
        self._rate_limiter.report_success()

    def _retry_wait(self, attempt):
        """重试退避：指数退避 + 被拦冷却 + 自适应延迟的最小值，可被 stop 打断"""
        backoff = RETRY_BACKOFF_BASE ** attempt + random.uniform(0, 1)
        with self._state_lock:
            pending = self._pending_cooldown
            self._pending_cooldown = 0.0
        if pending:
            backoff = max(backoff, pending)
        d_min, _ = self._rate_limiter.get_delay()
        backoff = max(backoff, d_min)
        self.log.emit(f'  重试中... (等待 {backoff:.1f}s)')
        if not self._sleep(backoff):
            return False
        return not self._abort_message()

    # -------------------- 请求与重试 --------------------

    def _fetch_with_retry(self, url, params=None):
        """带冷却与身份轮换重试的请求。

        被拦分支只累计状态并冷却，实际换身份/退避在下一轮 attempt 开头做，
        避免同一轮里既换身份又重试（那样重试用的就是刚换的新身份，等于没换）。
        """
        for attempt in range(MAX_RETRIES):
            with self._state_lock:
                abort = self._abort_reason
                force_rotate = self._force_rotate
                blocks = self._consecutive_blocks
            if self._stop or abort:
                return None
            try:
                if attempt > 0:
                    if force_rotate or blocks >= IDENTITY_ROTATE_AFTER_BLOCKS:
                        with self._state_lock:
                            self._force_rotate = False
                        self._rotate_identity()
                    if not self._retry_wait(attempt):
                        return None

                # 请求前微抖动，打破机器人式等间隔请求
                if not self._sleep(random.uniform(0.05, 0.15)):
                    return None

                session = self._session
                if session is None:
                    session = self._create_session()
                resp = session.get(
                    url, params=params,
                    headers=xhr_headers(self._profile, self._referer(), self._lang),
                    timeout=DEFAULT_TIMEOUT,
                )

                reason = self._block_reason(resp)
                if reason:
                    cooldown = self._note_block(resp, reason)
                    if cooldown is None:
                        return None
                    self.log.emit(f'  {reason}，冷却 {cooldown:.0f}s 后重试...')
                    continue

                # 服务端 5xx 属于临时故障，冷却重试但不计入「被拦」熔断
                if 500 <= resp.status_code < 600:
                    self.log.emit(f'  服务器错误 (HTTP {resp.status_code})，稍后重试...')
                    self._rate_limiter.report_failure()
                    with self._state_lock:
                        self._pending_cooldown = max(
                            self._pending_cooldown,
                            min(BLOCK_COOLDOWN_BASE, RETRY_AFTER_MAX),
                        )
                        cooldown = self._pending_cooldown
                        self._pending_cooldown = 0.0
                    if not self._sleep(cooldown):
                        return None
                    continue

                self._note_success()
                return resp

            except _NETWORK_ERRORS as e:
                # 网络抖动不算被拦，只做退避重试
                if attempt < MAX_RETRIES - 1:
                    self.log.emit(f'  网络异常: {e}，准备重试...')
                    if not self._sleep(RETRY_BACKOFF_BASE ** attempt + random.uniform(0, 1)):
                        return None
                else:
                    raise
        return None

    # -------------------- 并发获取评论页 --------------------

    def _thread_session(self, snapshot):
        """获取线程局部 session；主身份换代（epoch 变化）时重建"""
        local = self._thread_local
        session = getattr(local, 'session', None)
        if session is None or getattr(local, 'epoch', None) != snapshot['epoch']:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            session = create_session(snapshot['profile'], proxy=self._proxy())
            # 复用主会话拿到的服务端 cookie（bid 等）
            session.cookies.update(snapshot['cookies'])
            local.session = session
            local.epoch = snapshot['epoch']
        return session

    def _fetch_comments_page(self, start):
        """获取单页评论。

        线程安全：身份参数全部来自启动时的 snapshot，不修改共享可变状态。
        成功返回 list（空列表也是合法结果）；失败返回 None，由调用方补漏。
        """
        abbrev = self._abort_message()
        if self._stop or abbrev:
            if abbrev:
                log_error(f'评论页 offset={start} 跳过：{abbrev}')
            return None
        snapshot = self._identity_snapshot()
        comments_url = f'{API_BASE}/{self.topic_id}/comments'
        params = {'start': start, 'count': COMMENTS_PER_PAGE, 'order': 'asc'}

        # 错开并发请求，避免同一瞬间打出多个请求（使用自适应延迟）
        d_min, _ = self._rate_limiter.get_delay()
        if not self._sleep(random.uniform(0.1, max(0.6, d_min * 0.3))):
            return None

        session = self._thread_session(snapshot)

        for attempt in range(MAX_RETRIES):
            abbrev = self._abort_message()
            if self._stop or abbrev:
                if abbrev:
                    log_error(f'评论页 offset={start} 中止：{abbrev}')
                return None
            try:
                if attempt > 0:
                    backoff = RETRY_BACKOFF_BASE ** attempt + random.uniform(0, 1)
                    if not self._sleep(backoff):
                        return None

                resp = session.get(
                    comments_url, params=params,
                    headers=xhr_headers(snapshot['profile'], snapshot['referer'],
                                        snapshot['lang']),
                    timeout=DEFAULT_TIMEOUT,
                )

                reason = self._block_reason(resp)
                if reason:
                    cooldown = self._note_block(resp, reason)
                    if cooldown is None:
                        return None
                    self.log.emit(f'  评论页 offset={start}: {reason}，冷却 {cooldown:.0f}s')
                    if not self._sleep(cooldown):
                        return None
                    continue

                if 500 <= resp.status_code < 600:
                    self._rate_limiter.report_failure()
                    if not self._sleep(random.uniform(2, 4)):
                        return None
                    continue

                if resp.status_code != 200:
                    return None

                try:
                    data = _response_json(resp)
                except Exception:
                    return None
                if not isinstance(data, dict):
                    return None
                comments = data.get('comments')
                if not isinstance(comments, list):
                    self.log.emit(f'  评论页 offset={start}: 响应缺少 comments 字段')
                    return None
                self._note_success()
                return comments

            except _NETWORK_ERRORS as e:
                if attempt < MAX_RETRIES - 1:
                    self.log.emit(f'  评论页 offset={start} 网络异常: {e}')
                    if not self._sleep(random.uniform(2, 4)):
                        return None
                else:
                    return None
        return None

    # -------------------- 主流程 --------------------

    def _resolve_short_link(self, url):
        """解析 d.douban.com 等分享短链，跟随重定向后提取帖子 ID"""
        if not url.startswith('http'):
            url = 'https://' + url
        session = None
        try:
            session = create_session(self._profile, proxy=self._proxy())
            resp = session.get(
                url, timeout=SHORT_LINK_TIMEOUT, allow_redirects=True,
                headers=nav_headers(self._profile, None, 'none', self._lang),
            )
            # 优先从重定向后的最终 URL 提取
            m = re.search(r'/topic/(\d+)', resp.url or '')
            if m:
                return m.group(1)
            # 部分短链落地页是 JS 跳转，从页面内容提取
            m = re.search(r'/topic/(\d+)', (resp.text or '')[:4096])
            if m:
                return m.group(1)
        except Exception as e:
            self.log.emit(f'  短链解析出错: {e}')
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
        return None

    def _local_comment_identity(self, floor):
        """读取本地某楼层评论的 (接口 id, 正文)，用作增量更新的重叠锚点"""
        row = get_comment_by_floor(self.topic_id, floor) or {}
        text = (row.get('content') or row.get('text') or '').strip()
        cid = None
        raw = row.get('raw_json')
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get('id') is not None:
                    cid = str(data['id'])
            except (ValueError, TypeError):
                cid = None
        return cid, text

    def _incremental_anchor(self, first_data, base):
        """在重叠页里定位「本地最后一条评论」，返回它在页内的下标

        - 返回 None：楼层已位移（有评论被删），必须完整重抓
        - 返回 0：重叠条正好是页内第一条（没有隐藏评论）
        - 返回 N：接口把 N 条隐藏/折叠评论也算进了 start 位置，页内第一条
          其实比 base+1 楼更靠前，整页楼层要往前校正 N

        豆瓣的 start 是「含隐藏评论的绝对位置」：帖子里有折叠评论时，
        start=base 返回的首条并不是第 base+1 楼（实测 505492619：total=44，
        可见 43 条，start=42 返回的是第 42 条）。所以不能假设
        「页内第一条 == base+1 楼」，必须按 id/正文找到锚点再换算楼层。

        注意：接口输出上「隐藏评论」与「被删评论」不可区分（两者都只是从列表里
        消失，前者仍计入 total、后者不计）。这里统一按隐藏处理：本地已有楼层保持
        不变，新评论继续按「本地最大楼层 + 1」递增。若实际是评论被删，楼层会短暂
        比豆瓣页面多算 shift 个，下一次完整抓取（replace=True）会自愈。
        """
        if base <= 0:
            return 0
        target_floor = base + 1
        total = first_data.get('total', 0) or 0
        if total and total < target_floor:
            # 接口总数比本地最后楼层还小 → 有评论被删，楼层已位移
            return None
        comments = first_data.get('comments') or []
        if not comments:
            # 接口没返回重叠内容：可能是限流返回的空页，不能当成「没有新评论」，
            # 返回 None 让上层回退完整抓取（完整抓取仍空则由上层判断是否中止）。
            return None
        cid, text = self._local_comment_identity(target_floor)
        for idx, c in enumerate(comments):
            if not isinstance(c, dict):
                continue
            if cid and str(c.get('id')) == cid:
                return idx
            c_text = (c.get('content') or c.get('text') or '').strip()
            if text and c_text == text:
                return idx
        return None

    def run(self):
        """执行爬取"""
        result = {}

        # 短链输入（非纯数字）：先解析重定向拿到帖子 ID
        if not str(self.topic_id).isdigit():
            self.log.emit(f'检测到链接输入，正在解析: {self.topic_id}')
            resolved = self._resolve_short_link(str(self.topic_id))
            if not resolved:
                self.error.emit(f'无法从链接解析出帖子 ID: {self.topic_id}')
                return
            self.topic_id = resolved
            self.log.emit(f'链接已解析为帖子 ID: {self.topic_id}')

        self._create_session()
        if self._stop:
            self.error.emit('已取消')
            return

        # Step 1: 获取帖子详情
        self.log.emit('正在获取帖子详情...')
        topic_url = f'{API_BASE}/{self.topic_id}'
        try:
            resp = self._fetch_with_retry(topic_url)
            if not resp or resp.status_code != 200:
                if self._abort_reason:
                    self.error.emit(self._abort_message() or self._abort_reason)
                else:
                    self.error.emit(f'获取帖子详情失败 (HTTP {resp.status_code if resp else "无响应"})')
                return
            try:
                topic_data = _response_json(resp)
            except Exception:
                self.error.emit(f'返回内容非 JSON (可能被反爬拦截)，前 200 字符: {resp.text[:200]}')
                return
            result['topic'] = topic_data
            self.log.emit(f'标题: {topic_data.get("title", "N/A")}')
            self.log.emit(f'作者: {topic_data.get("author", {}).get("name", "N/A")}')
            self.log.emit(f'回复数: {topic_data.get("comments_count", 0)}')
        except Exception as e:
            self.error.emit(f'获取帖子详情出错: {e}')
            return

        if self._stop:
            # 取消也必须发信号，否则 UI 状态无法复位、线程无法退出
            self.error.emit('已取消')
            return

        # Step 2: 获取评论
        comments_count = topic_data.get('comments_count', 0)
        comments_url = f'{API_BASE}/{self.topic_id}/comments'

        # 增量更新：只从「本地最后一条评论」往后拉。
        # 楼层 = API 绝对偏移 + 1，所以 start = 最后楼层 - 1 就能多取 1 条，
        # 用来校验楼层是否还对齐（中间有楼被删时后面整段上移，必须回退完整抓取）。
        base = 0
        floor_shift = 0  # 隐藏/折叠评论导致的楼层校正量（见 _incremental_anchor）
        incremental = False
        if self.incremental:
            last_floor = 0
            try:
                last_floor = get_max_floor(self.topic_id)
            except Exception as e:
                self.log.emit(f'  读取本地楼层失败，改为完整抓取: {e}')
            if last_floor:
                incremental = True
                base = max(0, last_floor - 1)
                self.log.emit(f'增量更新: 本地已到 #{last_floor}，从偏移 {base} 开始拉取')
            else:
                self.log.emit('增量更新: 本地还没有该帖评论，改为完整抓取')

        def _fetch_first(start):
            resp = self._fetch_with_retry(
                comments_url,
                params={'start': start, 'count': COMMENTS_PER_PAGE, 'order': 'asc'},
            )
            if not resp or resp.status_code != 200:
                detail = self._abort_message() or self._abort_reason or (
                    f'HTTP {resp.status_code if resp else "无响应"}')
                raise RuntimeError(f'获取评论失败 ({detail})')
            return _response_json(resp)

        self.log.emit(f'正在获取评论 (共 {comments_count} 条)...')
        self.progress.emit(base, max(comments_count, 1, base))

        try:
            first_data = _fetch_first(base)
        except Exception as e:
            if self._abort_reason:
                self.error.emit(self._abort_message() or self._abort_reason)
            else:
                self.error.emit(f'获取评论出错: {e}')
            return

        # 增量模式首屏为空：大概率是限流返回的空壳页，不能当成「没有新评论」。
        # 先重试一次，仍为空则回退完整抓取，保证不会漏掉真实的新评论。
        if incremental and not (first_data.get('comments') or []):
            self.log.emit('  增量首屏为空，疑似限流，重试一次...')
            retried = None
            if self._sleep(1.5):
                try:
                    retried = _fetch_first(base)
                except Exception as e:
                    self.log.emit(f'  增量首屏重试失败: {e}')
            if retried is not None:
                first_data = retried
            if not (first_data.get('comments') or []):
                self.log.emit('  增量首页为空，疑似限流，已回退完整抓取')
                incremental = False
                base = 0
                floor_shift = 0
                try:
                    first_data = _fetch_first(0)
                except Exception as e:
                    self.error.emit(self._abort_message() or f'获取评论出错: {e}')
                    return

        # 增量模式：先确认「本地最后一条评论」还在这页里、内容没变。
        # 对不上说明有评论被删、楼层整体上移，此时按楼层 upsert 会把旧数据改错，
        # 必须从头完整抓一遍。
        if incremental:
            anchor = self._incremental_anchor(first_data, base)
            if anchor is None:
                self.log.emit(f'  ⚠ 本地楼层 #{base + 1} 与接口不一致（可能有评论被删除），改为完整抓取')
                incremental = False
                base = 0
                floor_shift = 0
                try:
                    first_data = _fetch_first(0)
                except Exception as e:
                    if self._abort_reason:
                        self.error.emit(self._abort_message() or self._abort_reason)
                    else:
                        self.error.emit(f'获取评论出错: {e}')
                    return
            elif anchor:
                # 接口把隐藏/折叠评论也计入 start 位置，整页（及后续页）楼层往前校正
                floor_shift = anchor
                self.log.emit(f'  接口含 {anchor} 条隐藏/折叠评论，楼层按重叠锚点校正 -{anchor}')

        total = first_data.get('total', 0) or 0
        first_comments = first_data.get('comments', []) or []
        # 楼层必须按 API 的绝对偏移计算，不能等合并后再从 1 重排：
        # 中间某页失败时，后面的评论会被错误地挤到前面的楼层并覆盖旧数据。
        for idx, c in enumerate(first_comments):
            if isinstance(c, dict) and not c.get('floor'):
                c['floor'] = max(1, base + idx + 1 - floor_shift)
        all_comments = list(first_comments)
        # 增量模式永远不完整：绝不能用 replace=True，否则会把历史评论清空
        comments_complete = not incremental

        # ---- 诊断日志：对比 API 返回值 ----
        self.log.emit(f'  诊断: topic.comments_count={comments_count}, API.total={total}, '
                      f'首页返回={len(first_comments)} 条')
        if total != comments_count:
            self.log.emit(f'  ⚠ API 返回的 total ({total}) 与帖子评论数 ({comments_count}) 不一致！')
            self.log.emit('  可能原因: API 有返回上限 / 楼中楼被单独计数 / 被折叠评论不计入')
        if not first_comments and (total or comments_count) and not incremental:
            # 首页该有评论却返回空列表：大概率不是「没有评论」，而是被限流/风控
            # 返回了空壳 JSON。此时继续保存会清空旧数据或写入残缺数据，直接中止。
            self._abort_reason = (
                '接口返回空评论列表（帖子显示有评论），疑似被限流/风控；'
                '为避免写入不完整数据，已中止本次抓取，建议稍后重试'
            )
            self._rate_limiter.report_failure()
            self.log.emit(f'  ⛔ {self._abort_reason}')
            self.error.emit(self._abort_message() or self._abort_reason)
            return
        if incremental and not first_comments:
            self.log.emit('  增量更新: 接口没有返回更新的评论')

        target_total = max(total, comments_count, base + len(all_comments))
        self.progress.emit(base + len(all_comments), target_total)
        self.log.emit(f'  第 1 页: 获取 {len(first_comments)} 条 '
                      f'(累计 {base + len(all_comments)}/{target_total})')

        # 剩余页并发获取 + 失败补漏
        # 循环上界必须用 target_total：total 可能小于帖子显示的 comments_count，
        # 只用 total 会提前停页，而下面的完整性判断又按 target_total 算，必然判为不完整。
        # 完整性用「应有 offset 是否都成功拿到」判断，首页(base)也计入成功页。
        offsets = []
        page_results = {base: first_comments}
        failed_offsets = []
        if (first_comments and base + len(all_comments) < target_total
                and not self._stop and not self._abort_reason):
            # 分页步长不能直接用 COMMENTS_PER_PAGE：豆瓣 API 单页上限虽是 100，
            # 但被限流/删帖时首页可能只返回更少条数，此时按首页实际条数缩小步长
            # （窗口会重叠，由后面的内容指纹去重处理），任何一页都不留空洞。
            page_size = max(1, min(len(first_comments), COMMENTS_PAGE_STEP))
            offsets = []
            start = base + page_size
            while start < target_total:
                offsets.append(start)
                start += page_size

            def _page_no(off):
                return off // page_size + 1

            # 只有确实还有后续页要抓时才值得告警：增量更新尾部通常本就没那么多新评论
            if page_size < COMMENTS_PER_PAGE and offsets:
                self.log.emit(f'  ⚠ 首页只返回 {page_size} 条（请求 {COMMENTS_PER_PAGE}），'
                              f'已按实际页大小计算偏移量，避免跨页漏抓')

            self.log.emit(f'  待获取 {len(offsets)} 页 (offset: {offsets[0]} ~ {offsets[-1]})')

            # ---- 第一轮：并发获取（在飞任务数随自适应限流动态调整）----
            # page_results 已放入首页(base)结果，后续页面继续累加；
            # failed_offsets 只记录失败项，补漏成功的会从列表移除。
            executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PAGES)
            pending = {}  # future -> offset
            offset_iter = iter(offsets)

            def _submit_next():
                try:
                    off = next(offset_iter)
                except StopIteration:
                    return False
                pending[executor.submit(self._fetch_comments_page, off)] = off
                return True

            limit = self._rate_limiter.get_concurrent_pages()
            self.log.emit(f'  并发获取剩余页 (初始并发数: {limit}, 上限 {MAX_CONCURRENT_PAGES})')
            for _ in range(min(limit, len(offsets))):
                _submit_next()
            try:
                while pending and not self._stop and not self._abort_reason:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        off = pending.pop(future)
                        page_num = _page_no(off)
                        try:
                            page_comments = future.result()
                            if page_comments is not None:
                                page_results[off] = page_comments
                                self.log.emit(f'  第 {page_num} 页: ✓ 获取 {len(page_comments)} 条')
                            else:
                                failed_offsets.append(off)
                                self.log.emit(f'  第 {page_num} 页: ✗ 失败 (无响应/被拦截)')
                        except Exception as e:
                            failed_offsets.append(off)
                            self.log.emit(f'  第 {page_num} 页: ✗ 异常 ({e})')
                    # 每完成一批就重新读一次限流等级：被限时不再提交新页，
                    # 冷却后连续成功会恢复默认等级，再逐步把并发补回来。
                    limit = self._rate_limiter.get_concurrent_pages()
                    while (not self._stop and not self._abort_reason
                           and len(pending) < limit):
                        if not _submit_next():
                            break
            finally:
                # 停止时不要把 UI 卡在等待所有在飞请求上；未停止则正常等待。
                executor.shutdown(wait=not self._stop, cancel_futures=self._stop)

            # ---- 第二轮：用主 session 顺序补漏失败的页面 ----
            if self._abort_reason:
                self.log.emit('  熔断中止：跳过补漏重试，避免继续触发风控')
            elif failed_offsets and not self._stop:
                self.log.emit(f'  补漏重试 {len(failed_offsets)} 个失败页面 (使用主 session)...')
                retry_offsets = sorted(failed_offsets)
                for retry_idx, off in enumerate(retry_offsets):
                    if self._stop or self._abort_reason:
                        break
                    page_num = _page_no(off)
                    # 使用主 session 的 _fetch_with_retry（具备完整反爬检测 + 预热链）
                    params = {'start': off, 'count': COMMENTS_PER_PAGE, 'order': 'asc'}
                    try:
                        retry_resp = self._fetch_with_retry(comments_url, params=params)
                        if retry_resp and retry_resp.status_code == 200:
                            try:
                                retry_data = _response_json(retry_resp)
                                retry_comments = retry_data.get('comments', [])
                                if retry_comments:
                                    page_results[off] = retry_comments
                                    self.log.emit(f'  第 {page_num} 页: ✓ 补漏成功 ({len(retry_comments)} 条)')
                                else:
                                    self.log.emit(f'  第 {page_num} 页: ✗ 补漏返回空 (API 可能已达上限)')
                            except Exception:
                                self.log.emit(f'  第 {page_num} 页: ✗ 补漏 JSON 解析失败')
                        else:
                            self.log.emit(f'  第 {page_num} 页: ✗ 补漏请求失败')
                    except Exception as e:
                        self.log.emit(f'  第 {page_num} 页: ✗ 补漏异常 ({e})')
                    # 补漏请求之间加延迟，避免触发限流（最后一页之后不必再等）
                    if retry_idx < len(retry_offsets) - 1:
                        if not self._sleep(random.uniform(1.5, 3.0)):
                            break

            # 按偏移顺序合并，保持楼层顺序；楼层按绝对偏移计算。
            # 首页(base)已在上面合并过，这里跳过避免重复。
            for off in sorted(page_results):
                if off == base:
                    continue
                for idx, c in enumerate(page_results[off]):
                    if isinstance(c, dict) and not c.get('floor'):
                        c['floor'] = max(1, off + idx + 1 - floor_shift)
                    all_comments.append(c)

            # 记录补漏后仍失败的 offset（用于完整性判断的诊断日志）
            failed_offsets = sorted(off for off in offsets if off not in page_results)

        # ---- 完整性判断：按 offset 覆盖判断，而不是去重前条数 ----
        # 分页窗口可能重叠、补漏也可能重复取回同一页，去重前条数会虚高，
        # 用 fetched 判断会误判为完整抓取，进而 replace=True 清掉历史评论。
        # 只有「应有 offset 全部拿到成功响应」且未被熔断中止，才允许覆盖旧评论。
        fetched = base + len(all_comments)  # 仅用于进度/日志，不参与完整性判断
        self.progress.emit(fetched, target_total)
        expected_offsets = [base] + list(offsets)
        missing = sorted(off for off in expected_offsets if off not in page_results)
        comments_complete = (not incremental) and (not self._abort_reason) and (not missing)
        self.log.emit(f'  分页抓取成功 {len(page_results)}/{len(expected_offsets)} 页，'
                      f'合并去重前 {len(all_comments)} 条')
        if self._abort_reason:
            comments_complete = False
            log_error(f'抓取被反爬中止，不视为完整抓取，将保留已有评论数据：'
                      f'{self._abort_message()}')
        elif missing:
            self.log.emit(f'  ⚠ 补漏后仍有 {len(missing)} 个分页抓取失败（offset 从 '
                          f'{missing[0]} 起: {missing}），评论可能不完整，将保留原有评论数据')
        elif comments_complete:
            self.log.emit('  评论抓取完整（offset 全覆盖），可安全覆盖旧评论')
        # 增量模式只补抓新楼层，本来就不覆盖 1..base，这里的「缺失」是按接口
        # 总数算的绝对值，只做诊断提示，不参与完整性判断。
        if fetched < target_total and not incremental:
            lost = target_total - fetched
            self.log.emit(f'  ⚠ 缺失 {lost} 条评论（目标 {target_total}，实际 {fetched}），可能原因:')
            self.log.emit('    1. 豆瓣 API 有实际返回上限（最常见）')
            self.log.emit('    2. 被删除/折叠/审核中的评论不会在 API 中返回')
            self.log.emit('    3. 部分页面被反爬拦截')

        if self._stop:
            # 取消也必须发信号，否则 UI 状态无法复位、线程无法退出
            self.error.emit('已取消')
            return

        # ---- 去重 ----
        # 豆瓣响应里没有 floor 字段，楼层是下面按数组下标合成的。若分页窗口出现
        # 重叠（API 对超大 start 做回退）或补漏重复取回同一页，就会出现重复评论，
        # 合成楼层后反而变成「正常的连续楼层」，从界面和数据库都发现不了。
        # 这里按内容指纹去重；floor 保留 API 绝对偏移对应的真实序号，
        # 去重后允许出现空洞（不重排，避免把真实楼层改错）。
        def _comment_key(c):
            if not isinstance(c, dict):
                return ('raw', repr(c))
            author = c.get('author')
            a_uid = author.get('uid', '') if isinstance(author, dict) else str(author)
            # floor 此时还没生成（全是 None），不能放进 key；用评论 id/内容/时间/
            # 点赞/ip/配图/回复关系组合，既去掉分页重复，又尽量不误删两条真实相同评论。
            return (c.get('id'), a_uid,
                    (c.get('content') or c.get('text') or '').strip(),
                    c.get('create_time') or c.get('time') or '',
                    c.get('likes'), c.get('ip_location'),
                    repr(c.get('photos')), repr(c.get('reply_to')))

        deduped = []
        seen_keys = set()
        duplicates = 0
        merged_count = len(all_comments)
        for c in all_comments:
            key = _comment_key(c)
            if key in seen_keys:
                duplicates += 1
                continue
            seen_keys.add(key)
            deduped.append(c)
        self.log.emit(f'  评论去重完成：合并 {merged_count} 条，去重后 {len(deduped)} 条，'
                      f'重复 {duplicates} 条')
        all_comments = deduped

        # 此时每条评论的 floor 都已按「绝对偏移 + 页内序号」赋值（首页与分页合并处），
        # 不能再去重后按下标重排：那会把真实楼层改成看似连续的假序号。

        # 本次真正抓到的评论（增量时含 1 条用于校验楼层是否位移的重叠评论）
        fetched_comments = all_comments

        # ---- 增量模式：把库里旧评论拼回来，界面/导出要看到完整帖子 ----
        new_count = 0
        if incremental:
            # 重叠的那条（floor == base + 1）是本地已有的最后一条，不算新增
            known_floor = base + 1
            new_count = sum(
                1 for c in fetched_comments
                if isinstance(c, dict) and (c.get('floor') or 0) > known_floor
            )
            try:
                old_comments = get_comment_dicts(self.topic_id)
            except Exception as e:
                old_comments = []
                self.log.emit(f'  读取本地旧评论失败（界面只显示本次新评论）: {e}')
            merged = {}
            for c in old_comments:
                if isinstance(c, dict) and c.get('floor') is not None:
                    merged[c['floor']] = c
            # 新抓到的覆盖旧行（重叠的那条以接口最新内容为准）
            for c in fetched_comments:
                if isinstance(c, dict) and c.get('floor') is not None:
                    merged[c['floor']] = c
            all_comments = [merged[k] for k in sorted(merged)]
            self.log.emit(f'  增量结果: 新增 {new_count} 条，合并后共 {len(all_comments)} 条')

        # ---- 回复关系归一化 ----
        # 豆瓣 comments 接口用 ref_comment（被回复评论的完整对象，但没有 floor）
        # 表示「楼中楼回复」。统一转成 reply_to 并补上楼层层级，界面、导出、
        # 数据库三处都只认 reply_to，避免各自解析造成口径不一致。
        id_to_floor = {
            str(c.get('id')): c.get('floor')
            for c in all_comments if isinstance(c, dict) and c.get('id') is not None
        }
        reply_links = 0
        for c in all_comments:
            if not isinstance(c, dict) or c.get('reply_to'):
                continue
            ref = c.get('ref_comment')
            if isinstance(ref, dict):
                c['reply_to'] = normalize_ref_comment(ref, id_to_floor)
                if c.get('reply_to'):
                    reply_links += 1
        if reply_links:
            self.log.emit(f'  回复关系: {reply_links} 条评论带楼中楼引用')

        # 提取图片 URL（区分来源：topic / comment，并记录评论图片所属楼层）
        topic_image_urls = []
        comment_image_urls = []
        comment_image_floors = {}  # url -> 评论楼层
        seen = set()

        def _add(url, source_list, floor=None):
            if url and url not in seen:
                seen.add(url)
                source_list.append(url)
                if floor is not None and source_list is comment_image_urls:
                    comment_image_floors[url] = floor

        topic_content = topic_data.get('content', '')
        for url in extract_image_urls(topic_content):
            _add(url, topic_image_urls)
        for c in all_comments:
            c_floor = c.get('floor')
            c_content = c.get('content', c.get('text', ''))
            for url in extract_image_urls(c_content):
                _add(url, comment_image_urls, c_floor)
            # 豆瓣 API 评论图片以 photos 数组形式独立返回（URL 在 image.normal.url）
            for photo in (c.get('photos') or []):
                if isinstance(photo, dict):
                    img_obj = photo.get('image') or {}
                    pu = ''
                    if isinstance(img_obj, dict):
                        normal = img_obj.get('normal') or {}
                        pu = normal.get('url') if isinstance(normal, dict) else ''
                    if not pu:
                        pu = photo.get('url') or photo.get('src') or ''
                    if pu:
                        _add(pu, comment_image_urls, c_floor)
        unique_images = topic_image_urls + comment_image_urls
        result['image_urls'] = unique_images
        if unique_images:
            self.log.emit(f'发现 {len(unique_images)} 张图片')

        result['comments'] = all_comments
        result['total_comments'] = len(all_comments)
        result['incremental'] = incremental
        result['new_count'] = new_count if incremental else len(all_comments)

        # 保存到数据库：单连接单事务，帖子/评论/图片要么全部写入、要么全部回滚。
        # 评论是否覆盖旧数据由上面的 offset 完整性判断决定（comments_complete）。
        try:
            topic_id = save_scrape_result(
                topic_data,
                fetched_comments,
                replace=comments_complete,
                topic_image_urls=topic_image_urls,
                comment_image_urls=comment_image_urls,
                comment_image_floors=comment_image_floors,
            )
            self.log.emit(f'已保存到数据库 (topic_id={topic_id})')
        except Exception as exc:
            log_error(f'数据保存失败（已回滚）：{exc}\n{traceback.format_exc()}')
            self.error.emit(f'数据保存失败，已回滚：{exc}')
            return

        if self._abort_reason:
            # 部分数据已保存，但必须让 UI 知道任务是被风控中止的
            self.log.emit('  ⚠ 本次抓取被熔断中止，仅保存已获取的部分数据（不覆盖历史完整数据）')
            self.error.emit(self._abort_message() or self._abort_reason)
            return

        if incremental:
            self.log.emit(f'完成！新增 {new_count} 条评论（本地共 {len(all_comments)} 条）')
        else:
            self.log.emit(f'完成！共获取 {len(all_comments)} 条评论')

        self.finished.emit(result)
