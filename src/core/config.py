# -*- coding: utf-8 -*-
"""反反爬配置

核心原则：**一个设备档位（DEVICE_PROFILES）同时决定 TLS 指纹、UA、Client Hints
和请求头**。以前 UA 池里有 iPhone Safari、也有 Android Chrome，但 TLS 永远
impersonate='chrome120'，等于告诉豆瓣「UA 说自己是 Safari，TLS 却是桌面
Chrome」——这是比不伪装更容易被识别的矛盾指纹。

curl_cffi 的 impersonate 会自带与 UA 匹配的 TLS/HTTP2 指纹和默认请求头，
因此这里不再维护 UA/屏幕/设备内存等散装池子，同一个 profile 内所有字段
天然一致。
"""

# ==================== 设备档位（TLS + UA + Client Hints 一体） ====================
#
# 'family'      给日志/强制指定用
# 'impersonate' curl_cffi 指纹档位（决定 TLS/JA3、HTTP2 帧序、默认头）
# 'ua'          与该指纹同族、同大版本的浏览器 UA
# 'ch_*'        Chromium 系必须与 UA 的版本一致；Safari 不发 Client Hints
# 'nav_accept'  顶层导航请求的 Accept（XHR 用固定 JSON Accept）
DEVICE_PROFILES = [
    {
        'family': 'chrome-android',
        'label': 'Chrome 131 / Android 10',
        'impersonate': 'chrome131_android',
        'ua': ('Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36'),
        'ch_ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        'ch_mobile': '?1',
        'ch_platform': '"Android"',
        'nav_accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                       'image/avif,image/webp,image/apng,*/*;q=0.8,'
                       'application/signed-exchange;v=b3;q=0.7'),
    },
    {
        'family': 'safari-ios',
        'label': 'Safari 18 / iOS 18',
        'impersonate': 'safari180_ios',
        'ua': ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
               'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 '
               'Mobile/15E148 Safari/604.1'),
        # Safari 不发送 Sec-CH-UA 系列
        'ch_ua': '',
        'ch_mobile': '',
        'ch_platform': '',
        'nav_accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    },
]

# curl_cffi 不可用（回退 requests）时的默认档位
DEFAULT_IMPERSONATE = 'chrome131_android'

# ==================== Accept-Language 轮换 ====================

ACCEPT_LANGUAGES = [
    'zh-CN,zh;q=0.9,en;q=0.8',
    'zh-CN,zh;q=0.9',
    'zh-CN,zh;q=0.8,en-US;q=0.6,en;q=0.4',
    'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.6',
    'zh-Hans-CN,zh-Hans;q=0.9,en;q=0.8',
]

# ==================== 豆瓣 API ====================

API_BASE = 'https://m.douban.com/rexxar/api/v2/group/topic'

# 请求默认超时（秒）：评论 API 响应可达 100KB+，正常网络下单次 1s 内完成。
# 60s 的真实问题不在正常请求，而在网络被墙/抖动时会白白卡满一分钟；
# 25s 仍给足慢速连接余量，同时把「偶发挂起」的最坏耗时压到原来的 1/2 以内。
DEFAULT_TIMEOUT = 25

# 预热导航超时（秒）：预热页只有几十 KB，8s 足够；预热失败本身不致命，
# 快速失败后还能补一次首页拿 bid，比死等 15s 更划算。
WARMUP_TIMEOUT = 8

# 短链跳转解析超时（秒）：只做一次 HEAD 式跳转跟随，10s 足够。
SHORT_LINK_TIMEOUT = 10

# 重试次数
MAX_RETRIES = 4

# 每页评论数（请求时传的值）。
# 实测豆瓣接口 count 上限为 100：传 100 返回 100 条，传 150/200 也只返回 100 条。
# 单页 100 条可把 300 条评论的请求数从 7 页降到 3 页，请求更少、翻页节奏也更像真人。
COMMENTS_PER_PAGE = 100

# 计算后续页 start 偏移时的步长（与请求条数保持一致）。
# 步长必须等于「实际能拿到的条数」：若按 100 推进而每页只回 48 条，每页都会漏 52 条。
# 若首页因限流/删帖返回更少，爬虫会按首页实际条数缩小步长（窗口重叠，由去重处理），
# 保证任何情况下都不留空洞。
COMMENTS_PAGE_STEP = COMMENTS_PER_PAGE

# 并发获取评论页的线程数。
# 真实浏览器滚动时确实会并发拉页，但爬虫 6 个请求瞬间打完更像脚本；
# 2 并发在速度与「不触发风控」之间更平衡，被限时自适应还会降到 1。
MAX_CONCURRENT_PAGES = 2

# 自适应并发：被限制时降至此值
MAX_CONCURRENT_ADAPTIVE_MIN = 1

# 图片下载并发数。
# 图片是 img*.doubanio.com 的静态 CDN 资源，浏览器对单域名并发本来就在 6 左右，
# 2 并发是纯瓶颈：实测同一批 16 张图，2 并发要 20~54s，4~6 并发只要 3~4.5s；
# 250 张图的帖子从数分钟降到约 1 分钟。被 403/429 时仍有自适应退避，不会硬刷。
IMAGE_DOWNLOAD_CONCURRENCY = 6

# 单张图片大小上限（字节）。
# 豆瓣原图通常 < 10MB；超过该值多半是被 CDN 换成了错误页/视频片段，
# 或异常的巨型文件。请求先用 Content-Length 预检、落盘后再按实际大小复核，
# 超限直接判失败，避免坏图占满磁盘（也避免把坏文件永久标记为已下载）。
MAX_IMAGE_BYTES = 30 * 1024 * 1024

# ==================== 延迟策略 ====================

# 页间延迟范围（秒）—— 仅用于顺序回退
PAGE_DELAY_MIN = 0.5
PAGE_DELAY_MAX = 1.5

# 重试基础退避（秒）
RETRY_BACKOFF_BASE = 2

# Cookie 获取页延迟
COOKIE_PAGE_DELAY_MIN = 0.4
COOKIE_PAGE_DELAY_MAX = 1.0

# ==================== 自适应延迟 ====================

# 自适应延迟范围（秒），被限流时指数增长到此上限
ADAPTIVE_DELAY_MIN = 1.0
ADAPTIVE_DELAY_MAX = 15.0

# 每次被限后延迟范围乘法因子（指数退避）
ADAPTIVE_BACKOFF_FACTOR = 1.5

# 冷却时间（秒）：连续成功此时间后恢复默认延迟
ADAPTIVE_COOLDOWN = 60.0

# ==================== 身份轮换 / 熔断 ====================

# 两次完整身份轮换（换 profile + 新 session + 重新预热）的最小间隔。
# 频繁换身份本身就像脚本：真实用户不会每 5 秒换一台设备。
IDENTITY_ROTATE_MIN_INTERVAL = 45.0

# 连续被拦多少次后允许换身份（配合上面的最小间隔）
IDENTITY_ROTATE_AFTER_BLOCKS = 2

# 单次任务内连续被拦达到该次数就熔断中止，避免把临时限流刷成长时间封禁
MAX_CONSECUTIVE_BLOCKS = 5

# 被拦后的冷却基数（秒）与上限（秒），按连续被拦次数指数增长
BLOCK_COOLDOWN_BASE = 20.0
BLOCK_COOLDOWN_MAX = 120.0

# 服务端 Retry-After 的最大等待（秒），防止被异常值拖死
RETRY_AFTER_MAX = 120.0

# ==================== 模拟浏览路径 ====================

# 预热路径（模拟真实用户先浏览再进帖子）。
# 只保留实测可用的页面：m.douban.com 的 /explore 恒 404，/movie/、
# /gallery/、/subject_collection/hot_movies 在当前网络下经常十几秒不响应；
# 预热页失败虽然会被吞掉，但既拖慢启动又会在日志里留下异常记录。
WARMUP_PATHS = [
    'https://m.douban.com/',
    'https://m.douban.com/group/',
]

# ==================== OCR 配置 ====================

# OCR 默认语言（中文简体 + 英文）
# 其他可选值：'chi_sim'、'chi_tra+eng'、'eng'、'jpn+eng' 等
# 需先安装对应的 Tesseract 语言包。
# 注意：界面里「设置 → OCR 识别语言」保存的选择会覆盖这里的默认值。
OCR_LANGUAGE = 'chi_sim+eng'

# Tesseract 二进制路径（None 表示自动检测）
# 若自动检测失败，可在此手动指定，例如：
#   OCR_TESSERACT_PATH = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
OCR_TESSERACT_PATH = None

# OCR 单张图片下载超时（秒）。
# 同时作为 Tesseract 单张识别的超时上限（pytesseract.image_to_string(timeout=...)）：
# 卡死/超长图最多等这么久，worker 会记录失败并继续下一张，而不是无限挂起。
OCR_IMAGE_TIMEOUT = 30

# 预留开关：爬取完成后自动跑 OCR。
# 目前未接线（界面上的「OCR 识别图片文字」按钮是唯一的触发入口），
# 保留常量仅为后续实现预留，改这里当前不会有任何效果。
OCR_AUTO_RUN = False

# OCR 图片最小尺寸（像素），过小的图片跳过（避免误识别）
OCR_MIN_IMAGE_SIZE = 50

# OCR 引擎选择：
#   'auto'      - 装了 rapidocr-onnxruntime 就用它（中文准确率更高、支持多线程），
#                 没装则自动回退 Tesseract
#   'rapidocr'  - 强制使用 RapidOCR（未安装时 OCR 不可用）
#   'tesseract' - 强制使用 Tesseract
OCR_ENGINE = 'auto'

# OCR 并发线程数（下载 + 识别一起并发）。0 = 自动（最多 8，且不超过 CPU 核数）
OCR_WORKERS = 0

# RapidOCR 是否做文字方向分类。截图基本都是正向，关掉可省 10~20% 时间；
# 如果经常识别竖排/倒置的图，改回 True。
OCR_RAPID_CLS = False

# RapidOCR 识别结果的置信度下限（0~1），低于该值的行会被丢弃
OCR_RAPID_TEXT_SCORE = 0.5

# RapidOCR 检测端最长边：把过大的图缩到该尺寸再检测，图越大越慢。
# 截图常见 600~1280 宽，960 是速度/准确率的平衡点（736/640 更快但小字会糊）。
OCR_RAPID_DET_SIDE = 960

# RapidOCR 每个会话的 onnxruntime 线程数。
# 0 = 自动：CPU 核数 / 并发数（上限 4）。并发跑多张时每会话独占全核会互相抢 CPU，
# 反而更慢——8 并发、18 核的机器上 2 线程/会话比默认快约 30%。
OCR_RAPID_THREADS = 0

# 是否用 GPU 加速识别。'auto' 会按 onnxruntime 实际可用的 provider 自动挑：
#   'auto' - 有 CUDA 用 CUDA，没有则 DirectML，都没有则 CPU
#   'cuda' - 强制 CUDA（NVIDIA 卡，需要 onnxruntime-gpu + CUDA 12）
#   'dml'  - 强制 DirectML（Windows 通用，A 卡/核显也能用）
#   'cpu'  - 强制用 CPU
# CUDA 装法：pip uninstall onnxruntime onnxruntime-directml -y
#            pip install onnxruntime-gpu==1.22.0 nvidia-cudnn-cu12 nvidia-cublas-cu12
#                        nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 nvidia-curand-cu12
# DirectML 装法：pip uninstall onnxruntime onnxruntime-gpu -y && pip install onnxruntime-directml
OCR_DEVICE = 'auto'

# ==================== 辅助函数 ====================

def random_accept_language():
    """随机 Accept-Language（身份模块使用）"""
    import random
    return random.choice(ACCEPT_LANGUAGES)
